#!/usr/bin/env python3
"""
poster.py — MCBBS 自动发帖模块（新闻模式 + 图片上传）

支持两种使用方式：
  1. CLI 模式：python poster.py [选项]（与原 post.py 兼容）
  2. 模块导入：from poster import MCBBSPoster（供 main.py 调用）

文件命名规则：
  news_xxx.txt  → 帖子正文（MCBBS BBCode）
  news_xxx.json → 元数据，需包含 "title" 字段
  news_xxx.jpg  → 题图（可选）
"""

import argparse
import contextlib
import glob
import json
import logging
import os
import re
import sys
import time

import requests

from utils import MODULE_TYPE_MAP

# ── 配置 ─────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

_CATEGORY_GROUP = {
    'java_snapshot': 'Java资讯',
    'java_prerelease': 'Java资讯',
    'java_rc': 'Java资讯',
    'java_release': 'Java资讯',
    'bedrock_beta': '基岩版资讯',
    'bedrock_release': '基岩版资讯',
    'commentary': '块讯',
    'normal': '块讯',
    'host': '主机资讯',
    'flash': '快讯',
}

# 高亮颜色映射（news_type → Discuz highlight_color 值）
# 对应 MCBBS 管理操作中的高亮设定
HIGHLIGHT_COLOR_MAP = {
    'java_release': 1,      # 正式版 #EE1B2E
    'bedrock_release': 1,   # 正式版 #EE1B2E
    'java_snapshot': 4,     # 快照/预览版 #3C9D40
    'java_prerelease': 4,   # 预发布版 #3C9D40
    'java_rc': 4,           # 候选版本 #3C9D40
    'bedrock_beta': 4,      # 测试版 #3C9D40
    'flash': 8,             # 快讯 #EC1282
    'host': 2,              # 主机资讯 #EE5023
    'peripheral': 6,        # 周边消息 #2B65B7
}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

IMG_INSERT_BEFORE = "[align=center][size=5][b]NEWS[/b]"


# ── 配置加载 ─────────────────────────────────────────

def load_poster_config(config_path: str = None) -> dict:
    """从统一 config.json 中提取 mcbbs 相关配置"""
    if config_path is None:
        config_path = os.path.join(SCRIPT_DIR, "config.json")

    cfg = {
        "base_url": "https://www.mcbbs.co",
        "forum_fid": 2,
        "username": "",
        "password": "",
        "captcha_answer": "",
        "sortid_map": {},
    }

    if os.path.exists(config_path):
        try:
            with open(config_path, encoding="utf-8") as f:
                full_config = json.load(f)
            mcbbs = full_config.get("mcbbs", {})
            for key in cfg:
                if key in mcbbs and mcbbs[key]:
                    cfg[key] = mcbbs[key]
        except Exception as e:
            print(f"[配置] 读取 mcbbs 配置失败: {e}")

    # 环境变量覆盖
    env_map = {
        "MCBBS_BASE_URL": "base_url",
        "MCBBS_FORUM_FID": "forum_fid",
        "MCBBS_USERNAME": "username",
        "MCBBS_PASSWORD": "password",
        "MCBBS_CAPTCHA_ANSWER": "captcha_answer",
    }
    for env_name, cfg_key in env_map.items():
        val = os.environ.get(env_name)
        if val:
            if cfg_key == "forum_fid":
                with contextlib.suppress(ValueError):
                    val = int(val)
            cfg[cfg_key] = val

    return cfg


# ── 辅助函数 ─────────────────────────────────────────

def detect_module_type(message: str, title: str) -> str | None:
    for tag, category in MODULE_TYPE_MAP.items():
        if f"[{tag}]" in message:
            return category
    from utils import classify_article_type
    return classify_article_type(title, chinese=True, fallback=None)


def find_image(news_dir: str, stem: str) -> str | None:
    for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]:
        img_path = os.path.join(news_dir, stem + ext)
        if os.path.exists(img_path):
            return img_path
    return None


def insert_image_bbcode(message: str, aid: str) -> str:
    img_tag = f"[align=center][attachimg]{aid}[/attachimg][/align]"
    idx = message.find(IMG_INSERT_BEFORE)
    if idx != -1:
        return message[:idx] + img_tag + "\n\n" + message[idx:]
    hr_idx = message.find("[hr]")
    if hr_idx != -1:
        insert_pos = hr_idx + len("[hr]")
        while insert_pos < len(message) and message[insert_pos] in "\n\r ":
            insert_pos += 1
        return message[:hr_idx + len("[hr]")] + "\n\n" + img_tag + "\n\n" + message[insert_pos:]
    return img_tag + "\n\n" + message


# ── MCBBS 会话与登录 ─────────────────────────────────

def extract_formhash(html: str) -> str:
    m = re.search(r'name="formhash"\s+value="([a-f0-9]+)"', html)
    if not m:
        raise ValueError("无法提取 formhash")
    return m.group(1)


def extract_loginhash(html: str) -> str:
    m = re.search(r'loginhash=([A-Za-z0-9]+)', html)
    return m.group(1) if m else ""


def _verify_login(session: requests.Session, base_url: str) -> str:
    r = session.get(f"{base_url}/forum.php")
    r.raise_for_status()
    uid = re.search(r"discuz_uid\s*=\s*'(\d+)'", r.text)
    if not uid or uid.group(1) == "0":
        raise RuntimeError("登录验证失败：discuz_uid=0")
    return extract_formhash(r.text)


def _init_ocr():
    """初始化 ddddocr，失败返回 None"""
    try:
        import ddddocr
        return ddddocr.DdddOcr(show_ad=False)
    except Exception:  # noqa: BLE001
        logging.debug("ddddocr 初始化失败，将跳过 OCR 识别", exc_info=True)
        return None


def _preprocess_captcha(img_bytes: bytes) -> list:
    """对验证码图片做多种预处理，返回多个候选图片 bytes"""
    candidates = [img_bytes]
    try:
        import io as _io

        from PIL import Image, ImageFilter
        img = Image.open(_io.BytesIO(img_bytes))
        gray = img.convert("L")
        bw = gray.point(lambda x: 255 if x > 128 else 0, "1")
        buf = _io.BytesIO()
        bw.save(buf, format="PNG")
        candidates.append(buf.getvalue())
        big = img.convert("L").resize((img.width * 2, img.height * 2), Image.LANCZOS)
        sharp = big.filter(ImageFilter.SHARPEN)
        buf2 = _io.BytesIO()
        sharp.save(buf2, format="PNG")
        candidates.append(buf2.getvalue())
    except Exception:  # noqa: BLE001
        logging.debug("验证码图片预处理失败，使用原始图片", exc_info=True)
    return candidates


def _ocr_recognize(ocr, img_bytes: bytes) -> str:
    """用 OCR 识别验证码，返回识别结果（失败返回空字符串）"""
    if not ocr:
        return ""
    candidates = _preprocess_captcha(img_bytes)
    for idx, img_data in enumerate(candidates):
        try:
            raw = re.sub(r'[^a-zA-Z0-9]', '', ocr.classification(img_data).strip())
            if 3 <= len(raw) <= 8:
                tag = "原始" if idx == 0 else f"预处理#{idx}"
                print(f"    OCR ({tag}): {raw}")
                return raw
        except Exception:  # noqa: BLE001
            logging.debug("OCR 识别单张图片失败", exc_info=True)
    return ""


def _submit_captcha_login(session, base_url, r_cap, idhash, auth_token,
                          username, password, answer) -> bool:
    """提交一次验证码登录，成功返回 True，失败返回 False"""
    from urllib.parse import quote
    fh_cap = extract_formhash(r_cap.text)
    lh_cap = extract_loginhash(r_cap.text)
    seccode_field = "seccodeverify"
    if 'name="seccode"' in r_cap.text and 'name="seccodeverify"' not in r_cap.text:
        seccode_field = "seccode"
    all_modids = re.findall(r"updateseccode\('[^']+',[^,]+,\s*'([^']+)'\)", r_cap.text)
    seccodemodid = all_modids[-1] if all_modids else "member::logging"

    post_data = {
        "formhash": fh_cap,
        "referer": f"{base_url}/",
        "auth": auth_token,
        "username": username,
        "password": password,
        "questionid": "0",
        "answer": "",
        "seccodehash": idhash,
        "seccodemodid": seccodemodid,
        seccode_field: answer,
    }
    referer_url = (
        f"{base_url}/member.php?mod=logging&action=login"
        f"&auth={quote(auth_token, safe='')}&referer={base_url}/&cookietime=1"
    )
    print(f"    提交: {seccode_field}={answer}, password={'(set)' if password else '(EMPTY!)'}")
    session.headers.update({"Referer": referer_url})
    r_post = session.post(
        f"{base_url}/member.php?mod=logging&action=login"
        f"&loginsubmit=yes&loginhash={lh_cap}&inajax=1",
        data=post_data,
    )
    session.headers.pop("Referer", None)

    if "欢迎您回来" in r_post.text or "succeedhandle_" in r_post.text:
        return True

    err_match = re.search(r'id="messagetext"[^>]*>(.*?)</div>', r_post.text, re.S)
    if err_match:
        err_msg = re.sub(r'<[^>]+>', '', err_match.group(1)).strip()[:200]
        if "安全问题" not in err_msg:
            print(f"    ✗ 登录被拒: {err_msg}")
    else:
        print(f"    ✗ 未知登录结果（HTTP {r_post.status_code}，长度 {len(r_post.text)}）")
    return False


def _login_with_captcha(session, base_url, username, password, captcha_answer, r):
    from urllib.parse import quote, unquote
    print("    ⚠ 需要验证码")
    auth_m = re.search(r"auth=([A-Za-z0-9%/+]+)", r.text)
    if not auth_m:
        raise RuntimeError("无法提取 auth token")
    auth_token = unquote(auth_m.group(1))

    r_cap = session.get(
        f"{base_url}/member.php?mod=logging&action=login"
        f"&auth={quote(auth_token, safe='')}&referer={base_url}/&cookietime=1"
    )
    seccode_m = re.search(r"updateseccode\('([a-zA-Z0-9]+)'", r_cap.text)
    if not seccode_m:
        raise RuntimeError("无法提取验证码 hash")
    idhash = seccode_m.group(1)
    ocr = _init_ocr()

    for attempt in range(1, 6):
        referer_url = (
            f"{base_url}/member.php?mod=logging&action=login"
            f"&auth={auth_token}&referer={base_url}/&cookietime=1"
        )
        session.headers.update({"Referer": referer_url})
        update_seed = str(int(time.time() * 1000))[-6:]
        r_img = session.get(f"{base_url}/misc.php?mod=seccode&update={update_seed}&idhash={idhash}")
        session.headers.pop("Referer", None)

        if len(r_img.content) < 100:
            time.sleep(1)
            continue

        with open("captcha.png", "wb") as f:
            f.write(r_img.content)
        print(f"    验证码已保存: captcha.png ({len(r_img.content)} bytes)")

        answer = _ocr_recognize(ocr, r_img.content)
        if not answer:
            if captcha_answer:
                answer = captcha_answer
                print(f"    OCR 失败，使用手动验证码: {answer}")
            else:
                print(f"    OCR 失败，无手动验证码，跳过第{attempt}次")
                time.sleep(1)
                continue

        if _submit_captcha_login(session, base_url, r_cap, idhash, auth_token,
                                 username, password, answer):
            print(f"    ✓ 验证码登录成功！（第{attempt}次）")
            return _verify_login(session, base_url)

        if attempt < 5:
            print(f"    重新获取登录页（第{attempt+1}次尝试）...")
            r_cap = session.get(
                f"{base_url}/member.php?mod=logging&action=login"
                f"&auth={auth_token}&referer={base_url}/&cookietime=1"
            )
            auth_m_new = re.search(r"auth=([A-Za-z0-9%/+]+)", r_cap.text)
            if auth_m_new:
                auth_token = unquote(auth_m_new.group(1))
            seccode_m2 = re.search(r"updateseccode\('([a-zA-Z0-9]+)'", r_cap.text)
            if seccode_m2:
                idhash = seccode_m2.group(1)
            time.sleep(1)
            r_cap = session.get(
                f"{base_url}/member.php?mod=logging&action=login"
                f"&auth={quote(auth_token, safe='')}&referer={base_url}/&cookietime=1"
            )
            seccode_m2 = re.search(r"updateseccode\('([a-zA-Z0-9]+)'", r_cap.text)
            if seccode_m2:
                idhash = seccode_m2.group(1)

    raise RuntimeError("验证码识别率过低，请检查 captcha.png 后手动重试")


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    return s


# ── MCBBSPoster 类（编程接口）──────────────────────

class MCBBSPoster:
    """MCBBS 发帖器，支持批量发帖时复用登录会话"""

    def __init__(self, config: dict = None):
        if config is None:
            config = load_poster_config()
        self.base_url = config["base_url"]
        self.forum_fid = config["forum_fid"]
        self.username = config["username"]
        self.password = config["password"]
        self.captcha_answer = config.get("captcha_answer", "")
        self.sortid_map = config.get("sortid_map", {})
        self.session = None
        self.formhash = None

    def login(self) -> bool:
        """登录 MCBBS，返回是否成功"""
        if not self.username or not self.password:
            raise RuntimeError("未配置 MCBBS 账号密码")

        print(f"[*] 登录 MCBBS: {self.username}")
        self.session = _make_session()

        r = self.session.get(f"{self.base_url}/member.php?mod=logging&action=login")
        r.raise_for_status()
        formhash = extract_formhash(r.text)
        loginhash = extract_loginhash(r.text)

        login_url = (
            f"{self.base_url}/member.php?mod=logging&action=login"
            f"&loginsubmit=yes&loginhash={loginhash}"
        )
        data = {
            "formhash": formhash, "referer": "./",
            "username": self.username, "password": self.password,
            "questionid": "0", "answer": "", "cookietime": "2592000",
        }
        r = self.session.post(login_url, data=data)

        if "欢迎您回来" in r.text:
            self.formhash = _verify_login(self.session, self.base_url)
            print("    ✓ 登录成功！")
            return True

        if "验证码" in r.text:
            self.formhash = _login_with_captcha(
                self.session, self.base_url,
                self.username, self.password, self.captcha_answer, r
            )
            return True

        err = re.search(r'id="messagetext"[^>]*>(.*?)</div>', r.text, re.S)
        err_msg = re.sub(r"<[^>]+>", "", err.group(1)).strip()[:200] if err else "未知错误"
        raise RuntimeError(f"登录失败: {err_msg}")

    def _upload(self, file_path: str, upload_filename: str, mime_type: str,
                extra_data: dict = None) -> str:
        """内部上传方法，封装重试逻辑。extra_data 用于图片上传的 type:image 参数。"""
        upload_url = (
            f"{self.base_url}/misc.php?mod=swfupload&action=swfupload"
            f"&operation=upload&fid={self.forum_fid}"
        )
        referer = f"{self.base_url}/forum.php?mod=post&action=newthread&fid={self.forum_fid}"
        aid = ""
        for attempt in range(1, 4):
            r = self.session.get(referer)
            r.raise_for_status()
            hash_m = re.search(r'"hash"\s*:\s*"([a-f0-9]+)"', r.text)
            uid_m = re.search(r'"uid"\s*:\s*"(\d+)"', r.text)
            if not hash_m or not uid_m:
                raise RuntimeError("无法提取上传参数")

            self.session.headers.update({"Referer": referer})
            data = {"uid": uid_m.group(1), "hash": hash_m.group(1)}
            if extra_data:
                data.update(extra_data)
            with open(file_path, "rb") as f:
                r_up = self.session.post(
                    upload_url,
                    files={"Filedata": (upload_filename, f, mime_type)},
                    data=data,
                    timeout=30,
                )
            self.session.headers.pop("Referer", None)

            aid = r_up.text.strip()
            if aid.isdigit():
                return aid

            if attempt < 3:
                print(f"    ⚠ 第{attempt}次上传失败，2秒后重试...")
                time.sleep(2)

        raise RuntimeError(f"上传失败: {aid[:200]}")

    def upload_file(self, file_path: str, mime_type: str = None) -> str:
        """上传普通文件（非图片附件），返回附件 ID。"""
        if not self.session:
            raise RuntimeError("未登录，请先调用 login()")

        filename = os.path.basename(file_path)
        ext = os.path.splitext(file_path)[1].lower()
        if mime_type is None:
            mime_map = {
                ".json": "text/plain",   # Discuz swfupload 拒绝 application/json
                ".txt": "text/plain",
                ".md": "text/plain",     # Discuz 不接受 text/markdown
                ".pdf": "application/pdf",
                ".zip": "application/zip",
            }
            mime_type = mime_map.get(ext, "application/octet-stream")

        print(f"    上传文件: {filename}")

        # Discuz swfupload 服务端校验文件扩展名
        # 上传时改扩展名为 .txt 绕过限制（内容不变，用户下载后改名即可）
        allowed_exts = {".chm", ".pdf", ".zip", ".7z", ".7zip", ".rar", ".tar",
                        ".gz", ".bzip2", ".gif", ".jpg", ".jpeg", ".png", ".jar",
                        ".txt", ".webp", ".log", ".conf", ".mcworld", ".mcpack",
                        ".lang", ".bmp"}
        upload_filename = filename
        if ext not in allowed_exts:
            upload_filename = os.path.splitext(filename)[0] + ".txt"
            mime_type = "text/plain"
            print(f"    (扩展名 {ext} 不被允许，上传时改用 {upload_filename})")

        aid = self._upload(file_path, upload_filename, mime_type)
        print(f"    ✓ 文件上传成功！aid={aid}")
        return aid

    def upload_image(self, image_path: str) -> str:
        """上传图片，返回附件 ID"""
        if not self.session:
            raise RuntimeError("未登录，请先调用 login()")

        print(f"    上传图片: {os.path.basename(image_path)}")
        ext = os.path.splitext(image_path)[1].lower()
        mime_map = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"
        }
        mime = mime_map.get(ext, "image/jpeg")
        filename = os.path.basename(image_path)
        aid = self._upload(image_path, filename, mime, extra_data={"type": "image"})
        print(f"    ✓ 上传成功！aid={aid}")
        return aid

    def post_thread(self, title: str, message: str,
                    attachment_ids: list = None, sortid: int = None) -> str:
        """发帖，返回帖子 URL"""
        if not self.session:
            raise RuntimeError("未登录，请先调用 login()")

        print(f"    标题: {title}")
        print(f"    正文长度: {len(message)} 字符")

        newthread_url = (
            f"{self.base_url}/forum.php?mod=post&action=newthread&fid={self.forum_fid}"
        )
        r = self.session.get(newthread_url)
        r.raise_for_status()

        if "绑定手机号" in r.text:
            raise RuntimeError("MCBBS 要求绑定手机号，请先在网页端完成绑定")

        uid = re.search(r"discuz_uid\s*=\s*'(\d+)'", r.text)
        if not uid or uid.group(1) == "0":
            raise RuntimeError("发帖前验证失败：未登录")

        post_formhash = extract_formhash(r.text)
        post_url = (
            f"{self.base_url}/forum.php?mod=post&action=newthread"
            f"&fid={self.forum_fid}&extra=&topicsubmit=yes"
        )
        data = {
            "formhash": post_formhash,
            "posttime": str(int(time.time())),
            "wysiwyg": "1",
            "subject": title,
            "message": message,
            "usesig": "1",
            "allownoticeauthor": "1",
        }
        if sortid is not None:
            data["typeid"] = str(sortid)
        if attachment_ids:
            for aid in attachment_ids:
                data[f"attachnew[{aid}][description]"] = ""

        self.session.headers.update({"Referer": newthread_url})
        r = self.session.post(post_url, data=data, allow_redirects=False)
        self.session.headers.pop("Referer", None)
        r.raise_for_status()

        # 提取帖子 URL
        js_match = re.search(
            r"window\.location\.href\s*=\s*'([^']+thread-\d+-1-1[^']*)'", r.text
        )
        if js_match:
            relative_url = js_match.group(1)
            full_url = f"{self.base_url}/{relative_url.lstrip('./')}"
            if "需要审核" in r.text:
                print(f"    ✓ 发帖成功（需审核）: {full_url}")
            else:
                print(f"    ✓ 发帖成功: {full_url}")
            return full_url

        for source in (r.headers.get("Location", ""), r.url):
            tid_match = re.search(r"thread-(\d+)-1-1", source)
            if tid_match:
                full_url = f"{self.base_url}/thread-{tid_match.group(1)}-1-1.html"
                print(f"    ✓ 发帖成功: {full_url}")
                return full_url

        if "需要审核" in r.text or "通过审核" in r.text:
            print("    ✓ 发帖成功（需审核）")
            return "(需审核)"

        err = re.search(r'id="messagetext"[^>]*>(.*?)</div>', r.text, re.S)
        if err:
            raise RuntimeError(f"发帖失败: {re.sub(r'<[^>]+>', '', err.group(1)).strip()[:200]}")

        raise RuntimeError(f"发帖结果不明: {r.url}")

    def _apply_highlight(self, thread_url: str, highlight_color: int) -> bool:
        """对已发帖子应用高亮（Discuz topicadmin 管理操作）

        需要版主/管理员权限。通过 topicadmin moderate 端点提交高亮请求。
        """
        if not self.session:
            return False
        if highlight_color <= 0:
            return False

        tid_match = re.search(r'thread-(\d+)-', thread_url)
        if not tid_match:
            print("    ⚠ 无法提取帖子 ID，跳过高亮")
            return False
        tid = tid_match.group(1)

        try:
            # 确保 formhash 有效
            if not self.formhash:
                r = self.session.get(f"{self.base_url}/forum.php")
                r.raise_for_status()
                self.formhash = extract_formhash(r.text)

            # 通过 topicadmin 获取高亮表单
            moderate_url = (
                f"{self.base_url}/forum.php?mod=topicadmin&action=moderate"
                f"&fid={self.forum_fid}&tid={tid}&optgroup=1&infloat=yes&inajax=1"
            )
            referer = f"{self.base_url}/thread-{tid}-1-1.html"
            self.session.headers.update({"Referer": referer, "X-Requested-With": "XMLHttpRequest"})

            r_get = self.session.get(moderate_url)
            self.session.headers.pop("X-Requested-With", None)

            # 检查是否有权限
            if "没有权限" in r_get.text or "alert_error" in r_get.text:
                err_m = re.search(r'class="alert_error">([^<]+)', r_get.text)
                err_msg = err_m.group(1).strip() if err_m else "无管理权限"
                print(f"    ⚠ 高亮失败: {err_msg}")
                self.session.headers.pop("Referer", None)
                return False

            # 从返回的表单中提取 formhash 和 action URL
            form_action = re.search(
                r'<form[^>]*action="([^"]*moderate[^"]*)"', r_get.text
            )
            form_formhash = re.search(
                r'name="formhash"\s+value="([a-f0-9]+)"', r_get.text
            )
            if not form_action or not form_formhash:
                print("    ⚠ 无法提取高亮表单参数")
                self.session.headers.pop("Referer", None)
                return False

            action_url = form_action.group(1).replace('&amp;', '&')
            if action_url.startswith('./'):
                action_url = action_url[2:]
            full_action_url = f"{self.base_url}/{action_url}"

            # 提交高亮
            post_data = {
                "formhash": form_formhash.group(1),
                "fid": str(self.forum_fid),
                "operation": "highlight",
                "optgroup": "1",
                "highlight_color": str(highlight_color),
                "modsubmit": "yes",
                "redirect": f"{self.base_url}/./thread-{tid}-1-1.html",
                "handlekey": "mods",
            }

            r_post = self.session.post(full_action_url, data=post_data)
            self.session.headers.pop("Referer", None)

            # 检查结果
            resp = r_post.text
            if "succeedhandle" in resp or "成功" in resp or r_post.status_code in (200, 301, 302):
                return True
            if "没有权限" in resp:
                print("    ⚠ 高亮失败: 账号无管理权限")
                return False
            # Discuz AJAX 成功响应通常是 XML
            if "<root>" in resp and "errorhandle" not in resp:
                return True
            print(f"    ⚠ 高亮响应异常 (HTTP {r_post.status_code}): {resp[:200]}")
            return False
        except Exception as e:
            print(f"    ⚠ 高亮失败: {e}")
            return False

    def post_news_file(self, stem: str, txt_path: str, json_path: str,
                       news_dir: str, no_image: bool = False,
                       attach_json: bool = True) -> str:
        """
        发布单个新闻文件（txt + json + 可选图片 + 可选 JSON 附件）

        Args:
            attach_json: 是否将 JSON 文件作为附件上传并插入下载链接

        Returns:
            帖子 URL
        """
        # 加载元数据
        with open(json_path, encoding="utf-8") as f:
            meta = json.load(f)
        title = meta.get("translated_title", "").strip()
        if not title:
            title = meta.get("title", "").strip()
        if not title:
            raise ValueError(f"{json_path} 中找不到 title 字段")
        title = title + " [AI翻译]"

        with open(txt_path, encoding="utf-8") as f:
            message = f.read().strip()
        if not message:
            raise ValueError(f"{txt_path} 内容为空")

        # 上传图片
        attachment_ids = []
        if not no_image:
            img_path = find_image(news_dir, stem)
            if img_path:
                try:
                    aid = self.upload_image(img_path)
                    message = insert_image_bbcode(message, aid)
                    attachment_ids.append(aid)
                except Exception as e:
                    print(f"    ⚠ 图片上传失败，继续无图发帖: {e}")

        # 上传 JSON 附件并追加到正文末尾
        if attach_json and os.path.exists(json_path):
            try:
                json_aid = self.upload_file(json_path, mime_type="text/plain")  # Discuz 不接受 application/json
                message += f"\n\n[attach]{json_aid}[/attach]"
                attachment_ids.append(json_aid)
            except Exception as e:
                print(f"    ⚠ JSON 附件上传失败，跳过: {e}")

        # 检测分类（同时检测英文和中文标题，避免关键词丢失）
        original_title = meta.get("title", "").strip()
        # 先用英文标题检测
        module_type = detect_module_type(message, original_title) if original_title else None
        # 如果英文标题检测失败，再用中文标题检测
        if not module_type:
            module_type = detect_module_type(message, title)
        # 如果无法检测到类型，使用 "normal" 作为默认分类
        if not module_type:
            module_type = "normal"
        sortid = self.sortid_map.get(module_type)
        if sortid:
            cat_name = _CATEGORY_GROUP.get(module_type, "未知")
            print(f"    分类: {cat_name} (sortid={sortid})")
        else:
            print(f"    ⚠ 警告: 未找到分类 {module_type} 的 sortid 配置")

        post_url = self.post_thread(title, message, attachment_ids=attachment_ids, sortid=sortid)

        # 发帖后自动高亮
        if post_url and post_url.startswith("http"):
            # 用原标题检测新闻类型用于高亮（英文优先，中文补充）
            from utils import classify_article_type
            news_type = None
            if original_title:
                news_type = classify_article_type(original_title, chinese=False, fallback=None)
            if not news_type:
                news_type = classify_article_type(title, chinese=True, fallback=None)
            highlight_color = HIGHLIGHT_COLOR_MAP.get(news_type, 0) if news_type else 0
            if highlight_color:
                color_names = {1: "红色(正式版)", 2: "橙色(主机)", 4: "绿色(快照/测试版)", 6: "蓝色(周边)", 8: "粉色(快讯)"}
                print(f"    高亮: {color_names.get(highlight_color, str(highlight_color))}")
                if self._apply_highlight(post_url, highlight_color):
                    print("    ✓ 高亮设置成功！")
                else:
                    print("    ⚠ 高亮设置失败（不影响发帖）")

        return post_url


# ── 状态管理 ─────────────────────────────────────────

def load_posted(state_file: str) -> set:
    if os.path.exists(state_file):
        with open(state_file) as f:
            return set(json.load(f))
    return set()


def save_posted(state_file: str, posted: set):
    with open(state_file, "w") as f:
        json.dump(sorted(posted), f, ensure_ascii=False, indent=2)


# ── CLI 入口 ─────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="MCBBS 新闻自动发帖（含图片上传）")
    parser.add_argument("files", nargs="*", help="指定要发布的文件（stem）")
    parser.add_argument("--dry-run", action="store_true", help="预览模式")
    parser.add_argument("--dir", default="./output", help="新闻目录")
    parser.add_argument("--no-image", action="store_true", help="跳过图片上传")
    parser.add_argument("--no-json", action="store_true", help="跳过 JSON 附件上传")
    parser.add_argument("--fid", type=int, help="覆盖版块 ID")
    args = parser.parse_args()

    config = load_poster_config()
    if args.fid is not None:
        config["forum_fid"] = args.fid

    news_dir = args.dir
    state_file = os.path.join(news_dir, ".posted.json")

    if not config["username"] or not config["password"]:
        print("[!] 未配置 MCBBS 账号密码！请设置 config.json 中的 mcbbs 部分")
        sys.exit(1)

    # 扫描新闻文件
    if not os.path.isdir(news_dir):
        print(f"[!] 新闻目录不存在: {news_dir}")
        sys.exit(1)

    all_news = []
    for txt_path in sorted(glob.glob(os.path.join(news_dir, "*.txt"))):
        stem = os.path.splitext(os.path.basename(txt_path))[0]
        json_path = os.path.join(news_dir, stem + ".json")
        if os.path.exists(json_path):
            all_news.append((stem, txt_path, json_path))
        else:
            print(f"    ⚠ 跳过 {stem}: 找不到 .json")

    if not all_news:
        print("[!] 没有找到新闻文件")
        sys.exit(0)

    posted = load_posted(state_file)
    if args.files:
        pending = [(s, t, j) for s, t, j in all_news if s in set(args.files)]
    else:
        pending = [(s, t, j) for s, t, j in all_news if s not in posted]

    if not pending:
        print("[!] 没有需要发布的新闻")
        sys.exit(0)

    print(f"[*] 待发布: {len(pending)} 个")

    if args.dry_run:
        for stem, txt_path, json_path in pending:
            with open(json_path, encoding="utf-8") as f:
                meta = json.load(f)
            title = meta.get("title") or meta.get("translated_title", "")
            with open(txt_path, encoding="utf-8") as f:
                message = f.read().strip()
            img_path = find_image(news_dir, stem)
            module_type = detect_module_type(message, title)
            cat = _CATEGORY_GROUP.get(module_type, "未分类") if module_type else "未分类"
            print(f"\n  文件: {stem}")
            print(f"  标题: {title}")
            print(f"  分类: {cat}")
            print(f"  图片: {'✓ ' + os.path.basename(img_path) if img_path else '✗ 无'}")
        print(f"\n[Dry Run] 共 {len(pending)} 个待发布")
        sys.exit(0)

    poster = MCBBSPoster(config)
    try:
        poster.login()
    except Exception as e:
        print(f"\n[!] 登录失败: {e}", file=sys.stderr)
        sys.exit(1)

    success = 0
    failed = 0
    for stem, txt_path, json_path in pending:
        try:
            print(f"\n[*] 发布: {stem}")
            poster.post_news_file(stem, txt_path, json_path, news_dir,
                                 no_image=args.no_image, attach_json=not args.no_json)
            posted.add(stem)
            save_posted(state_file, posted)
            success += 1
            time.sleep(2)
        except Exception as e:
            print(f"    ✗ 发布失败: {e}")
            failed += 1

    print(f"\n{'=' * 50}")
    print(f"  完成！成功: {success}, 失败: {failed}")
    print(f"{'=' * 50}")
    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
