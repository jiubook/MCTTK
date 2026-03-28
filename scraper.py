#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scraper.py — Minecraft 新闻爬取与翻译模块

功能：
  - 从 Minecraft 官方 API 获取最新新闻列表
  - 解析文章页面，提取结构化内容
  - 调用 AI API 翻译为简体中文
  - 保存为 JSON（不自动转换 BBCode/Markdown，由 main.py 调用 converter 处理）

配置：统一由 config.json 加载
"""

import requests
import json
import os
import re
import urllib3
import hashlib
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
from bs4 import BeautifulSoup, NavigableString, Tag
from concurrent.futures import ThreadPoolExecutor, as_completed

# 尝试导入 curl_cffi（用于 Feedback 网站爬取，绕过 Cloudflare）
try:
    from curl_cffi import requests as cffi_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False
    cffi_requests = None

# 禁用 SSL 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ── 配置加载 ─────────────────────────────────────────

DEFAULT_CONFIG = {
    "openai_compat": {
        "host": "www.example.com",
        "endpoint": "/v1/chat/completions",
        "api_key_env": "OPENAI_API_KEY",
        "api_key": "",
        "model": "your-model-name",
        "max_tokens": 10000,
        "timeout": 120
    },
    "prompts": {
        "translate_text_default": (
            "你是专业的 Minecraft 游戏翻译。请将下面文本翻译成简体中文。\n"
            "翻译规则：\n"
            "1. 使用 Minecraft 官方中文译名（Java版）。\n"
            "2. 不要直译游戏术语。\n"
            "要求：\n"
            "- 保留版本号/编号（如 MC-12345）、URL、代码片段\n"
            "- 如包含 Markdown 链接 [text](url)，请只翻译 visible text\n"
            "- 仅输出译文，不要解释"
        ),
        "translate_blocks_system": (
            "你是 Minecraft 官方更新日志翻译专家，请把用户提供的 JSON 数组逐条翻译成简体中文。\n"
            "输出要求：\n1. 只输出 JSON 数组\n2. 每项格式：{\"id\":..., \"translated_text\":...}\n"
            "3. 不要输出任何解释\n4. 保留 URL / MC-编号 / 代码\n5. 保留换行"
        ),
        "translate_title_system": (
            "请将 Minecraft 新闻标题翻译成简体中文。要求：保留版本号/编号/专有名词的拼写，只输出译文标题。"
        )
    },
    "minecraft_api": {
        "search_url": "https://net-secondary.web.minecraft-services.net/api/v1.0/zh-cn/search",
        "pageSize": 10,
        "sortType": "Recent",
        "category": "News",
        "site_base": "https://www.minecraft.net"
    },
    "http": {
        "verify_ssl": False,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "proxies": { "http": "", "https": "" },
        "timeout": 120
    },
    "output": { "save_dir": "output" },
    "retry": {
        "translation": { "max_retries": 3, "wait_for_input": False },
        "download":    { "max_retries": 3, "wait_for_input": False }
    },
    "concurrency": {
        "translation_workers": 3,
        "batch_max_chars": 1000,
        "batch_max_items": 10
    }
}


def _deep_merge(a: dict, b: dict) -> dict:
    """深度合并字典 b 到 a"""
    if not b:
        return dict(a)
    result = dict(a)
    for key, value in b.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(config_path: str = None) -> dict:
    """加载统一配置文件"""
    if config_path is None:
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

    config = dict(DEFAULT_CONFIG)
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                user_config = json.load(f)
            config = _deep_merge(config, user_config)
        except (json.JSONDecodeError, IOError) as e:
            print(f"[配置] 加载失败，使用默认配置: {e}")
    else:
        print(f"[配置] 配置文件不存在: {config_path}")

    # 环境变量覆盖 API Key
    env_var = config.get("openai_compat", {}).get("api_key_env", "OPENAI_API_KEY")
    if env_var:
        env_key = os.getenv(env_var)
        if env_key:
            config["openai_compat"]["api_key"] = env_key

    return config


# ── 全局配置 ─────────────────────────────────────────

CFG = load_config()

PROXIES = CFG["http"].get("proxies")
if PROXIES and not any(PROXIES.values()):
    PROXIES = None

HEADERS_HTML = {
    "User-Agent": CFG["http"]["user_agent"],
    "Accept": CFG["http"]["accept"]
}

# ── 词汇表加载 ─────────────────────────────────────────

def load_glossary(glossary_path: str = None) -> dict:
    """加载专业术语词汇表"""
    if glossary_path is None:
        glossary_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "glossary.json")

    if not os.path.exists(glossary_path):
        return {}

    try:
        with open(glossary_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("terms", {})
    except (json.JSONDecodeError, IOError) as e:
        print(f"[词汇表] 加载失败: {e}")
        return {}

GLOSSARY = load_glossary()


def find_relevant_terms(text: str, glossary: dict) -> dict:
    """
    从文本中查找相关的专业术语

    Args:
        text: 待翻译的文本
        glossary: 词汇表字典 {英文: 中文}

    Returns:
        相关术语的字典 {英文: 中文}
    """
    if not text or not glossary:
        return {}

    relevant = {}
    text_lower = text.lower()

    for en_term, zh_term in glossary.items():
        # 不区分大小写查找
        if en_term.lower() in text_lower:
            relevant[en_term] = zh_term

    return relevant


def build_glossary_prompt(relevant_terms: dict) -> str:
    """
    根据相关术语构建提示词片段

    Args:
        relevant_terms: 相关术语字典 {英文: 中文}

    Returns:
        提示词字符串
    """
    if not relevant_terms:
        return ""

    terms_list = [f"  - {en} → {zh}" for en, zh in relevant_terms.items()]
    prompt = "\n专业术语对照（请严格使用以下译名）：\n" + "\n".join(terms_list)
    return prompt


# ── 工具函数 ─────────────────────────────────────────

def _normalize_whitespace(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\u00A0", "<<<NBSP>>>")
    s = re.sub(r"\s+", " ", s).strip()
    s = s.replace("<<<NBSP>>>", " ")
    return s


def _extract_text_preserve_links(tag: Tag, base_url: str = "", stop_at_lists: bool = False) -> str:
    parts = []

    def walk(node):
        if isinstance(node, NavigableString):
            text = str(node)
            if text:
                parts.append(text)
            return
        if not isinstance(node, Tag):
            return
        tag_name = (node.name or "").lower()
        if stop_at_lists and tag_name in ("ul", "ol"):
            return
        if tag_name == "br":
            parts.append("\n")
            return
        if tag_name == "a":
            href = node.get("href", "").strip()
            href = urljoin(base_url, href) if base_url else href
            visible_text = _normalize_whitespace(node.get_text(" ", strip=True))
            if href and visible_text:
                parts.append(f"[{visible_text}]({href})")
            elif href:
                parts.append(f"<{href}>")
            elif visible_text:
                parts.append(visible_text)
            return
        if tag_name in ("code", "kbd", "samp"):
            code_text = _normalize_whitespace(node.get_text(" ", strip=True))
            if code_text:
                parts.append(f"\u00A0`{code_text}`\u00A0")
            return
        for child in node.children:
            walk(child)
        if tag_name in ("p", "li", "blockquote"):
            parts.append("\n")

    walk(tag)
    lines = [_normalize_whitespace(line) for line in "".join(parts).split("\n")]
    result = "\n".join(lines)
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    return result


def extract_blocks_in_order(container: Tag, blocks: list, base_url: str = ""):
    """从 HTML 容器中按顺序提取结构化内容块"""
    if not container:
        return

    def add_text_block(block_type: str, source_text: str, meta=None):
        source_text = (source_text or "").strip()
        if not source_text:
            return
        lines = [line.strip() for line in source_text.split("\n") if line.strip()]
        for line in lines:
            if not line:
                continue
            blocks.append({
                "id": f"b{len(blocks)+1:04d}",
                "type": block_type,
                "source_text": line,
                "translated_text": "",
                "meta": meta or {}
            })

    def add_code_block(source_text: str, meta=None):
        source_text = (source_text or "").strip()
        if not source_text:
            return
        blocks.append({
            "id": f"b{len(blocks)+1:04d}",
            "type": "pre",
            "source_text": source_text,
            "translated_text": "",
            "meta": meta or {}
        })

    def add_img_block(src: str, alt: str = "", meta=None):
        src = (src or "").strip()
        if not src:
            return
        src = urljoin(base_url, src) if base_url else src
        img_meta = {"src": src, "alt": alt or ""}
        if meta:
            img_meta.update(meta)
        blocks.append({
            "id": f"b{len(blocks)+1:04d}",
            "type": "img",
            "source_text": "",
            "translated_text": "",
            "meta": img_meta
        })

    def walk(node):
        if isinstance(node, NavigableString):
            text = _normalize_whitespace(str(node))
            if text:
                add_text_block("text", text)
            return
        if not isinstance(node, Tag):
            return
        tag_name = (node.name or "").lower()
        if tag_name == "pre":
            code_text = node.get_text("\n", strip=True).strip()
            if code_text:
                add_code_block(code_text.replace('\t', '  '))
            return
        if tag_name == "img":
            add_img_block(node.get("src"), node.get("alt", ""))
            return
        if tag_name in ("ul", "ol"):
            def process_list(list_node, indent_level=0):
                for li in list_node.find_all("li", recursive=False):
                    li_text_parts = []
                    for child in li.children:
                        if isinstance(child, NavigableString):
                            text = _normalize_whitespace(str(child))
                            if text:
                                li_text_parts.append(text)
                        elif isinstance(child, Tag):
                            if (child.name or "").lower() not in ("ul", "ol"):
                                text = _extract_text_preserve_links(child, base_url=base_url, stop_at_lists=True)
                                if text:
                                    li_text_parts.append(text)
                    li_text = " ".join(li_text_parts).strip()
                    if li_text:
                        add_text_block("li", li_text, meta={"indent_level": indent_level})
                    for nested_list in li.find_all(["ul", "ol"], recursive=False):
                        process_list(nested_list, indent_level + 1)
            process_list(node, indent_level=0)
            return
        if tag_name in ("h1", "h2", "h3", "h4"):
            heading_text = _extract_text_preserve_links(node, base_url=base_url)
            heading_text = re.sub(r'<https?://[^>]+>$', '', heading_text).strip()
            add_text_block(tag_name, heading_text)
            return
        if tag_name == "p":
            para_text = _extract_text_preserve_links(node, base_url=base_url)
            add_text_block(tag_name, para_text)
            return
        if tag_name == "blockquote":
            para_text = _extract_text_preserve_links(node, base_url=base_url)
            add_text_block(tag_name, para_text)
            return
        for child in node.children:
            walk(child)

    for child in container.children:
        walk(child)


# ── Feedback 网站爬虫 ───────────────────────────────

class FeedbackScraper:
    """
    Minecraft Feedback 网站爬虫类
    使用 curl_cffi 模拟真实浏览器，绕过 Cloudflare Bot Management 防护
    """

    def __init__(self, config):
        if not CURL_CFFI_AVAILABLE:
            raise ImportError(
                "curl_cffi 未安装。Feedback 爬虫需要 curl_cffi 来绕过 Cloudflare 防护。\n"
                "请运行: pip install curl_cffi"
            )
        self.config = config
        self.feedback_config = config.get('feedback_site', {})
        self.base_url = self.feedback_config.get('base_url', 'https://feedback.minecraft.net')
        self.timeout = self.feedback_config.get('timeout', 30)
        self.headers = {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-User': '?1',
            'Sec-Fetch-Dest': 'document',
        }
        self.session = cffi_requests.Session()

    def fetch_page(self, url, referer=None):
        try:
            full_url = urljoin(self.base_url, url)
            headers = self.headers.copy()
            if referer:
                headers['Referer'] = referer
            response = self.session.get(
                full_url, headers=headers,
                timeout=self.timeout, impersonate="chrome"
            )
            response.raise_for_status()
            response.encoding = 'utf-8'
            return BeautifulSoup(response.text, 'html.parser')
        except Exception as e:
            print(f"[Feedback] 获取页面失败 {full_url}: {e}")
            return None

    def parse_knowledge_base(self, soup):
        sections_data = {}
        for section in soup.find_all('section', class_='section category-section'):
            title_link = section.find('h3', class_='section-tree-title')
            if not title_link:
                continue
            section_link = title_link.find('a', class_='section-tree-title-link')
            if not section_link:
                continue
            section_name = section_link.get_text(strip=True).replace(' →', '')
            section_url = section_link.get('href', '')
            articles = []
            article_list = section.find('ul', class_='article-list')
            if article_list:
                for li in article_list.find_all('li', class_='article-list-item'):
                    article_link = li.find('a', class_='article-list-link')
                    if article_link:
                        articles.append({
                            'title': article_link.get_text(strip=True),
                            'url': article_link.get('href', '')
                        })
            sections_data[section_name] = {
                'section_url': section_url,
                'articles': articles
            }
        return sections_data

    def parse_article(self, soup):
        result = {'title': '', 'content': '', 'posted_date': ''}
        title_elem = soup.find('h1', class_='article-title')
        if title_elem:
            result['title'] = title_elem.get_text(strip=True)
        article_body = soup.find('div', class_='article-body')
        if article_body:
            posted_elem = article_body.find('strong', string='Posted:')
            if posted_elem and posted_elem.parent:
                result['posted_date'] = posted_elem.parent.get_text(strip=True).replace('Posted:', '').strip()
            result['content'] = str(article_body)
        return result

    def get_latest_articles(self, limit_per_section=6):
        knowledge_base_url = self.feedback_config.get('knowledge_base_url')
        if not knowledge_base_url:
            print("[Feedback] 未配置 knowledge_base_url")
            return {}
        print(f"[Feedback] 获取 Knowledge Base: {knowledge_base_url}")
        soup = self.fetch_page(knowledge_base_url)
        if not soup:
            return {}
        sections_data = self.parse_knowledge_base(soup)
        configured_sections = self.feedback_config.get('sections', [])
        result = {}
        for section_config in configured_sections:
            if not section_config.get('enabled', True):
                continue
            section_name = section_config['name']
            section_name_cn = section_config.get('name_cn', section_name)
            articles_count = section_config.get('articles_count', limit_per_section)
            if section_name in sections_data:
                section_info = sections_data[section_name]
                articles = section_info['articles'][:articles_count]
                result[section_name] = {
                    'name_cn': section_name_cn,
                    'section_url': section_info['section_url'],
                    'articles': articles
                }
        return result

    def fetch_article_content(self, article_url):
        print(f"[Feedback] 获取文章: {article_url}")
        referer = self.feedback_config.get('knowledge_base_url', self.base_url)
        soup = self.fetch_page(article_url, referer=referer)
        if not soup:
            return None
        article_data = self.parse_article(soup)
        article_data['url'] = article_url
        return article_data


def convert_feedback_html_to_blocks(html_content, base_url=""):
    """将 Feedback 文章的 HTML 内容转换为结构化 blocks"""
    soup = BeautifulSoup(html_content, 'html.parser')
    article_body = soup.find('div', class_='article-body')
    if not article_body:
        article_body = soup
    blocks = []
    extract_blocks_in_order(article_body, blocks, base_url=base_url)
    return blocks


def process_feedback_news(news_item: dict, config: dict) -> dict:
    """完整处理单篇 Feedback 文章"""
    import sys
    scraper = FeedbackScraper(config)
    article_content = scraper.fetch_article_content(news_item['url'])
    if not article_content:
        print("[Feedback] 无法获取文章内容")
        return None

    title_to_translate = article_content['title']
    translated_title = translate_text(
        title_to_translate,
        system_prompt=config["prompts"]["translate_title_system"]
    ) or ""
    if translated_title:
        print(f"  原标题: {title_to_translate}")
        print(f"  译标题: {translated_title}")

    feedback_base_url = config.get('feedback_site', {}).get('base_url', 'https://feedback.minecraft.net')
    full_url = urljoin(feedback_base_url, news_item['url'])
    blocks = convert_feedback_html_to_blocks(article_content['content'], base_url=full_url)
    translate_blocks(blocks)

    return {
        "title": title_to_translate,
        "translated_title": translated_title,
        "release_date": article_content.get("posted_date", ""),
        "url": full_url,
        "section": news_item.get('section', ''),
        "section_cn": news_item.get('section_cn', ''),
        "blocks": blocks,
        "content": blocks_to_plaintext(blocks, field="source_text"),
        "translated_content": blocks_to_plaintext(blocks, field="translated_text"),
        "source": "feedback"
    }


# ── 翻译 ─────────────────────────────────────────────

def translate_text(text, system_prompt=None, use_glossary=True):
    """
    调用 OpenAI 兼容 API 翻译文本（支持自动重试）

    Args:
        text: 待翻译的文本
        system_prompt: 系统提示词（可选）
        use_glossary: 是否使用词汇表动态添加术语对照（默认 True）
    """
    system_prompt = system_prompt or CFG["prompts"]["translate_text_default"]

    # 动态添加相关术语到提示词
    if use_glossary and GLOSSARY:
        relevant_terms = find_relevant_terms(text, GLOSSARY)
        if relevant_terms:
            glossary_prompt = build_glossary_prompt(relevant_terms)
            system_prompt = system_prompt + glossary_prompt
            print(f"[词汇表] 添加 {len(relevant_terms)} 个相关术语到提示词")

    host = CFG["openai_compat"]["host"]
    endpoint = CFG["openai_compat"]["endpoint"]
    api_key = CFG["openai_compat"]["api_key"]
    model = CFG["openai_compat"]["model"]
    max_tokens = int(CFG["openai_compat"].get("max_tokens", 10000))
    timeout = int(CFG["openai_compat"].get("timeout", 120))
    verify_ssl = CFG["http"]["verify_ssl"]
    max_retries = int(CFG.get("retry", {}).get("translation", {}).get("max_retries", 3))

    if not api_key or "********" in api_key:
        print("[翻译] 错误: 未配置 API Key")
        return None
    if not host or "example" in host:
        print("[翻译] 错误: 未配置 API Host")
        return None
    if not model or "your-" in model:
        print("[翻译] 错误: 未配置 Model")
        return None

    api_url = f"https://{host}{endpoint}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text}
        ],
        "max_tokens": max_tokens
    }

    retry_count = 0
    while retry_count <= max_retries:
        try:
            if retry_count > 0:
                print(f"[翻译] 第 {retry_count} 次重试...")
            response = requests.post(
                api_url, json=payload, headers=headers,
                timeout=timeout, verify=verify_ssl, proxies=PROXIES
            )
            response.raise_for_status()
            result = response.json()
            content = result["choices"][0]["message"]["content"]
            return content
        except requests.exceptions.Timeout:
            print(f"[翻译] 请求超时（{timeout}秒）")
        except requests.exceptions.ConnectionError as e:
            print(f"[翻译] 连接失败: {e}")
        except requests.exceptions.HTTPError as e:
            print(f"[翻译] HTTP 错误: {e}")
        except (KeyError, IndexError) as e:
            print(f"[翻译] 响应格式错误: {e}")
            return None
        except json.JSONDecodeError as e:
            print(f"[翻译] JSON 解析失败: {e}")
            return None

        retry_count += 1
        if retry_count <= max_retries:
            time.sleep(retry_count * 2)

    print(f"[翻译] 已重试 {max_retries} 次，跳过")
    return None


# ── 新闻获取 ─────────────────────────────────────────

def get_latest_news_list(page_size=None):
    """通过 Minecraft 官方 API 获取最新新闻列表"""
    api_url = CFG["minecraft_api"]["search_url"]
    params = {
        "pageSize": page_size or CFG["minecraft_api"]["pageSize"],
        "sortType": CFG["minecraft_api"]["sortType"],
        "category": CFG["minecraft_api"]["category"]
    }

    try:
        print(f"[API] 正在获取新闻列表 (pageSize={params['pageSize']})...")
        response = requests.get(
            api_url, params=params, headers=HEADERS_HTML,
            timeout=int(CFG["http"].get("timeout", 120)), verify=CFG["http"]["verify_ssl"], proxies=PROXIES
        )
        response.raise_for_status()
        result = response.json()
        items = result.get("result", {}).get("results", [])

        if not items:
            print("[API] 未返回任何新闻")
            return []

        news_list = []
        site_base = CFG["minecraft_api"]["site_base"]
        for item in items:
            news_url = item.get("url", "")
            if news_url and news_url.startswith("/"):
                news_url = site_base + news_url
            news_list.append({
                "title": item.get("title", ""),
                "author": item.get("author", ""),
                "imageAltText": item.get("imageAltText", ""),
                "description": item.get("description", ""),
                "release_date": item.get("publishDate", ""),
                "url": news_url
            })

        print(f"[API] 获取 {len(news_list)} 条新闻")
        return news_list

    except Exception as e:
        print(f"[API] 获取失败: {e}")
        return []


def classify_news_type(title: str) -> str:
    """根据标题判断新闻类型"""
    t = title.lower()
    # Java 版本（优先级高）
    if "snapshot" in t:
        return "java_snapshot"
    if "pre-release" in t or "prerelease" in t:
        return "java_prerelease"
    if "release candidate" in t:
        return "java_rc"
    # 基岩版本
    if "beta" in t or "preview" in t or "预览" in t:
        return "bedrock_beta"
    if "bedrock" in t or "基岩" in t:
        return "bedrock_release"
    # Java 正式版
    if "java edition" in t or "java版" in t or re.search(r'\b1\.\d+(\.\d+)?\b', t):
        return "java_release"
    return "other"


# ── 文章解析 ─────────────────────────────────────────

def parse_article_page(article_url):
    """解析 Minecraft 新闻文章页面"""
    if not article_url:
        return None

    try:
        print(f"[解析] 获取文章: {article_url}")
        response = requests.get(
            article_url, headers=HEADERS_HTML,
            timeout=int(CFG["http"].get("timeout", 120)), verify=CFG["http"]["verify_ssl"], proxies=PROXIES
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        title_tag = soup.find("h1")
        title = title_tag.get_text(strip=True) if title_tag else ""

        date_tag = soup.find("meta", {"property": "article:published_time"})
        release_date = date_tag["content"] if date_tag else ""

        # 提取头图
        header_image_url = ""
        og_image = soup.find("meta", {"property": "og:image"})
        if og_image and og_image.get("content"):
            header_image_url = og_image["content"]
        if not header_image_url:
            article_head = soup.find("div", class_="article-head")
            if article_head:
                img_tag = article_head.find("img")
                if img_tag and img_tag.get("src"):
                    header_image_url = img_tag["src"]
        if header_image_url and header_image_url.startswith("/"):
            header_image_url = urljoin(article_url, header_image_url)

        # 提取内容块
        blocks = []
        seen_containers = set()

        def _container_signature(tag):
            if not tag:
                return None
            text = tag.get_text("\n", strip=True)
            text = re.sub(r"\s+", " ", text or "").strip()
            if not text:
                return None
            return hashlib.sha1(text.encode("utf-8")).hexdigest()

        candidates = []
        intro = soup.find("div", class_="article-text")
        if intro:
            candidates.append(intro)
        candidates.extend(soup.find_all("div", class_="article-section"))

        # 只提取 MC_AEM_Wrapper 中的 blockquote
        for wrapper in soup.find_all("div", class_="MC_AEM_Wrapper"):
            blockquotes = wrapper.find_all("blockquote")
            for bq in blockquotes:
                candidates.append(bq)

        for container in candidates:
            signature = _container_signature(container)
            if signature and signature in seen_containers:
                continue
            if signature:
                seen_containers.add(signature)
            extract_blocks_in_order(container, blocks, base_url=article_url)

        # 智能去重：去除连续重复 + 检测大段重复
        # 第一步：去除连续重复
        deduplicated = []
        prev_key = None
        for block in blocks:
            key = (
                block.get("type"),
                (block.get("source_text") or "").strip(),
                json.dumps(block.get("meta") or {}, sort_keys=True, ensure_ascii=False),
            )
            if key == prev_key:
                continue
            prev_key = key
            deduplicated.append(block)

        # 第二步：检测并移除大段重复（连续5个以上block重复）
        def find_duplicate_sequences(blocks, min_length=5):
            """查找重复的连续序列"""
            n = len(blocks)
            duplicates = []

            for i in range(n - min_length + 1):
                # 生成当前位置开始的序列签名
                seq_keys = []
                for j in range(i, min(i + 20, n)):  # 最多检查20个连续block
                    key = (
                        blocks[j].get("type"),
                        (blocks[j].get("source_text") or "").strip(),
                        json.dumps(blocks[j].get("meta") or {}, sort_keys=True, ensure_ascii=False),
                    )
                    seq_keys.append(key)

                # 在后续位置查找相同序列
                for k in range(i + min_length, n - min_length + 1):
                    match_length = 0
                    for offset in range(min(len(seq_keys), n - k)):
                        key_k = (
                            blocks[k + offset].get("type"),
                            (blocks[k + offset].get("source_text") or "").strip(),
                            json.dumps(blocks[k + offset].get("meta") or {}, sort_keys=True, ensure_ascii=False),
                        )
                        if seq_keys[offset] == key_k:
                            match_length += 1
                        else:
                            break

                    if match_length >= min_length:
                        duplicates.append((k, k + match_length))

            return duplicates

        # 查找重复序列
        duplicate_ranges = find_duplicate_sequences(deduplicated, min_length=5)

        # 标记要删除的索引
        indices_to_remove = set()
        for start, end in duplicate_ranges:
            for idx in range(start, end):
                indices_to_remove.add(idx)

        # 移除重复序列
        if indices_to_remove:
            print(f"[去重] 检测到大段重复，移除 {len(indices_to_remove)} 个block")
            deduplicated = [block for idx, block in enumerate(deduplicated) if idx not in indices_to_remove]

        # 第三步：去除长文本的非连续重复（可能是容器级重复导致的单个block重复）
        final_blocks = []
        seen_long_texts = {}  # {text_hash: first_index}

        for idx, block in enumerate(deduplicated):
            source_text = (block.get("source_text") or "").strip()
            block_type = block.get("type")

            # 只对长文本（>80字符）且非列表项的block进行去重
            if len(source_text) > 80 and block_type not in ("li",):
                text_key = (
                    block_type,
                    source_text,
                    json.dumps(block.get("meta") or {}, sort_keys=True, ensure_ascii=False),
                )

                if text_key in seen_long_texts:
                    first_idx = seen_long_texts[text_key]
                    # 如果两次出现间隔较近（<15个block），认为是异常重复，跳过
                    if idx - first_idx < 15:
                        print(f"[去重] 跳过长文本重复 (间隔{idx - first_idx}): {source_text[:50]}...")
                        continue
                else:
                    seen_long_texts[text_key] = idx

            final_blocks.append(block)

        deduplicated = final_blocks

        for i, block in enumerate(deduplicated):
            block["id"] = f"b{i+1:04d}"

        print(f"[解析] 提取 {len(deduplicated)} 个内容块")
        return {
            "title": title,
            "release_date": release_date,
            "header_image_url": header_image_url,
            "blocks": deduplicated
        }

    except Exception as e:
        print(f"[解析] 失败: {e}")
        return None


# ── 批量翻译 ─────────────────────────────────────────

def _chunk_items_for_translation(items, max_chars=1000, max_items=10):
    batches = []
    current_batch = []
    current_length = 0
    for item in items:
        item_length = len(json.dumps(item, ensure_ascii=False))
        if current_batch and (len(current_batch) >= max_items or current_length + item_length > max_chars):
            batches.append(current_batch)
            current_batch = []
            current_length = 0
        current_batch.append(item)
        current_length += item_length
    if current_batch:
        batches.append(current_batch)
    return batches


def translate_blocks(blocks: list) -> list:
    """批量翻译内容块"""
    if not blocks:
        return blocks

    for block in blocks:
        if block.get("type") == "pre":
            block["translated_text"] = block.get("source_text", "")

    items_to_translate = []
    block_index_map = {}
    for block_idx, block in enumerate(blocks):
        if block.get("type") in ("img", "pre"):
            continue
        source_text = (block.get("source_text") or "").strip()
        if not source_text:
            continue
        translate_idx = len(items_to_translate)
        items_to_translate.append({"id": f"t{translate_idx:04d}", "text": source_text})
        block_index_map[translate_idx] = block_idx

    if not items_to_translate:
        print("[翻译] 无需翻译")
        return blocks

    print(f"[翻译] 开始翻译 {len(items_to_translate)} 个文本块")
    max_workers = int(CFG.get("concurrency", {}).get("translation_workers", 3))
    batch_max_chars = int(CFG.get("concurrency", {}).get("batch_max_chars", 1000))
    batch_max_items = int(CFG.get("concurrency", {}).get("batch_max_items", 10))
    batches = _chunk_items_for_translation(items_to_translate, max_chars=batch_max_chars, max_items=batch_max_items)
    system_prompt = CFG["prompts"]["translate_blocks_system"]
    translate_idx_to_translation = {}

    def translate_batch(batch_index, batch):
        batch_json = json.dumps(batch, ensure_ascii=False, indent=0)

        # 为当前批次动态添加相关术语
        batch_system_prompt = system_prompt
        if GLOSSARY:
            # 收集批次中所有文本
            batch_texts = " ".join([item.get("text", "") for item in batch])
            relevant_terms = find_relevant_terms(batch_texts, GLOSSARY)
            if relevant_terms:
                glossary_prompt = build_glossary_prompt(relevant_terms)
                batch_system_prompt = system_prompt + glossary_prompt
                print(f"[词汇表] 批次 {batch_index + 1} 添加 {len(relevant_terms)} 个术语")

        translated_result = translate_text(batch_json, system_prompt=batch_system_prompt, use_glossary=False)
        if not translated_result:
            print(f"[翻译] 批次 {batch_index + 1} 失败，跳过")
            return {}

        parsed_result = None
        try:
            parsed_result = json.loads(translated_result)
        except json.JSONDecodeError:
            cleaned = re.sub(r"^```(?:json)?\s*", "", translated_result.strip())
            cleaned = re.sub(r"\s*```$", "", cleaned)
            try:
                parsed_result = json.loads(cleaned)
            except json.JSONDecodeError:
                pass

        batch_translations = {}
        if isinstance(parsed_result, list):
            for obj in parsed_result:
                if isinstance(obj, dict) and "id" in obj and "translated_text" in obj:
                    tid = str(obj["id"])
                    if tid.startswith("t"):
                        try:
                            batch_translations[int(tid[1:])] = str(obj["translated_text"])
                        except ValueError:
                            pass
        else:
            lines = [l.strip() for l in (translated_result or "").splitlines() if l.strip()]
            for item, line in zip(batch, lines):
                tid = str(item["id"])
                if tid.startswith("t"):
                    try:
                        batch_translations[int(tid[1:])] = line
                    except ValueError:
                        pass

        print(f"[翻译] 批次 {batch_index + 1}/{len(batches)} 完成: {len(batch_translations)} 项")
        return batch_translations

    if max_workers <= 1:
        for i, batch in enumerate(batches):
            translate_idx_to_translation.update(translate_batch(i, batch))
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(translate_batch, i, b): i for i, b in enumerate(batches)}
            for future in as_completed(futures):
                try:
                    translate_idx_to_translation.update(future.result())
                except Exception as e:
                    print(f"[翻译] 批次异常: {e}")

    translated_count = 0
    for translate_idx, block_idx in block_index_map.items():
        if translate_idx in translate_idx_to_translation:
            blocks[block_idx]["translated_text"] = translate_idx_to_translation[translate_idx]
            translated_count += 1

    print(f"[翻译] 完成: {translated_count}/{len(items_to_translate)}")
    return blocks


# ── 内容提取 ─────────────────────────────────────────

def blocks_to_plaintext(blocks: list, field: str = "source_text") -> str:
    """将 blocks 列表转为纯文本"""
    text_parts = []
    for block in blocks or []:
        if block.get("type") == "img":
            meta = block.get("meta") or {}
            src = meta.get("src", "")
            alt = meta.get("alt", "")
            if src:
                text_parts.append(f"[IMAGE:{alt}]({src})" if alt else f"[IMAGE]({src})")
            continue
        text = (block.get(field) or "").strip()
        if text:
            text_parts.append(text)
    return "\n\n".join(text_parts).strip()


# ── 下载 ─────────────────────────────────────────────

def download_header_image(image_url, save_path):
    """下载文章头图"""
    if not image_url:
        return False
    max_retries = int(CFG.get("retry", {}).get("download", {}).get("max_retries", 3))
    for attempt in range(max_retries + 1):
        try:
            if attempt > 0:
                print(f"[下载] 第 {attempt} 次重试...")
            response = requests.get(
                image_url, headers=HEADERS_HTML,
                timeout=int(CFG["http"].get("timeout", 120)), verify=CFG["http"]["verify_ssl"],
                proxies=PROXIES, stream=True
            )
            response.raise_for_status()
            with open(save_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            print(f"[下载] 头图保存: {save_path}")
            return True
        except Exception as e:
            print(f"[下载] 失败: {e}")
            if attempt < max_retries:
                time.sleep(attempt * 2 + 2)
    return False


# ── 保存 ─────────────────────────────────────────────

def reindex_blocks(blocks: list) -> list:
    for i, block in enumerate(blocks):
        block["id"] = f"b{i+1:04d}"
    return blocks


def save_article_json(data: dict, save_dir: str = None) -> str:
    """
    将文章数据保存为 JSON 文件，返回保存路径。
    文件名使用安全的标题+时间戳，避免冲突。
    """
    if not data:
        return None

    save_dir = save_dir or CFG["output"]["save_dir"]
    os.makedirs(save_dir, exist_ok=True)

    # 重新编号 blocks
    if "blocks" in data and isinstance(data["blocks"], list):
        data["blocks"] = reindex_blocks(data["blocks"])
        for block in data["blocks"]:
            if "translated_text" in block and block["translated_text"]:
                block["translated_text"] = block["translated_text"].replace('\\\\"', '"')

    title = data.get("title", "untitled")
    release_date = data.get("release_date", "")

    # 生成时间戳
    try:
        if 'T' in release_date:
            date_part, time_part = release_date.split('T')
            time_part = time_part.replace(':', '_').replace('Z', '')
            timestamp = f"{date_part}_{time_part}"
        else:
            timestamp = release_date.replace(':', '_').replace(' ', '_')
    except (ValueError, AttributeError):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 清理非法字符
    safe_title = title.replace(' ', '_')
    for char in ['\\', '/', ':', '*', '?', '"', '<', '>', '|']:
        safe_title = safe_title.replace(char, '_')
    safe_title = re.sub(r'_+', '_', safe_title).strip('_')
    timestamp = re.sub(r'_+', '_', timestamp).strip('_')

    file_path = os.path.join(save_dir, f"news_{safe_title}_{timestamp}.json")

    # 避免文件冲突：如果已存在同名文件，加序号
    if os.path.exists(file_path):
        base, ext = os.path.splitext(file_path)
        counter = 2
        while os.path.exists(f"{base}_{counter}{ext}"):
            counter += 1
        file_path = f"{base}_{counter}{ext}"

    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[保存] JSON: {file_path}")
    except IOError as e:
        print(f"[保存] 写入失败: {e}")
        return None

    # 下载头图（与 JSON 同名）
    header_image_url = data.get("header_image_url", "")
    if header_image_url:
        image_ext = ".jpg"
        try:
            url_path = header_image_url.split("?")[0]
            if "." in url_path:
                ext = url_path.rsplit(".", 1)[-1].lower()
                if ext in ["jpg", "jpeg", "png", "gif", "webp"]:
                    image_ext = f".{ext}"
        except Exception:
            pass
        base_path = file_path.rsplit(".", 1)[0]
        image_path = base_path + image_ext
        download_header_image(header_image_url, image_path)

    return file_path


# ── 高级处理 ─────────────────────────────────────────

def process_article(news_item: dict) -> dict:
    """
    完整处理单篇文章：解析 → 翻译 → 组装数据
    
    Args:
        news_item: 新闻列表项，包含 url, title 等字段
    
    Returns:
        完整的文章数据字典，失败返回 None
    """
    import sys

    print(f"\n[处理] {news_item['title']}")
    sys.stdout.flush()

    # 解析
    article_data = parse_article_page(news_item['url'])
    if not article_data:
        print("[处理] 解析失败")
        return None

    # 翻译标题
    title_to_translate = article_data["title"] or news_item['title']
    translated_title = translate_text(
        title_to_translate,
        system_prompt=CFG["prompts"]["translate_title_system"]
    ) or ""

    if translated_title:
        print(f"  原标题: {title_to_translate}")
        print(f"  译标题: {translated_title}")

    # 翻译内容
    blocks = article_data.get("blocks", [])
    translate_blocks(blocks)

    source_content = blocks_to_plaintext(blocks, field="source_text")
    translated_content = blocks_to_plaintext(blocks, field="translated_text")

    return {
        "title": title_to_translate,
        "translated_title": translated_title,
        "release_date": article_data.get("release_date") or news_item.get("release_date", ""),
        "url": news_item.get("url", ""),
        "author": news_item.get("author", ""),
        "imageAltText": news_item.get("imageAltText", ""),
        "description": news_item.get("description", ""),
        "header_image_url": article_data.get("header_image_url", ""),
        "blocks": blocks,
        "content": source_content,
        "translated_content": translated_content,
        "source": "minecraft_api"
    }
