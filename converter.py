#!/usr/bin/env python3
"""converter.py — JSON 到 BBCode/Markdown 转换器（原 J2MM）

用法:
  python converter.py <input.json> [选项]
  python converter.py --batch <目录> [选项]

也可作为模块导入:
  from converter import J2MMConverter
"""
import json
import re
import argparse
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime


# ── 工具函数 ─────────────────────────────────────────

def _md_links_to_bbcode(text: str) -> str:
    return re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'[url=\2]\1[/url]', text)


def _parse_date(date_str: str) -> str:
    if not date_str:
        return date_str
    from datetime import timezone, timedelta
    TZ_CN = timezone(timedelta(hours=8))
    try:
        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        dt = dt.astimezone(TZ_CN)
        return f"{dt.year}/{dt.month}/{dt.day} {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
    except Exception:
        pass
    try:
        dt = datetime.strptime(date_str.strip(), "%d %B %Y")
        return f"{dt.year}/{dt.month}/{dt.day} 00:00:00"
    except Exception:
        pass
    try:
        dt = datetime.strptime(date_str.strip(), "%B %d, %Y")
        return f"{dt.year}/{dt.month}/{dt.day} 00:00:00"
    except Exception:
        pass
    return date_str


def _bbcode_to_markdown(bbcode: str) -> str:
    text = bbcode
    for _ in range(5):
        text = re.sub(r'\[b\](.*?)\[/b\]', r'**\1**', text, flags=re.DOTALL)
        text = re.sub(r'\[i\](.*?)\[/i\]', r'*\1*', text, flags=re.DOTALL)
        text = re.sub(r'\[url=([^\]]+)\](.*?)\[/url\]', r'[\2](\1)', text, flags=re.DOTALL)
        text = re.sub(r'\[size=[^\]]+\](.*?)\[/size\]', r'\1', text, flags=re.DOTALL)
        text = re.sub(r'\[color=[^\]]+\](.*?)\[/color\]', r'\1', text, flags=re.DOTALL)
        text = re.sub(r'\[align=[^\]]+\](.*?)\[/align\]', r'\1', text, flags=re.DOTALL)
        text = re.sub(r'\[font=[^\]]+\](.*?)\[/font\]', r'\1', text, flags=re.DOTALL)
        text = re.sub(r'\[table=[^\]]+\](.*?)\[/table\]', r'\1', text, flags=re.DOTALL)
        text = re.sub(r'\[tr=[^\]]+\](.*?)\[/tr\]', r'\1\n', text, flags=re.DOTALL)
        text = re.sub(r'\[td\](.*?)\[/td\]', r'\1', text, flags=re.DOTALL)
        text = re.sub(r'\[float=[^\]]+\](.*?)\[/float\]', r'\1', text, flags=re.DOTALL)
        text = re.sub(r'\[img=[^\]]+\](.*?)\[/img\]', r'![](\1)', text, flags=re.DOTALL)
        text = re.sub(r'\[img\](.*?)\[/img\]', r'![](\1)', text, flags=re.DOTALL)
        text = re.sub(r'\[list=?\d*\](.*?)\[/list\]', r'\1', text, flags=re.DOTALL)
        text = text.replace('[*]', '- ')
        text = re.sub(r'\[quote\](.*?)\[/quote\]',
                      lambda m: '> ' + m.group(1).replace('\n', '\n> '), text, flags=re.DOTALL)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ── BBCode 渲染器 ───────────────────────────────────

class BBCodeRenderer:
    def render(self, blocks: List[Dict]) -> str:
        out = []
        i = 0
        while i < len(blocks):
            block = blocks[i]
            btype = block.get('type', 'p').lower()
            if btype == 'li':
                chunk, i = self._collect_li_chunk(blocks, i)
                out.append(self._render_li_chunk(chunk))
            elif btype in ('pre', 'code'):
                src = block.get('source_text', '') or block.get('translated_text', '')
                out.append(f'[code]{src}[/code]')
                i += 1
            elif btype == 'img':
                out.append(self._render_img_bbcode(block))
                i += 1
            elif btype in ('h1', 'h2', 'h3', 'h4'):
                out.append(self._render_heading_bbcode(block))
                i += 1
            elif btype in ('blockquote', 'quote'):
                out.append(self._render_quote_bbcode(block))
                i += 1
            else:
                rendered = self._render_para_bbcode(block)
                if rendered:
                    out.append(rendered)
                i += 1
        return '\n'.join(out)

    def _collect_li_chunk(self, blocks, start):
        chunk = []
        i = start
        while i < len(blocks) and blocks[i].get('type', '').lower() == 'li':
            chunk.append(blocks[i])
            i += 1
        return chunk, i

    def _render_li_chunk(self, items):
        if not items:
            return ''
        lines = []
        indent_stack = []
        for item in items:
            level = item.get('meta', {}).get('indent_level', 0)
            src = _md_links_to_bbcode(item.get('source_text', '').strip())
            tr = _md_links_to_bbcode(item.get('translated_text', '').strip())
            while indent_stack and indent_stack[-1] > level:
                lines.append('[/list]')
                indent_stack.pop()
            if not indent_stack or indent_stack[-1] < level:
                lines.append('[list]')
                indent_stack.append(level)
            if tr and tr == src:
                lines.append(f'[*]{tr}')
            elif tr and src:
                lines.append(f'[*]{tr}\n[color=#bcbcbc]{src}[/color]')
            else:
                lines.append(f'[*]{tr or src}')
        while indent_stack:
            lines.append('[/list]')
            indent_stack.pop()
        return '\n'.join(lines)

    def _render_heading_bbcode(self, block):
        btype = block.get('type', '').lower()
        src = _md_links_to_bbcode(block.get('source_text', '').strip())
        tr = _md_links_to_bbcode(block.get('translated_text', '').strip())
        def duo(main, sub):
            if main and sub and main == sub:
                return main
            if main and sub:
                return f'{main}\n[color=#bcbcbc]{sub}[/color]\n'
            return main or sub or ''
        content = duo(tr, src)
        if btype in ('h1', 'h2'):
            return f'[hr]\n[size=6][b]{content}[/b][/size]'
        if btype == 'h3':
            return f'[size=5][b]{content}[/b][/size]'
        if btype == 'h4':
            return f'[size=4][b]{content}[/b][/size]'
        return content

    def _render_quote_bbcode(self, block):
        src = block.get('source_text', '').strip()
        tr = block.get('translated_text', '').strip()
        if tr and tr == src:
            return f'[quote]{tr}[/quote]'
        if tr and src:
            return f'[quote]{tr}\n[color=#bcbcbc]{src}[/color][/quote]'
        return f'[quote]{tr or src}[/quote]'

    def _render_img_bbcode(self, block):
        meta = block.get('meta', {})
        src = meta.get('src', '').strip()
        alt = meta.get('alt', '').strip()
        if not src:
            return f'[i]{alt}[/i]' if alt else ''
        return f'[align=center][img]{src}[/img][/align]'

    def _render_para_bbcode(self, block):
        src = _md_links_to_bbcode(block.get('source_text', '').strip())
        tr = _md_links_to_bbcode(block.get('translated_text', '').strip())
        if tr and tr == src:
            return tr
        if tr and src:
            return f'{tr}\n[color=#bcbcbc]{src}[/color]'
        return tr or src or ''


# ── Markdown 渲染器 ─────────────────────────────────

class MarkdownRenderer:
    def render(self, blocks: List[Dict]) -> str:
        out = []
        i = 0
        while i < len(blocks):
            block = blocks[i]
            btype = block.get('type', 'p').lower()
            if btype == 'li':
                chunk, i = self._collect_li_chunk(blocks, i)
                out.append(self._render_li_chunk(chunk))
            elif btype in ('pre', 'code'):
                src = block.get('source_text', '') or block.get('translated_text', '')
                out.append(f'```\n{src}\n```')
                i += 1
            elif btype == 'img':
                out.append(self._render_img_md(block))
                i += 1
            elif btype in ('h1', 'h2', 'h3', 'h4'):
                out.append(self._render_heading_md(block))
                i += 1
            elif btype in ('blockquote', 'quote'):
                out.append(self._render_quote_md(block))
                i += 1
            else:
                rendered = self._render_para_md(block)
                if rendered:
                    out.append(rendered)
                i += 1
        return '\n\n'.join(out)

    def _collect_li_chunk(self, blocks, start):
        chunk = []
        i = start
        while i < len(blocks) and blocks[i].get('type', '').lower() == 'li':
            chunk.append(blocks[i])
            i += 1
        return chunk, i

    def _render_li_chunk(self, items):
        if not items:
            return ''
        lines = []
        for item in items:
            level = item.get('meta', {}).get('indent_level', 0)
            src = item.get('source_text', '').strip()
            tr = item.get('translated_text', '').strip()
            indent = '    ' * level
            prefix = f'{indent}- '
            if tr and tr == src:
                lines.append(f'{prefix}{tr}')
            elif tr and src:
                lines.append(f'{prefix}{tr}')
                lines.append(f'{prefix}{src}')
            else:
                lines.append(f'{prefix}{tr or src}')
        return '\n'.join(lines)

    def _render_heading_md(self, block):
        btype = block.get('type', '').lower()
        src = block.get('source_text', '').strip()
        tr = block.get('translated_text', '').strip()
        level_map = {'h1': '#', 'h2': '##', 'h3': '###', 'h4': '####'}
        prefix = level_map.get(btype, '#')
        hr = '---\n\n' if btype in ('h1', 'h2') else ''
        text = tr or src
        suffix = f'\n\n> {src}' if src and tr and src != tr else ''
        return f'{hr}{prefix} {text}{suffix}'

    def _render_quote_md(self, block):
        src = block.get('source_text', '').strip()
        tr = block.get('translated_text', '').strip()
        a = tr.replace('\n', '\n> ') if tr else ''
        b = src.replace('\n', '\n> ') if src else ''
        if a and b and a == b:
            return f'> {a}'
        if a and b:
            return f'> {a}\n>\n> {b}'
        return f'> {a or b}' if (a or b) else ''

    def _render_img_md(self, block):
        meta = block.get('meta', {})
        src = meta.get('src', '').strip()
        alt = meta.get('alt', '').strip()
        if not src:
            return f'*{alt}*' if alt else ''
        return f'![{alt}]({src})'

    def _render_para_md(self, block):
        src = block.get('source_text', '').strip()
        tr = block.get('translated_text', '').strip()
        if tr and tr == src:
            return tr
        if tr and src:
            return f'{tr}\n\n> {src}'
        return tr or (f'> {src}' if src else '') or ''


# ── 文章类型检测 ─────────────────────────────────────

def _detect_article_type(title: str) -> str:
    t = title or ''
    if 'Snapshot' in t:
        return 'java_snapshot'
    if 'Pre-Release' in t or 'Pre Release' in t:
        return 'java_prerelease'
    if 'Release Candidate' in t:
        return 'java_rc'
    if 'Beta' in t or 'Preview' in t:
        return 'bedrock_beta'
    if 'Bedrock' in t:
        return 'bedrock_release'
    if '时评' in t or 'Commentary' in t:
        return 'commentary'
    if 'Java Edition' in t:
        return 'java_release'
    return 'normal'


_MODULE_TYPE_MAP = {
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


# ── 主转换器 ─────────────────────────────────────────

class J2MMConverter:
    def __init__(self, modules_config: Optional[Dict] = None):
        self.modules_config = modules_config or {'default_modules': [], 'custom_modules': []}
        self._bb = BBCodeRenderer()
        self._md = MarkdownRenderer()

    def convert_to_bbcode(self, json_data: Dict) -> str:
        blocks = json_data.get('blocks', [])
        parts = []
        article_type = _detect_article_type(json_data.get('title', ''))

        for m in self._get_modules('start', article_type):
            parts.append(m['content'])

        parts.append('[hr]')
        parts.append('[align=center][size=5][b]NEWS[/b][/size][/align]')

        title_cn = json_data.get('translated_title', '')
        title_en = json_data.get('title', '')
        if title_cn:
            parts.append(f'[align=center][size=6][b]{title_cn}[/b][/size][/align]')
        if title_en and title_en != title_cn:
            parts.append(f'[align=center][size=4]{title_en}[/size][/align]')

        meta_lines = []
        if json_data.get('release_date'):
            meta_lines.append(f"[b]时间：[/b] {_parse_date(json_data['release_date'])}")
        if json_data.get('author'):
            meta_lines.append(f"[b]作者：[/b] {json_data['author']}")
        if json_data.get('url'):
            u = json_data['url']
            meta_lines.append(f'[b]原文：[/b] [url={u}]{u}[/url]')
        if json_data.get('description'):
            meta_lines.append(f"[b]简介：[/b][i]{json_data['description']}[/i]")
        if meta_lines:
            parts.append('[quote]' + '\n'.join(meta_lines) + '[/quote]')

        if blocks:
            parts.append(self._bb.render(blocks))

        parts.append('[hr]')

        for m in self._get_modules('custom', article_type):
            parts.append(m['content'])
        for m in self._get_modules('end', article_type):
            parts.append(m['content'])

        return '\n\n'.join(p for p in parts if p)

    def convert_to_markdown(self, json_data: Dict) -> str:
        blocks = json_data.get('blocks', [])
        parts = []
        article_type = _detect_article_type(json_data.get('title', ''))

        for m in self._get_modules('start', article_type):
            parts.append(_bbcode_to_markdown(m['content']))

        parts.append('---')
        parts.append('**NEWS**')

        title_cn = json_data.get('translated_title', '')
        title_en = json_data.get('title', '')
        if title_cn:
            parts.append(f'# {title_cn}')
        if title_en and title_en != title_cn:
            parts.append(f'_{title_en}_')

        meta_lines = []
        if json_data.get('release_date'):
            meta_lines.append(f"- 时间：{_parse_date(json_data['release_date'])}")
        if json_data.get('author'):
            meta_lines.append(f"- 作者：{json_data['author']}")
        if json_data.get('url'):
            meta_lines.append(f"- 原文：{json_data['url']}")
        if json_data.get('description'):
            meta_lines.append(f"- 简介：{json_data['description']}")
        if meta_lines:
            parts.append('\n'.join(meta_lines))

        if blocks:
            parts.append(self._md.render(blocks))

        parts.append('---')

        for m in self._get_modules('custom', article_type):
            parts.append(_bbcode_to_markdown(m['content']))
        for m in self._get_modules('end', article_type):
            parts.append(_bbcode_to_markdown(m['content']))

        return '\n\n'.join(p for p in parts if p)

    def _get_modules(self, position: str, article_type: str = 'normal') -> List[Dict]:
        cfg = self.modules_config
        if position == 'custom':
            return [m for m in cfg.get('custom_modules', []) if m.get('enabled')]
        modules = []
        for m in cfg.get('default_modules', []):
            if m.get('position') != position:
                continue
            if m.get('enabled'):
                modules.append(m)
            elif _MODULE_TYPE_MAP.get(m.get('id', '')) == article_type:
                modules.append(m)
        return sorted(modules, key=lambda m: m.get('order', 9999))


# ── 文件转换辅助 ─────────────────────────────────────

def convert_json_file(json_path: str, output_prefix: str = None, modules_config: dict = None) -> tuple:
    """
    将单个 JSON 文件转换为 BBCode (.txt) 和 Markdown (.md)
    
    Returns:
        (bbcode_path, markdown_path) 元组
    """
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    conv = J2MMConverter(modules_config)
    stem = output_prefix or Path(json_path).stem

    bbcode_content = conv.convert_to_bbcode(data)
    bbcode_path = f'{stem}.txt'
    with open(bbcode_path, 'w', encoding='utf-8') as f:
        f.write(bbcode_content)
    print(f"[转换] BBCode: {bbcode_path}")

    md_content = conv.convert_to_markdown(data)
    md_path = f'{stem}.md'
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(md_content)
    print(f"[转换] Markdown: {md_path}")

    return bbcode_path, md_path


# ── CLI 入口 ─────────────────────────────────────────

def _load_json(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _load_modules(path):
    if path and Path(path).exists():
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None


def _save(content, path):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f'[OK] {path}')


def main():
    parser = argparse.ArgumentParser(description='converter.py — JSON 到 BBCode/Markdown 转换器')
    parser.add_argument('input', nargs='?', help='输入 JSON 文件路径')
    parser.add_argument('--batch', metavar='DIR', help='批量转换目录')
    parser.add_argument('-o', '--output', help='输出路径')
    parser.add_argument('-m', '--modules', help='模块配置文件路径')
    parser.add_argument('--bbcode-only', action='store_true', help='仅输出 BBCode')
    parser.add_argument('--markdown-only', action='store_true', help='仅输出 Markdown')
    args = parser.parse_args()

    if not args.modules:
        default_cfg = Path(__file__).parent / 'modules_config.json'
        if default_cfg.exists():
            args.modules = str(default_cfg)

    if args.batch:
        in_dir = Path(args.batch)
        out_dir = Path(args.output) if args.output else in_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        modules = _load_modules(args.modules)
        conv = J2MMConverter(modules)
        json_files = list(in_dir.glob('*.json'))
        if not json_files:
            print(f'未找到 JSON 文件: {in_dir}')
            return
        ok = 0
        for jf in json_files:
            try:
                data = _load_json(str(jf))
                stem = jf.stem
                if not args.markdown_only:
                    _save(conv.convert_to_bbcode(data), str(out_dir / f'{stem}.txt'))
                if not args.bbcode_only:
                    _save(conv.convert_to_markdown(data), str(out_dir / f'{stem}.md'))
                ok += 1
            except Exception as e:
                print(f'[错误] {jf.name}: {e}')
        print(f'\n完成：{ok}/{len(json_files)}')
    elif args.input:
        modules = _load_modules(args.modules)
        data = _load_json(args.input)
        conv = J2MMConverter(modules)
        stem = args.output or Path(args.input).stem
        if not args.markdown_only:
            _save(conv.convert_to_bbcode(data), f'{stem}.txt')
        if not args.bbcode_only:
            _save(conv.convert_to_markdown(data), f'{stem}.md')
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
