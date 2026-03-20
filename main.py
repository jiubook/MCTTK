#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py — MCTTK 新闻自动爬取 + 翻译 + 转换 + 发布 编排器

工作流程：
  1. 从 Minecraft 官方 API 获取最新新闻
  2. 按 news_types 配置过滤类型（Java 正式版/快照/预发布/RC、基岩版正式版/测试版）
  3. 检查已发布状态，跳过已处理的新闻
  4. 逐篇处理：解析 → 翻译 → 保存 JSON → 转换 BBCode/Markdown → 发布到 MCBBS

用法：
  python main.py                    # 自动运行全流程
  python main.py --dry-run          # 仅检测，不翻译也不发布
  python main.py --scrape-only      # 只爬取+翻译+转换，不发布
  python main.py --post-only        # 只发布 output 目录中未发布的文件

配置：
  统一使用 config.json（同目录下）
  环境变量覆盖：OPENAI_API_KEY, MCBBS_USERNAME, MCBBS_PASSWORD 等
"""

import os
import sys
import json
import time
import argparse

# 项目根目录
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# 自动加载 .env 文件（本地开发用，不影响 GitHub Actions）
_env_path = os.path.join(PROJECT_DIR, ".env")
if os.path.exists(_env_path):
    with open(_env_path, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())


def load_main_config() -> dict:
    """加载统一配置"""
    config_path = os.path.join(PROJECT_DIR, "config.json")
    from scraper import load_config
    return load_config(config_path)


def classify_news_type(title: str) -> str:
    """根据标题判断新闻类型"""
    from scraper import classify_news_type
    return classify_news_type(title)


def filter_news_by_types(news_list: list, config: dict) -> list:
    """按配置的 news_types 过滤新闻"""
    news_types = config.get("news_types", {})
    # 如果没有配置 news_types 或全部为 true，不过滤
    if not news_types or all(news_types.values()):
        return news_list

    filtered = []
    for news in news_list:
        ntype = classify_news_type(news['title'])
        # "other" 类型不受过滤控制（始终保留或跳过取决于配置）
        if ntype == "other":
            if news_types.get("other", True):
                filtered.append(news)
            continue
        if news_types.get(ntype, True):
            filtered.append(news)

    print(f"[过滤] {len(filtered)}/{len(news_list)} 条通过类型过滤")
    return filtered


def load_state(state_file: str) -> dict:
    """加载处理状态"""
    if os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"posted_urls": [], "last_run": None}


def save_state(state_file: str, state: dict):
    """保存处理状态"""
    state["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    # 确保目录存在
    os.makedirs(os.path.dirname(state_file) or ".", exist_ok=True)
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def run_scrape(config: dict, state_file: str, dry_run: bool = False) -> list:
    """
    执行爬取流程：获取新闻（API + Feedback） → 过滤类型 → 检查状态 → 翻译 → 保存
    
    Returns:
        已处理的文章 (stem, txt_path, json_path) 列表
    """
    from scraper import (
        get_latest_news_list, process_article, save_article_json,
        FeedbackScraper, process_feedback_news
    )
    from converter import convert_json_file

    save_dir = config["output"]["save_dir"]
    os.makedirs(save_dir, exist_ok=True)

    all_news = []

    # ── 1. Minecraft 官方 API 新闻 ──
    page_size = config.get("minecraft_api", {}).get("pageSize", 10)
    api_news = get_latest_news_list(page_size=page_size)
    for news in api_news:
        news['_source'] = 'minecraft_api'
    all_news.extend(api_news)
    print(f"[主] API 新闻: {len(api_news)} 条")

    # ── 2. Feedback 网站新闻 ──
    feedback_config = config.get('feedback_site', {})
    if feedback_config.get('enabled', False):
        try:
            scraper = FeedbackScraper(config)
            feedback_sections = scraper.get_latest_articles()
            for section_name, section_data in feedback_sections.items():
                for article in section_data['articles']:
                    article['_source'] = 'feedback'
                    article['section'] = section_name
                    article['section_cn'] = section_data['name_cn']
                    # Feedback 文章 URL 需要补全
                    if article['url'].startswith('/'):
                        article['url'] = feedback_config.get('base_url', 'https://feedback.minecraft.net') + article['url']
                    # 用 section_cn + title 作为 display title
                    if not article.get('release_date'):
                        article['release_date'] = ''
                    all_news.append(article)
            print(f"[主] Feedback 新闻: {sum(len(v['articles']) for v in feedback_sections.values())} 条")
        except ImportError as e:
            print(f"[主] Feedback 不可用: {e}")
        except Exception as e:
            print(f"[主] Feedback 获取失败: {e}")

    if not all_news:
        print("[主] 未获取到任何新闻")
        return []

    # 按类型过滤（仅对 API 新闻生效，Feedback 不过滤）
    api_items = [n for n in all_news if n.get('_source') == 'minecraft_api']
    feedback_items = [n for n in all_news if n.get('_source') == 'feedback']
    filtered_api = filter_news_by_types(api_items, config)
    all_news = filtered_api + feedback_items

    # 检查已处理状态
    state = load_state(state_file)
    posted_urls = set(state.get("posted_urls", []))
    new_news = [n for n in all_news if n['url'] not in posted_urls]
    if not new_news:
        print(f"[主] 没有新新闻（共 {len(all_news)} 条，已全部处理过）")
        return []

    print(f"[主] 发现 {len(new_news)} 条新新闻待处理")

    if dry_run:
        print("\n[Dry Run] 新新闻列表：")
        for i, news in enumerate(new_news, 1):
            source = news.get('_source', 'minecraft_api')
            ntype = classify_news_type(news['title']) if source == 'minecraft_api' else 'feedback'
            print(f"  {i}. [{source}][{ntype}] {news['title']}")
            print(f"     {news['url']}")
        return []

    # 逐篇处理
    processed = []
    for i, news in enumerate(new_news, 1):
        source = news.get('_source', 'minecraft_api')
        print(f"\n{'=' * 60}")
        print(f"[主] 处理第 {i}/{len(new_news)} 条 [{source}]")
        print(f"{'=' * 60}")

        try:
            # 根据来源选择处理方式
            if source == 'feedback':
                full_data = process_feedback_news(news, config)
            else:
                full_data = process_article(news)
            if not full_data:
                print("[主] 文章处理失败，跳过")
                continue

            # 保存 JSON
            json_path = save_article_json(full_data, save_dir=save_dir)
            if not json_path:
                print("[主] JSON 保存失败，跳过")
                continue

            # 转换为 BBCode 和 Markdown
            base_path = json_path.rsplit(".", 1)[0]
            try:
                modules_cfg_path = os.path.join(PROJECT_DIR, "modules_config.json")
                modules_cfg = json.load(open(modules_cfg_path, encoding="utf-8")) if os.path.exists(modules_cfg_path) else None
                bbcode_path, md_path = convert_json_file(json_path, output_prefix=base_path, modules_config=modules_cfg)
            except Exception as e:
                print(f"[主] 转换失败: {e}")
                # 即使转换失败，JSON 已保存，继续处理
                bbcode_path = None

            stem = os.path.basename(base_path)

            # 标记为已处理（即使发布失败，也不重复爬取翻译）
            posted_urls.add(news['url'])
            state["posted_urls"] = list(posted_urls)
            save_state(state_file, state)

            if bbcode_path:
                processed.append((stem, bbcode_path, json_path))

        except Exception as e:
            print(f"[主] 处理异常: {e}")
            import traceback
            traceback.print_exc()

    return processed


def run_post(processed: list, config: dict, no_image: bool = False, no_json: bool = False):
    """执行发布流程"""
    from poster import MCBBSPoster, load_posted, save_posted, load_poster_config
    if not config.get("mcbbs", {}).get("enabled", False):
        print("[主] MCBBS 发布未启用（config.json 中 mcbbs.enabled = false）")
        return

    if not processed:
        print("[主] 没有需要发布的文章")
        return

    mcbbs_config = load_poster_config()

    poster = MCBBSPoster(mcbbs_config)
    try:
        poster.login()
    except Exception as e:
        print(f"\n[主] MCBBS 登录失败: {e}")
        return

    save_dir = config["output"]["save_dir"]
    poster_state_file = os.path.join(save_dir, ".posted.json")
    posted = load_posted(poster_state_file)
    success = 0
    failed = 0

    for stem, txt_path, json_path in processed:
        try:
            print(f"\n[主] 发布: {stem}")
            poster.post_news_file(stem, txt_path, json_path, save_dir,
                                 no_image=no_image, attach_json=not no_json)
            posted.add(stem)
            save_posted(poster_state_file, posted)
            success += 1
            time.sleep(3)  # 发帖间隔，避免被封
        except Exception as e:
            print(f"[主] 发布失败: {e}")
            failed += 1

    print(f"\n[主] 发布完成: 成功 {success}, 失败 {failed}")


def run_post_only(config: dict):
    """仅发布 output 目录中未发布的文件"""
    from poster import MCBBSPoster, load_posted, save_posted, find_image, load_poster_config
    mcbbs_config = load_poster_config()
    if not config.get("mcbbs", {}).get("enabled", False):
        print("[主] MCBBS 发布未启用")
        return

    save_dir = config["output"]["save_dir"]

    import glob
    all_news = []
    for txt_path in sorted(glob.glob(os.path.join(save_dir, "*.txt"))):
        stem = os.path.splitext(os.path.basename(txt_path))[0]
        json_path = os.path.join(save_dir, stem + ".json")
        if os.path.exists(json_path):
            all_news.append((stem, txt_path, json_path))

    if not all_news:
        print("[主] output 目录没有新闻文件")
        return

    # 使用 poster 的状态文件
    poster_state_file = os.path.join(save_dir, ".posted.json")
    posted = load_posted(poster_state_file)
    pending = [(s, t, j) for s, t, j in all_news if s not in posted]

    if not pending:
        print(f"[主] 所有 {len(all_news)} 个文件已发布")
        return

    print(f"[主] 待发布: {len(pending)} 个")
    poster = MCBBSPoster(mcbbs_config)
    try:
        poster.login()
    except Exception as e:
        print(f"\n[主] MCBBS 登录失败: {e}")
        return

    for stem, txt_path, json_path in pending:
        try:
            print(f"\n[主] 发布: {stem}")
            poster.post_news_file(stem, txt_path, json_path, save_dir)
            posted.add(stem)
            save_posted(poster_state_file, posted)
            time.sleep(3)
        except Exception as e:
            print(f"[主] 发布失败: {e}")


# ── 入口 ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="MCTTK — Minecraft 新闻自动爬取+翻译+发布",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python main.py                    # 全流程自动运行\n"
            "  python main.py --dry-run          # 仅检测新新闻\n"
            "  python main.py --scrape-only      # 只爬取翻译，不发布\n"
            "  python main.py --post-only        # 只发布已翻译的文件\n"
        )
    )
    parser.add_argument("--dry-run", action="store_true", help="仅检测新新闻，不实际处理")
    parser.add_argument("--scrape-only", action="store_true", help="只爬取+翻译+转换，不发布到 MCBBS")
    parser.add_argument("--post-only", action="store_true", help="只发布 output 目录中未发布的文件")
    parser.add_argument("--no-image", action="store_true", help="发布时跳过图片上传")
    parser.add_argument("--no-json", action="store_true", help="发布时跳过 JSON 附件上传")
    parser.add_argument("--config", help="指定配置文件路径")
    args = parser.parse_args()

    # 设置输出编码
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)

    print("=" * 60)
    print("  MCTTK — Minecraft 新闻自动爬取 + 翻译 + 发布")
    print("=" * 60)

    # 加载配置
    if args.config:
        from scraper import load_config
        config = load_config(args.config)
    else:
        config = load_main_config()

    save_dir = config["output"]["save_dir"]
    state_file = os.path.join(save_dir, ".state.json")

    # 检查必要的 API 配置（非 post-only 模式需要）
    if not args.post_only:
        api_key = config.get("openai_compat", {}).get("api_key", "")
        host = config.get("openai_compat", {}).get("host", "")
        if not api_key or "example" in host:
            print("\n[!] 请先在 config.json 中配置 openai_compat 部分")
            print("    至少需要: host, api_key, model")
            sys.exit(1)

    if args.post_only:
        # 仅发布模式
        run_post_only(config)
    elif args.dry_run:
        # 预览模式
        print("\n[模式] Dry Run — 仅检测新新闻\n")
        processed = run_scrape(config, state_file, dry_run=True)
    else:
        # 全流程 或 scrape-only
        print(f"\n[配置] 新闻目录: {save_dir}")
        news_types = config.get("news_types", {})
        enabled_types = [k for k, v in news_types.items() if v]
        print(f"[配置] 启用类型: {', '.join(enabled_types) if enabled_types else '全部'}")
        mcbbs_enabled = config.get("mcbbs", {}).get("enabled", False)
        print(f"[配置] MCBBS 发布: {'启用' if mcbbs_enabled else '禁用'}")
        print()

        # 爬取
        processed = run_scrape(config, state_file)

        # 发布
        if processed and not args.scrape_only and mcbbs_enabled:
            print(f"\n{'=' * 60}")
            print("  开始发布到 MCBBS")
            print(f"{'=' * 60}")
            run_post(processed, config, no_image=args.no_image, no_json=args.no_json)
        elif processed and args.scrape_only:
            print(f"\n[主] Scrape-only 模式，跳过发布")
        elif not processed:
            print(f"\n[主] 没有新内容需要处理")

    print(f"\n{'=' * 60}")
    print("  完成！")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
