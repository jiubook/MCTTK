#!/usr/bin/env python3
"""utils.py — 项目公共工具函数"""

import contextlib
import json
import os
import re

MODULE_TYPE_MAP = {
    'module_java_snapshot_header': 'java_snapshot',
    'module_java_snapshot_footer': 'java_snapshot',
    'module_java_prerelease_header': 'java_prerelease',
    'module_java_prerelease_footer': 'java_prerelease',
    'module_java_rc_header': 'java_rc',
    'module_java_rc_footer': 'java_rc',
    'module_java_release_header': 'java_release',
    'module_java_release_footer': 'java_release',
    'module_bedrock_beta_header': 'bedrock_beta',
    'module_bedrock_beta_footer': 'bedrock_beta',
    'module_bedrock_release_header': 'bedrock_release',
    'module_bedrock_release_footer': 'bedrock_release',
    'module_commentary_header': 'commentary',
    'module_commentary_footer': 'commentary',
    'module_normal_header': 'normal',
    'module_normal_footer': 'normal',
}


def load_dotenv(project_dir: str = None) -> None:
    """加载同目录下的 .env 文件到环境变量（已存在的变量不覆盖）"""
    if project_dir is None:
        project_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(project_dir, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


# ── 新闻类型分类（规则引擎）──────────────────────────

_CLASSIFY_RULES = None


def _load_classify_rules(rules_path: str = None) -> list:
    """加载分类规则文件，返回规则列表（带预编译正则）"""
    if rules_path is None:
        rules_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "classify_rules.json")
    if not os.path.exists(rules_path):
        return []
    try:
        with open(rules_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []

    compiled = []
    for rule in data.get("rules", []):
        try:
            pattern = re.compile(rule["pattern"], re.IGNORECASE)
        except re.error:
            continue
        excludes = []
        for ex in rule.get("exclude", []):
            with contextlib.suppress(re.error):
                excludes.append(re.compile(ex, re.IGNORECASE))
        compiled.append({
            "type": rule["type"],
            "pattern": pattern,
            "excludes": excludes,
            "require_chinese": rule.get("require_chinese", False),
            "chinese_keywords": rule.get("chinese_keywords", []),
        })
    return compiled


def _get_classify_rules() -> list:
    global _CLASSIFY_RULES
    if _CLASSIFY_RULES is None:
        _CLASSIFY_RULES = _load_classify_rules()
    return _CLASSIFY_RULES


def classify_article_type(
    title: str,
    *,
    chinese: bool = False,
    commentary: bool = False,
    fallback: str | None = "other",
) -> str | None:
    """
    统一的文章类型分类核心逻辑（基于 classify_rules.json 规则引擎）。

    分类优先级：
      1. 按规则文件中的顺序匹配（优先级从高到低）
      2. 首个命中的规则生效
      3. 排除模式命中则跳过该规则
      4. 无规则命中时回退到 commentary / fallback

    Args:
        title: 文章标题
        chinese: True 时额外检测中文关键词
        commentary: True 时时评类型返回 "commentary"（向后兼容）
        fallback: 无匹配时的返回值（"other"、"normal" 或 None）
    """
    t = title or ""

    # 按规则顺序匹配
    for rule in _get_classify_rules():
        # 排除模式优先
        if any(ex.search(t) for ex in rule["excludes"]):
            continue
        # 英文正则匹配
        if rule["pattern"].search(t):
            return rule["type"]
        # 中文关键词匹配（仅当 chinese=True 且规则启用时）
        if chinese and rule["require_chinese"] and any(kw in t for kw in rule["chinese_keywords"]):
            return rule["type"]

    # 时评（向后兼容）
    if commentary and ("时评" in t or "commentary" in t.lower()):
        return "commentary"

    return fallback
