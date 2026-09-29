# -*- coding: utf-8 -*-
"""微信公众号草稿箱集成：access_token 缓存 + 封面素材上传 + Markdown→微信HTML + draft/add。

为什么放柳如烟后端而非 ai-quant 工作流引擎：access_token 有 2h 有效期，需常驻进程缓存复用；
"推送到草稿箱"是用户看完终稿后的人工动作，按钮触发比引擎自动发更可控；密钥只落柳如烟侧。

账号能力边界（2026 现状）：个人订阅号即可调用草稿箱接口（draft/add）；2025-07 起被回收的是
"自动群发/发布"(freepublish 系列)，不影响"入草稿箱"。最后一步发布由用户在「公众号助手」App /
公众号后台手动完成。
"""
from __future__ import annotations

import os
import re
import json
import time
import base64
import hashlib
import tempfile
import html as _html
import threading
from pathlib import Path
from typing import Optional

import requests

from backend.paths import wechat_mp_config_path

_API = "https://api.weixin.qq.com/cgi-bin"

# access_token 进程内缓存（2h 有效，必须复用；多账号按 appid 分别缓存）
_TOKEN_CACHE: dict = {}   # appid -> {"token": str, "expires_at": float}
_TOKEN_LOCK = threading.Lock()

# 永久素材 media_id 长期有效，按解析后的绝对路径缓存，避免重复上传同一封面
_THUMB_CACHE: dict = {}

# 常见错误码 → 中文解释（透传前端，帮用户自助排查）
_ERR_HINTS = {
    40001: "AppSecret 错误或 access_token 失效（检查 wechat_mp.json 里的 secret）",
    40013: "AppID 无效（检查 wechat_mp.json 里的 appid）",
    40164: "调用 IP 不在白名单：到微信开发者平台 developers.weixin.qq.com/platform → 我的业务 → 公众号 → 基础信息 → 开发密钥 → API IP 白名单，加入本机公网 IP",
    41001: "缺少 access_token",
    45009: "接口调用频率超限，请稍后再试",
    45028: "草稿数量已达上限",
    48001: "接口未授权：确认账号类型，个人订阅号草稿箱接口一般可用",
    53600: "草稿内容不合规或必填字段缺失",
}


class WeChatError(Exception):
    """微信接口/配置错误，携带可选 errcode 供前端展示。"""

    def __init__(self, message: str, errcode: Optional[int] = None):
        super().__init__(message)
        self.errcode = errcode
        self.message = message


def _load_config() -> dict:
    """读凭证：环境变量优先，wechat_mp.json 兜底。

    返回 {appid, secret, default_thumb_media_id, default_thumb_path}。
    """
    cfg = {
        "appid": os.getenv("WECHAT_APPID", "").strip(),
        "secret": os.getenv("WECHAT_SECRET", "").strip(),
        "default_thumb_media_id": os.getenv("WECHAT_DEFAULT_THUMB_MEDIA_ID", "").strip(),
        "default_thumb_path": os.getenv("WECHAT_DEFAULT_THUMB_PATH", "").strip(),
    }
    path = wechat_mp_config_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            for k in cfg:
                if not cfg[k] and data.get(k):
                    cfg[k] = str(data[k]).strip()
        except Exception:
            pass
    return cfg


def is_configured() -> bool:
    """是否已配置 appid + secret（前端据此提示用户先填密钥）。"""
    cfg = _load_config()
    return bool(cfg["appid"] and cfg["secret"])


def _check_errcode(data: dict) -> None:
    code = data.get("errcode", 0)
    if code and code != 0:
        hint = _ERR_HINTS.get(code) or data.get("errmsg", "未知错误")
        raise WeChatError(f"微信接口错误 {code}：{hint}", errcode=code)


def get_access_token(appid: str = "", secret: str = "", force: bool = False) -> str:
    """换取 access_token。传 appid/secret 用指定账号；否则回退全局配置。按 appid 分别缓存。"""
    if not appid or not secret:
        cfg = _load_config()
        appid = appid or cfg["appid"]
        secret = secret or cfg["secret"]
    if not appid or not secret:
        raise WeChatError("未配置公众号 AppID/AppSecret（账号未填，或全局 wechat_mp.json/环境变量未配）")
    with _TOKEN_LOCK:
        now = time.time()
        cached = _TOKEN_CACHE.get(appid)
        if not force and cached and now < cached["expires_at"]:
            return cached["token"]
        try:
            r = requests.get(
                f"{_API}/token",
                params={"grant_type": "client_credential", "appid": appid, "secret": secret},
                timeout=10,
            )
            data = r.json()
        except Exception as e:
            raise WeChatError(f"获取 access_token 网络失败：{e}")
        _check_errcode(data)
        token = data.get("access_token", "")
        if not token:
            raise WeChatError("获取 access_token 失败：返回为空")
        # 提前 5 分钟过期，避免边界失效
        _TOKEN_CACHE[appid] = {"token": token, "expires_at": now + int(data.get("expires_in", 7200)) - 300}
        return token


def upload_image_material(image_path: str, media_type: str = "image",
                          appid: str = "", secret: str = "") -> str:
    """上传永久图片素材，返回 media_id（封面 thumb 用）。"""
    p = Path(image_path)
    if not p.exists():
        raise WeChatError(f"封面图不存在：{image_path}")
    key = f"{appid}:{p.resolve()}"
    if key in _THUMB_CACHE:
        return _THUMB_CACHE[key]
    token = get_access_token(appid, secret)
    try:
        with open(p, "rb") as f:
            r = requests.post(
                f"{_API}/material/add_material",
                params={"access_token": token, "type": media_type},
                files={"media": (p.name, f)},
                timeout=30,
            )
        data = r.json()
    except Exception as e:
        raise WeChatError(f"上传封面素材网络失败：{e}")
    _check_errcode(data)
    media_id = data.get("media_id", "")
    if not media_id:
        raise WeChatError("上传封面素材失败：未返回 media_id")
    _THUMB_CACHE[key] = media_id
    return media_id


def upload_image_from_url(src: str, appid: str = "", secret: str = "") -> str:
    """从图片 URL（http）或 data:image;base64 取字节，上传公众号永久素材，返回 media_id。

    用于"把工作台生成的封面图设成本篇公众号封面"——生成图是 URL/base64，需先落临时文件再上传。
    """
    if not src:
        raise WeChatError("封面图来源为空")
    if src.startswith("data:"):
        try:
            header, b64 = src.split(",", 1)
            raw = base64.b64decode(b64)
            ext = ".jpg" if ("jpeg" in header or "jpg" in header) else (".gif" if "gif" in header else ".png")
        except Exception as e:
            raise WeChatError(f"解析封面 data 失败：{e}")
    else:
        try:
            r = requests.get(src, timeout=30)
            r.raise_for_status()
            raw = r.content
        except Exception as e:
            raise WeChatError(f"下载封面图失败：{e}")
        ext = os.path.splitext(src.split("?")[0])[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".gif"):
            ext = ".jpg"
    # 按内容 hash + 账号缓存：同一张图在同一个号只上传一次，复用 media_id（避免素材库堆重复图）
    key = f"{appid}:sha256:{hashlib.sha256(raw).hexdigest()[:16]}"
    if key in _THUMB_CACHE:
        return _THUMB_CACHE[key]
    fd, tmp = tempfile.mkstemp(suffix=ext, prefix="wxcover_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        media_id = upload_image_material(tmp, "image", appid, secret)
        _THUMB_CACHE[key] = media_id
        return media_id
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


def _resolve_thumb(thumb_media_id: Optional[str], cfg: dict,
                   appid: str = "", secret: str = "") -> str:
    """封面来源优先级：请求显式 > 配置 media_id > 配置本地图片(自动上传)。"""
    if thumb_media_id:
        return thumb_media_id
    if cfg.get("default_thumb_media_id"):
        return cfg["default_thumb_media_id"]
    if cfg.get("default_thumb_path"):
        return upload_image_material(cfg["default_thumb_path"], "image", appid, secret)
    raise WeChatError(
        "公众号图文草稿必须有封面图：请在 wechat_mp.json 配 default_thumb_media_id，"
        "或配 default_thumb_path（本地图片，会自动上传成素材），或在请求里传 thumb_media_id"
    )


def _inline(s: str) -> str:
    """行内标记：转义 + 粗体 + 行内代码（不含样式，样式由 _apply_inline_styles 统一注入）。"""
    s = _html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    return s


# ── 公众号 inline style 模板（按写作风格选视觉调性）──

_STYLE_TEMPLATES: dict = {
    "default": {
        "h1": "font-size:22px;font-weight:700;margin:24px 0 12px;color:#333;",
        "h2": "font-size:20px;font-weight:700;margin:24px 0 10px;color:#333;"
              "border-left:4px solid #07c160;padding-left:12px;",
        "h3": "font-size:18px;font-weight:700;margin:20px 0 8px;color:#555;",
        "h4": "font-size:16px;font-weight:700;margin:16px 0 6px;color:#666;",
        "p": "margin:0 0 18px;font-size:16px;line-height:1.8;color:#3f3f3f;letter-spacing:0.5px;",
        "blockquote": "margin:18px 0;padding:14px 20px;background:#f7f7f7;"
                      "border-left:4px solid #ddd;font-size:15px;color:#888;border-radius:0 4px 4px 0;",
        "pre": "margin:18px 0;padding:16px;background:#282c34;border-radius:5px;overflow-x:auto;",
        "code": "font-size:14px;color:#abb2bf;",
        "inline_code": "background:#fff5f5;color:#fa755e;padding:2px 6px;border-radius:3px;font-size:14px;",
        "ul": "margin:0 0 18px;padding-left:24px;font-size:16px;line-height:1.8;color:#3f3f3f;",
        "ol": "margin:0 0 18px;padding-left:24px;font-size:16px;line-height:1.8;color:#3f3f3f;",
        "li": "margin:4px 0;",
        "a": "color:#07c160;text-decoration:none;",
        "strong": "font-weight:700;color:#333;",
        "hr": "border:none;border-top:1px solid #e5e5e5;margin:28px 0;",
        "img": "max-width:100%;border-radius:4px;margin:18px auto;display:block;",
    },
    "liurun": {
        "h1": "font-size:22px;font-weight:700;margin:28px 0 12px;color:#2b2b2b;",
        "h2": "font-size:20px;font-weight:700;margin:28px 0 10px;color:#2b2b2b;"
              "border-left:3px solid #999;padding-left:12px;",
        "h3": "font-size:18px;font-weight:700;margin:22px 0 8px;color:#444;",
        "h4": "font-size:16px;font-weight:700;margin:16px 0 6px;color:#555;",
        "p": "margin:0 0 16px;font-size:16px;line-height:1.75;color:#3f3f3f;letter-spacing:0.3px;",
        "blockquote": "margin:18px 0;padding:12px 20px;background:#fafafa;"
                      "border-left:3px solid #ccc;font-size:15px;color:#666;",
        "pre": "margin:18px 0;padding:16px;background:#f5f5f5;border-radius:3px;"
               "overflow-x:auto;border:1px solid #e0e0e0;",
        "code": "font-size:14px;color:#333;",
        "inline_code": "background:#f0f0f0;color:#333;padding:2px 6px;border-radius:2px;font-size:14px;",
        "ul": "margin:0 0 16px;padding-left:22px;font-size:16px;line-height:1.75;color:#3f3f3f;",
        "ol": "margin:0 0 16px;padding-left:22px;font-size:16px;line-height:1.75;color:#3f3f3f;",
        "li": "margin:3px 0;",
        "a": "color:#576b95;text-decoration:none;",
        "strong": "font-weight:700;color:#2b2b2b;",
        "hr": "border:none;border-top:1px solid #e0e0e0;margin:28px 0;",
        "img": "max-width:100%;border-radius:2px;margin:18px auto;display:block;",
    },
    "khazix": {
        "h1": "font-size:22px;font-weight:700;margin:24px 0 10px;color:#333;",
        "h2": "font-size:20px;font-weight:700;margin:24px 0 10px;color:#333;line-height:1.4;",
        "h3": "font-size:18px;font-weight:700;margin:20px 0 6px;color:#555;",
        "h4": "font-size:16px;font-weight:700;margin:14px 0 4px;color:#666;",
        "p": "margin:0 0 15px;font-size:16px;line-height:1.85;color:#3a3a3a;letter-spacing:0.3px;",
        "blockquote": "margin:16px 0;padding:12px 18px;background:#fffbf5;"
                      "border-left:3px solid #f0a050;font-size:15px;color:#7a6a55;border-radius:0 4px 4px 0;",
        "pre": "margin:16px 0;padding:16px;background:#2d2d2d;border-radius:5px;overflow-x:auto;",
        "code": "font-size:14px;color:#e6e6e6;",
        "inline_code": "background:#fff3e8;color:#c8770f;padding:2px 6px;border-radius:3px;font-size:14px;",
        "ul": "margin:0 0 15px;padding-left:22px;font-size:16px;line-height:1.85;color:#3a3a3a;",
        "ol": "margin:0 0 15px;padding-left:22px;font-size:16px;line-height:1.85;color:#3a3a3a;",
        "li": "margin:3px 0;",
        "a": "color:#f07a2c;text-decoration:none;",
        "strong": "font-weight:700;color:#333;",
        "hr": "border:none;border-top:1px solid #eee;margin:24px 0;",
        "img": "max-width:100%;border-radius:6px;margin:16px auto;display:block;",
    },
}


def _preprocess_text(text: str) -> str:
    """终稿预处理：去发布检查尾行、压缩空行、trim 行尾、标题前后补空行。"""
    text = re.sub(r"\n*发布检查[:：].*均已过.*$", "", text, flags=re.S)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [ln.rstrip() for ln in text.splitlines()]
    text = "\n".join(lines)
    text = re.sub(r"([^\n])\n(#{1,6}\s)", r"\1\n\n\2", text)
    text = re.sub(r"(#{1,6}\s[^\n]+)\n([^\n#])", r"\1\n\n\2", text)
    return text.strip()


def _md_to_html_basic(text: str) -> str:
    """无 markdown 库时的手写转换（输出无样式 HTML，样式由 _apply_inline_styles 注入）。"""
    lines = text.replace("\r\n", "\n").split("\n")
    out: list = []
    list_buf: list = []
    list_type: Optional[str] = None
    in_code = False
    code_buf: list = []

    def flush_list() -> None:
        nonlocal list_buf, list_type
        if list_buf:
            items = "".join(f"<li>{x}</li>" for x in list_buf)
            tag = list_type or "ul"
            out.append(f"<{tag}>{items}</{tag}>")
            list_buf = []
            list_type = None

    def flush_code() -> None:
        nonlocal code_buf, in_code
        if code_buf:
            code = _html.escape("\n".join(code_buf))
            out.append(f"<pre><code>{code}</code></pre>")
        code_buf = []
        in_code = False

    for raw in lines:
        line = raw.rstrip()
        if line.strip().startswith("```"):
            if in_code:
                flush_code()
            else:
                flush_list()
                in_code = True
            continue
        if in_code:
            code_buf.append(raw)
            continue
        if not line.strip():
            flush_list()
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush_list()
            level = min(len(m.group(1)), 4)
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            continue
        m = re.match(r"^\s*[-*]\s+(.*)$", line)
        if m:
            if list_type not in (None, "ul"):
                flush_list()
            list_type = "ul"
            list_buf.append(_inline(m.group(1)))
            continue
        m = re.match(r"^\s*\d+\.\s+(.*)$", line)
        if m:
            if list_type not in (None, "ol"):
                flush_list()
            list_type = "ol"
            list_buf.append(_inline(m.group(1)))
            continue
        flush_list()
        out.append(f"<p>{_inline(line)}</p>")
    flush_list()
    if in_code:
        flush_code()
    return "\n".join(out) or "<p></p>"


def _make_style_injector(style: str):
    """构造 regex 替换回调：给 HTML 标签注入 inline style（已有 style 的跳过）。"""
    def _inject(m) -> str:
        full = m.group(0)
        if "style=" in full:
            return full
        if full.endswith("/>"):
            return full[:-2].rstrip() + f' style="{style}"/>'
        return full[:-1].rstrip() + f' style="{style}">'
    return _inject


def _apply_inline_styles(html: str, tpl: dict) -> str:
    """给 HTML 标签批量注入 inline style（公众号不加载外部 CSS）。

    先暂存 pre 块（避免内部 code 被当 inline code）→ 各标签加 style → 还原 pre 块。
    """
    pre_blocks: list = []

    def _stash(m) -> str:
        pre_blocks.append(m.group(0))
        return f"__PRE_{len(pre_blocks) - 1}__"

    html = re.sub(r"<pre[^>]*>.*?</pre>", _stash, html, flags=re.S)

    for tag, key in [("h1", "h1"), ("h2", "h2"), ("h3", "h3"), ("h4", "h4"),
                     ("p", "p"), ("blockquote", "blockquote"),
                     ("ul", "ul"), ("ol", "ol"), ("li", "li"),
                     ("code", "inline_code"), ("strong", "strong"),
                     ("a", "a"), ("hr", "hr"), ("img", "img")]:
        style = tpl.get(key, "")
        if style:
            html = re.sub(rf"<{tag}\b[^>]*>", _make_style_injector(style), html)

    for i, block in enumerate(pre_blocks):
        pre_style = tpl.get("pre", "")
        code_style = tpl.get("code", "")
        styled = re.sub(r"<pre(\s[^>]*)?>", f'<pre style="{pre_style}">', block, count=1)
        styled = re.sub(r"<code(\s[^>]*)?>", f'<code style="{code_style}">', styled, count=1)
        html = html.replace(f"__PRE_{i}__", styled)

    return html


def markdown_to_wechat_html(text: str, style: str = "") -> str:
    """Markdown/纯文本 → 微信兼容 HTML（inline style，按写作风格选视觉模板）。

    优先用 markdown 库（覆盖表格/嵌套列表等完整语法）；未装则降级到手写解析器。
    样式不写在标签里而是统一注入，换风格只换模板不改解析逻辑。
    """
    if not text or not text.strip():
        return "<p></p>"
    cleaned = _preprocess_text(text)
    try:
        import markdown as md
        html = md.markdown(cleaned, extensions=["extra", "sane_lists"])
    except ImportError:
        html = _md_to_html_basic(cleaned)
    tpl = _STYLE_TEMPLATES.get(style) or _STYLE_TEMPLATES["default"]
    return _apply_inline_styles(html, tpl)


def _looks_like_html(text: str) -> bool:
    """判断 content 是否已是可直接进草稿的 HTML（避免对 layout.html 再做 MD 转换）。"""
    t = (text or "").lstrip()
    if not t.startswith("<"):
        return False
    low = t[:500].lower()
    return any(tag in low for tag in ("<section", "<p", "<div", "<h1", "<h2", "<article"))


def add_draft(title: str, content: str, author: str = "", digest: str = "",
              source_url: str = "", thumb_media_id: Optional[str] = None,
              appid: str = "", secret: str = "", style: str = "") -> dict:
    """新增一篇图文到草稿箱，返回 {"media_id": ...}。

    传 appid/secret 用指定账号；否则回退全局配置（向后兼容旧的全局单账号调用）。
    title ≤ 64 字、author ≤ 8 字、digest ≤ 120 字（超长截断，符合微信限制）。

    content 可为 Markdown（默认走 markdown_to_wechat_html），也可为工作台排版步
    产出的内联 HTML（以 <section>/<p>/… 开头）——已是 HTML 时原样使用，不再二次转换。
    """
    cfg = _load_config()
    eff_appid = appid or cfg["appid"]
    eff_secret = secret or cfg["secret"]
    token = get_access_token(eff_appid, eff_secret)
    thumb = _resolve_thumb(thumb_media_id, cfg, eff_appid, eff_secret)
    if _looks_like_html(content):
        html_content = content.strip()
    else:
        html_content = markdown_to_wechat_html(content, style=style)
    if not digest:
        plain = re.sub(r"<[^>]+>", "", html_content).strip()[:120]
        digest = plain
    article = {
        "title": (title or "无标题").strip()[:64],
        "author": (author or "").strip()[:8],
        "digest": digest.strip()[:120],
        "content": html_content,
        "content_source_url": (source_url or "").strip(),
        "thumb_media_id": thumb,
        "need_open_comment": 0,
        "only_fans_can_comment": 0,
    }
    payload = {"articles": [article]}
    try:
        r = requests.post(
            f"{_API}/draft/add",
            params={"access_token": token},
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=20,
        )
        data = r.json()
    except Exception as e:
        raise WeChatError(f"新增草稿网络失败：{e}")
    _check_errcode(data)
    media_id = data.get("media_id", "")
    if not media_id:
        raise WeChatError("新增草稿失败：未返回 media_id")
    return {"media_id": media_id}
