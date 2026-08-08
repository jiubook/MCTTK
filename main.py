#!/usr/bin/env python3
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

import argparse
import json
import logging
import os
import sys
import time
import traceback

from converter import convert_json_file
from log_setup import log_info, setup_logging
from scraper import (
    FeedbackScraper,
    classify_news_type,
    download_header_image,
    get_latest_news_list,
    load_config,
    process_article,
    process_feedback_news,
    save_article_json,
)
from utils import load_dotenv

# 项目根目录
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# 项目根目录
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

load_dotenv(PROJECT_DIR)


def load_main_config() -> dict:
    """加载统一配置"""
    config_path = os.path.join(PROJECT_DIR, "config.json")
    return load_config(config_path)


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
            with open(state_file, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            logging.warning("状态文件 %s 读取失败，将重置为初始状态", state_file, exc_info=True)
    return {"posted_urls": [], "last_run": None, "_first_run": True}


def save_state(state_file: str, state: dict):
    """保存处理状态"""
    state["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    # 确保目录存在
    os.makedirs(os.path.dirname(state_file) or ".", exist_ok=True)
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _fetch_all_news(config: dict) -> list:
    """获取所有来源的新闻（API + Feedback），合并返回"""
    all_news = []

    page_size = config.get("minecraft_api", {}).get("pageSize", 10)
    api_news = get_latest_news_list(page_size=page_size, config=config)
    for news in api_news:
        news['_source'] = 'minecraft_api'
    all_news.extend(api_news)
    print(f"[主] API 新闻: {len(api_news)} 条")

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
                    if article['url'].startswith('/'):
                        base = feedback_config.get('base_url', 'https://feedback.minecraft.net')
                        article['url'] = base + article['url']
                    if not article.get('release_date'):
                        article['release_date'] = ''
                    all_news.append(article)
            print(f"[主] Feedback 新闻: {sum(len(v['articles']) for v in feedback_sections.values())} 条")
        except ImportError as e:
            print(f"[主] Feedback 不可用: {e}")
        except Exception as e:
            print(f"[主] Feedback 获取失败: {e}")

    return all_news


def _filter_and_check_state(all_news: list, config: dict, state_file: str):
    """
    类型过滤 + 加载状态 + 首次运行检测 + 过滤已处理。

    Returns:
        (new_news, state, posted_urls) 或 None（首次运行/无新内容时）
    """
    api_items = [n for n in all_news if n.get('_source') == 'minecraft_api']
    feedback_items = [n for n in all_news if n.get('_source') == 'feedback']
    filtered_api = filter_news_by_types(api_items, config)
    filtered = filtered_api + feedback_items

    state = load_state(state_file)
    posted_urls = set(state.get("posted_urls", []))

    if state.get("_first_run", False):
        print(f"[主] 检测到首次运行，将当前 {len(filtered)} 条新闻标记为已处理")
        posted_urls.update(n['url'] for n in filtered)
        state["posted_urls"] = list(posted_urls)
        state.pop("_first_run", None)
        save_state(state_file, state)

        # 同时将 output 目录中已有的文件标记为已发布，避免重复发布
        save_dir = config["output"]["save_dir"]
        poster_state_file = os.path.join(save_dir, ".posted.json")
        if os.path.exists(save_dir):
            import glob as _glob

            from poster import load_posted, save_posted
            existing_posted = load_posted(poster_state_file)
            existing_stems = {
                os.path.splitext(os.path.basename(p))[0]
                for p in _glob.glob(os.path.join(save_dir, "*.txt"))
            }
            if existing_stems:
                existing_posted.update(existing_stems)
                save_posted(poster_state_file, existing_posted)
                print(f"[主] 已将 output 目录中 {len(existing_stems)} 个文件标记为已发布")

        return None

    new_news = [n for n in filtered if n['url'] not in posted_urls]
    return new_news, state, posted_urls


def _process_single_article(news: dict, config: dict, save_dir: str, modules_cfg) -> tuple | None:
    """
    处理单篇文章：解析 → 翻译 → 保存 JSON → 下载头图 → 转换。

    Returns:
        (stem, bbcode_path, json_path) 或 None
    """
    source = news.get('_source', 'minecraft_api')
    full_data = process_feedback_news(news, config) if source == 'feedback' else process_article(news, config=config)
    if not full_data:
        print("[主] 文章处理失败，跳过")
        return None

    json_path = save_article_json(full_data, save_dir=save_dir, config=config)
    if not json_path:
        print("[主] JSON 保存失败，跳过")
        return None

    # 下载头图（与 JSON 同名）
    header_image_url = full_data.get("header_image_url", "")
    if header_image_url:
        image_ext = ".jpg"
        try:
            url_path = header_image_url.split("?")[0]
            if "." in url_path:
                ext = url_path.rsplit(".", 1)[-1].lower()
                if ext in ["jpg", "jpeg", "png", "gif", "webp"]:
                    image_ext = f".{ext}"
        except Exception:  # noqa: BLE001
            logging.debug("图片扩展名解析失败，使用默认 .jpg", exc_info=True)
        base_path = json_path.rsplit(".", 1)[0]
        download_header_image(header_image_url, base_path + image_ext, config=config)

    base_path = json_path.rsplit(".", 1)[0]
    try:
        bbcode_path, _ = convert_json_file(json_path, output_prefix=base_path, modules_config=modules_cfg)
    except Exception as e:
        print(f"[主] 转换失败: {e}")
        return None

    stem = os.path.basename(base_path)
    return stem, bbcode_path, json_path


def run_scrape(config: dict, state_file: str, dry_run: bool = False) -> list:
    """
    执行爬取流程：获取新闻 → 过滤类型 → 检查状态 → 翻译 → 保存

    Returns:
        已处理的文章 (stem, txt_path, json_path) 列表
    """
    save_dir = config["output"]["save_dir"]
    os.makedirs(save_dir, exist_ok=True)

    all_news = _fetch_all_news(config)
    if not all_news:
        print("[主] 未获取到任何新闻")
        return []

    result = _filter_and_check_state(all_news, config, state_file)
    if result is None:
        return []
    new_news, state, posted_urls = result

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

    modules_cfg_path = os.path.join(PROJECT_DIR, "modules_config.json")
    modules_cfg = None
    if os.path.exists(modules_cfg_path):
        with open(modules_cfg_path, encoding="utf-8") as f:
            modules_cfg = json.load(f)

    processed = []
    for i, news in enumerate(new_news, 1):
        source = news.get('_source', 'minecraft_api')
        print(f"\n{'=' * 60}")
        print(f"[主] 处理第 {i}/{len(new_news)} 条 [{source}]")
        print(f"{'=' * 60}")

        try:
            item = _process_single_article(news, config, save_dir, modules_cfg)
            posted_urls.add(news['url'])
            state["posted_urls"] = list(posted_urls)
            save_state(state_file, state)
            if item:
                processed.append(item)
        except Exception as e:
            print(f"[主] 处理异常: {e}")
            traceback.print_exc()

    return processed


def run_post(processed: list, config: dict, no_image: bool = False, no_json: bool = False):
    """执行发布流程"""
    from poster import MCBBSPoster, load_posted, load_poster_config, save_posted
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

    # 处理上次运行中进入审核的待高亮帖子
    try:
        poster.process_pending_highlights(save_dir)
    except Exception as e:
        print(f"[主] 处理待高亮帖子时出错: {e}")

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
    from poster import MCBBSPoster, load_posted, load_poster_config, save_posted
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

    # 处理待高亮帖子
    try:
        poster.process_pending_highlights(save_dir)
    except Exception as e:
        print(f"[主] 处理待高亮帖子时出错: {e}")

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

    # 初始化日志系统
    setup_logging(log_level=logging.DEBUG)

    print("=" * 60)
    print("  MCTTK — Minecraft 新闻自动爬取 + 翻译 + 发布")
    print("=" * 60)

    log_info("程序启动")

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
            print("\n[主] Scrape-only 模式，跳过发布")
        elif not processed:
            print("\n[主] 没有新内容需要处理")

    print(f"\n{'=' * 60}")
    print("  完成！")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
