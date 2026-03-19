#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
init_state.py — 初始化 output/.state.json 和 output/.posted.json
用途：测试前将除一条外的所有新闻标记为"已处理"，避免全量翻译

用法：
  python init_state.py
"""

import os
import sys
import json
import time

# 自动加载 .env
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

from scraper import load_config, get_latest_news_list, FeedbackScraper, classify_news_type

CONFIG_PATH = os.path.join(PROJECT_DIR, "config.json")
config = load_config(CONFIG_PATH)
save_dir = config["output"]["save_dir"]
os.makedirs(save_dir, exist_ok=True)

all_news = []

# ── 1. Minecraft 官方 API ──
print("正在获取 Minecraft 官方 API 新闻...")
api_news = get_latest_news_list(page_size=config.get("minecraft_api", {}).get("pageSize", 10))
for n in api_news:
    n["_source"] = "minecraft_api"
all_news.extend(api_news)
print(f"  API: {len(api_news)} 条")

# ── 2. Feedback ──
feedback_config = config.get("feedback_site", {})
if feedback_config.get("enabled", False):
    print("正在获取 Feedback 新闻...")
    try:
        scraper = FeedbackScraper(config)
        sections = scraper.get_latest_articles()
        for section_name, section_data in sections.items():
            for article in section_data["articles"]:
                article["_source"] = "feedback"
                article["section"] = section_name
                if article["url"].startswith("/"):
                    article["url"] = feedback_config.get("base_url", "https://feedback.minecraft.net") + article["url"]
                all_news.append(article)
        fb_count = sum(len(v["articles"]) for v in sections.values())
        print(f"  Feedback: {fb_count} 条")
    except Exception as e:
        print(f"  Feedback 获取失败（跳过）: {e}")

if not all_news:
    print("未获取到任何新闻，退出")
    sys.exit(1)

# ── 列出所有新闻 ──
print(f"\n共 {len(all_news)} 条新闻：")
for i, n in enumerate(all_news):
    source = n.get("_source", "?")
    ntype = classify_news_type(n["title"]) if source == "minecraft_api" else "feedback"
    print(f"  {i+1:2d}. [{source}][{ntype}] {n['title']}")
    print(f"       {n['url']}")

# ── 选择保留的新闻 ──
print()
choice = input("请输入要保留（不标记为已处理）的新闻编号（1 开始），直接回车默认保留第 1 条: ").strip()
if not choice:
    keep_idx = 0
else:
    try:
        keep_idx = int(choice) - 1
        if not (0 <= keep_idx < len(all_news)):
            print(f"编号超出范围，默认保留第 1 条")
            keep_idx = 0
    except ValueError:
        print("输入无效，默认保留第 1 条")
        keep_idx = 0

kept = all_news[keep_idx]
to_mark = [n for i, n in enumerate(all_news) if i != keep_idx]

print(f"\n保留：{kept['title']}")
print(f"标记为已处理：{len(to_mark)} 条")

# ── 写入 .state.json ──
state_file = os.path.join(save_dir, ".state.json")
state = {
    "posted_urls": [n["url"] for n in to_mark],
    "last_run": time.strftime("%Y-%m-%dT%H:%M:%S")
}
with open(state_file, "w", encoding="utf-8") as f:
    json.dump(state, f, ensure_ascii=False, indent=2)
print(f"\n已写入 {state_file}（{len(to_mark)} 条 URL）")

# ── 写入 .posted.json ──
# .posted.json 存的是已发布的文件 stem，此处无实际文件，留空即可
# 只需确保文件存在（空列表），poster 会正常工作
posted_file = os.path.join(save_dir, ".posted.json")
if not os.path.exists(posted_file):
    with open(posted_file, "w", encoding="utf-8") as f:
        json.dump([], f)
    print(f"已创建 {posted_file}（空列表）")
else:
    print(f"{posted_file} 已存在，未修改")

print("\n完成。现在运行 python main.py 将只处理保留的那条新闻。")
