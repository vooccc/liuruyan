#!/usr/bin/env python3
"""
简化版文件工具 - 纯本地文件操作

删除了 Hermes 环境管理系统，只保留核心文件操作功能
"""

import contextvars
import errno
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .binary_extensions import has_binary_extension, image_media_type, is_image
from .file_diff import build_file_diff_payload

logger = logging.getLogger(__name__)

# ── working_directory ContextVar：caller（spawn_subagent 等）传入的
# "本次任务要分析的项目根目录"。用 ContextVar 是因为：
#   - 多个 subagent 并发跑，每个有自己的 working_directory
#   - SubAgentEngine / LLMClient 都是全局单例，绝不能在它们身上挂实例变量
#     （会被并发覆盖）—— 跟 _current_agent_id_var / _current_session_id_var 同款
# 默认 None：未设时 _resolve_read_path 退化到旧行为（agent_dir → cwd fallback）。
_current_working_directory_var: contextvars.ContextVar[Optional[str]] = (
    contextvars.ContextVar("liuruyan_current_working_directory", default=None)
)


def _current_working_dir() -> Optional[Path]:
    """读当前 ContextVar 里的 working_directory（caller 指定的项目根）。
    无设 / 路径不存在 / 不是目录 → 返 None。
    """
    raw = _current_working_directory_var.get()
    if not raw:
        return None
    try:
        p = Path(raw).expanduser().resolve()
        if p.exists() and p.is_dir():
            return p
    except Exception:
        return None
    return None

# 常量配置
_DEFAULT_MAX_READ_CHARS = 100_000
_READ_FILE_MAX_LINE_CHARS = 2_000
_LARGE_FILE_HINT_BYTES = 512_000
_EXPECTED_WRITE_ERRNOS = {errno.EACCES, errno.EPERM, errno.EROFS}

# 图片 inline 上限：5MB（D1 决策，handover_image_read_2026-05-11.md §5）。
# 超限直接拒，不引入 Pillow 做 resize（D2 决策）。
_IMAGE_INLINE_MAX_BYTES = 5 * 1024 * 1024
_IMAGE_INLINE_WARN_BYTES = 1 * 1024 * 1024

# PDF inline 上限：5MB（与图片对齐，2026-05-12 P3 方案）。
# Anthropic 官方 document block 上限 32MB / 100 pages，但我们保守先 5MB，
# 避免 base64 爆 token。超限时 native 路径拒、text 路径放行（抽文本）。
_PDF_INLINE_MAX_BYTES = 5 * 1024 * 1024


# ============================================================================
# 简化的本地终端环境
# ============================================================================

class LocalTerminalEnv:
    """简化的本地终端环境，替代 Hermes 的复杂环境管理"""

    def __init__(self, cwd: str = None):
        self.cwd = cwd or os.getcwd()

    def execute(self, command: str, cwd: str = None) -> dict:
        """执行 shell 命令"""
        work_dir = cwd or self.cwd
        run_kwargs: dict = {
            "shell": True,
            "cwd": work_dir,
            "capture_output": True,
            "text": True,
            "timeout": 30,
        }
        if os.name == "nt":
            run_kwargs["creationflags"] = 0x08000000
        try:
            result = subprocess.run(
                command,
                **run_kwargs,
            )
            return {
                "output": result.stdout + result.stderr,
                "returncode": result.returncode
            }
        except subprocess.TimeoutExpired:
            return {
                "output": "Command timed out",
                "returncode": 124
            }
        except Exception as e:
            return {
                "output": str(e),
                "returncode": 1
            }


# 全局本地环境实例
_local_env = LocalTerminalEnv()


# ============================================================================
# 路径安全检查
# ============================================================================

def _resolve_path(filepath: str) -> Path:
    """解析文件路径。

    相对路径默认锚到当前角色工作目录（``agents/<agent_id>/``），
    避免角色 ``write_file('agents/x/foo.svg')`` 时落到 backend cwd 下。
    ContextVar 没设 / 角色目录不存在 → 退回 cwd（保留老行为，兼容测试与脚本）。
    绝对路径维持原样，跨目录干活不做限制。
    """
    p = Path(filepath).expanduser()
    if not p.is_absolute():
        anchor = _current_agent_dir()
        if anchor is not None:
            p = anchor / p
    return p.resolve()


def _resolve_read_path(filepath: str) -> Path:
    """**只读路径**专用解析。

    解析优先级（从高到低）：
      1) 绝对路径：原样返
      2) agent_dir 锚点（角色私有文件）—— 命中即返
      3) **working_directory ContextVar**（caller 指定的目标项目根）—— 命中即返
         ↑ 2026-05-06 新增：解决"用柳如烟分析另一个项目时，subagent 相对路径
         被 cwd fallback 解到柳如烟自己 backend"的事故。
      4) cwd（后端进程启动目录）—— 旧 fallback，作为最后兜底保留兼容
      5) 都没命中：返 agent_dir 版让 _suggest_path 在 agent_dir 父目录给候选

    write 路径**不**能用这个：会让"模型本意 agents/x/foo.txt 但拼错只剩 foo.txt"的
    情况污染父级目录。读不会污染所以可放宽。
    """
    p = Path(filepath).expanduser()
    anchor = _current_agent_dir()
    working = _current_working_dir()

    def _log(chosen: Path, stage: str) -> None:
        # 默认开——路径解析事故是高频 bug，日志量不大（每次 read/search 一条）。
        # 想关就 LIURUYAN_FILE_RESOLVE_LOG=0。
        if os.environ.get("LIURUYAN_FILE_RESOLVE_LOG", "1") == "0":
            return
        try:
            logger.debug(
                "[FILE-RESOLVE] input=%r agent_dir=%r working_dir=%r cwd=%r stage=%s chosen=%r exists=%s",
                filepath,
                str(anchor) if anchor else None,
                str(working) if working else None,
                os.getcwd(),
                stage,
                str(chosen),
                chosen.exists(),
            )
        except Exception:
            pass

    if p.is_absolute():
        resolved = p.resolve()
        _log(resolved, "absolute")
        return resolved

    # 第一次：agent_dir 锚点（角色私有文件优先）
    agent_resolved = (anchor / p).resolve() if anchor is not None else p.resolve()
    if agent_resolved.exists():
        _log(agent_resolved, "agent_dir")
        return agent_resolved

    # 第二次：caller 指定的 working_directory（spawn_subagent 传过来的目标项目根）
    if working is not None:
        wd_resolved = (working / p).resolve()
        if wd_resolved.exists():
            _log(wd_resolved, "working_directory")
            return wd_resolved

    # 第三次（兜底）：cwd 锚点（后端进程启动目录）
    cwd_resolved = (Path.cwd() / p).resolve()
    if cwd_resolved.exists():
        _log(cwd_resolved, "cwd_fallback")
        return cwd_resolved

    # 都没命中：返 agent_dir 版让 _suggest_path 在 agent_dir 父目录给候选
    _log(agent_resolved, "miss_all")
    return agent_resolved


def _current_agent_dir() -> Optional[Path]:
    """读当前 agent 的工作目录。无 agent / 目录不存在返回 None。

    懒 import 避免与 llm_client 循环依赖。
    """
    try:
        from backend.llm_client import _current_agent_id_var  # noqa: WPS433
        from backend import paths  # noqa: WPS433

        agent_id = _current_agent_id_var.get()
        if not agent_id:
            return None
        candidate = paths.agents_dir() / agent_id
        return candidate if candidate.exists() else None
    except Exception:
        return None


def _is_blocked_device(filepath: str) -> bool:
    """检查是否是设备文件"""
    blocked_prefixes = ("/dev/", "/proc/", "/sys/")
    normalized = os.path.normpath(filepath)
    return any(normalized.startswith(prefix) for prefix in blocked_prefixes)


def _check_sensitive_path(filepath: str) -> Optional[str]:
    """检查敏感路径"""
    sensitive_patterns = [
        ".ssh/id_",
        ".aws/credentials",
        ".env",
        "secrets",
    ]
    normalized = os.path.normpath(filepath).lower()
    for pattern in sensitive_patterns:
        if pattern in normalized:
            return f"Sensitive path detected: {pattern}"
    return None


def _is_expected_write_exception(exc: Exception) -> bool:
    """检查是否是预期的写入异常"""
    if isinstance(exc, OSError):
        return exc.errno in _EXPECTED_WRITE_ERRNOS
    return False


# ============================================================================
# P1#2 自愈提示：read_file 找不到文件 / 命中目录时给 LLM 候选路径，避免凭空猜路径反复重试
# ============================================================================

def _suggest_path(resolved: Path, max_suggestions: int = 5) -> dict:
    """父目录下用 difflib 模糊匹配近似文件名，返回 {"hint": "...", "did_you_mean": [...]}.

    设计：
    - 失败必须静默：建议系统挂了不能让 read_file 主路径阻断（已经在报错路径上）
    - 父目录不存在时一路向上爬：处理 LLM 拼错中段目录的情况（如 srcc/foo.py）
    - 候选限制 5 条避免 hint 暴长
    """
    try:
        import difflib
        target_name = resolved.name
        parent = resolved.parent
        # 父目录不存在时向上爬，最多 3 层
        for _ in range(3):
            if parent.exists() and parent.is_dir():
                break
            if parent.parent == parent:
                break
            parent = parent.parent
        if not parent.exists() or not parent.is_dir():
            return {}
        try:
            candidates = [e.name for e in parent.iterdir()]
        except (PermissionError, OSError):
            return {}
        if not candidates:
            return {}
        matches = difflib.get_close_matches(
            target_name, candidates, n=max_suggestions, cutoff=0.5
        )
        if not matches:
            return {}
        suggestions = [str(parent / m) for m in matches]
        return {
            "did_you_mean": suggestions,
            "hint": f"File not found. Closest matches in {parent}: {', '.join(matches)}",
        }
    except Exception:
        return {}


def _suggest_dir(resolved: Path, max_children: int = 10) -> dict:
    """resolved 是目录时，列出前几个子项 + 给 LLM 改用 glob_search/grep 的提示."""
    try:
        if not resolved.is_dir():
            return {}
        children = []
        for i, entry in enumerate(resolved.iterdir()):
            if i >= max_children:
                break
            kind = "dir" if entry.is_dir() else "file"
            children.append(f"{entry.name} ({kind})")
        if not children:
            children = ["(empty)"]
        return {
            "is_directory": True,
            "children_preview": children,
            "hint": (
                f"'{resolved}' is a directory, not a file. "
                f"Use glob_search / grep to scan, or pass a specific child file path."
            ),
        }
    except Exception:
        return {}


# ============================================================================
# 核心工具函数
# ============================================================================

def _read_image_as_blocks(orig_path: str, resolved: Path) -> str:
    """读图片 → 返回 JSON 字符串，内嵌 `_content_blocks` 标记位让 llm_client 在
    拼装 tool_result_msgs 时把 content 替换为 LLM content blocks list（含 image block）。

    返回协议见 _docs/handover_image_read_2026-05-11.md §3.1.2。
    Size cap = 5MB（D1），超限直接拒（D2 不 resize）。
    """
    import base64 as _b64

    media_type = image_media_type(str(resolved))
    if media_type is None:
        # 防御：is_image 通过但 media_type 没拿到，理论不该发生
        return json.dumps({"error": f"Cannot determine media type for '{orig_path}'"})

    if not resolved.exists():
        return json.dumps({"error": f"File not found: {orig_path}", **_suggest_path(resolved)})
    if not resolved.is_file():
        return json.dumps({"error": f"Not a file: {orig_path}"})

    try:
        size = resolved.stat().st_size
    except OSError as e:
        return json.dumps({"error": f"Cannot stat '{orig_path}': {e}"})

    if size > _IMAGE_INLINE_MAX_BYTES:
        size_mb = size / (1024 * 1024)
        cap_mb = _IMAGE_INLINE_MAX_BYTES / (1024 * 1024)
        return json.dumps({
            "error": (
                f"Image too large to inline ({size_mb:.1f} MB > {cap_mb:.0f} MB cap). "
                f"Resize or use a smaller version."
            )
        })

    if size > _IMAGE_INLINE_WARN_BYTES:
        logger.warning(
            "[read_file:image] inline-large path=%s size_bytes=%d", str(resolved), size
        )

    try:
        with open(resolved, "rb") as f:
            raw = f.read()
    except OSError as e:
        return json.dumps({"error": f"Cannot read image '{orig_path}': {e}"})

    b64 = _b64.b64encode(raw).decode("ascii")
    # 注意：这段 text 会被同时塞进 list[block]，伴随 image block 一起送给 LLM。
    # 不要写"Shown inline to the model"这种确定性话术——在 OpenAI 兼容协议下
    # （讯飞 / deepseek 等无 vision 路径）text 会保留但 image 被降级为占位符，
    # 这种强自信话术会让模型误以为自己看见了图，反而瞎编。
    summary_text = (
        f"Image attachment: {resolved} ({media_type}, {size} bytes)."
    )

    payload = {
        # _content_blocks 是约定标记位：llm_client 在拼装 tool_result_msgs 时
        # 检测到这个 key 就把 content 替换为这里的 list[block]（不 stringify）
        "_content_blocks": [
            {"type": "text", "text": summary_text},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": b64,
                },
            },
        ],
        # _meta 是给前端 / 日志看的精简版（不含 base64）
        "_meta": {
            "path": str(resolved),
            "size_bytes": size,
            "media_type": media_type,
        },
    }
    return json.dumps(payload, ensure_ascii=False)


# ============================================================================
# PDF 读取（2026-05-12 P3 方案 + Q1/Q2 升级）
# ============================================================================
#
# 双路径设计（P3）：
# - native：Anthropic 原生路径（_provider_supports_pdf_native_var=True）→
#   PDF base64 打包成 document block 给 Claude 自己解，质量最高。
# - text：OpenAI / 伪 Anthropic 反代 → pypdf / pdfplumber 抽文本 → cat -n 格式，
#   所有 provider 都能消费。扫描版 PDF 这条路抽不到文本 → 友好错误引导切
#   Claude（native 路径靠 Claude 视觉才能读）或本地 ocrmypdf。本地 OCR 引擎不打包。
#
# 可选参数（Q1/Q2 升级，2026-05-12）：
# - pages: list[int]（1-indexed）→ 只读指定页。pypdf/pdfplumber 路径过滤；
#   native 路径在 summary_text 加 hint，Claude 看完整 PDF 但被引导关注哪几页
#   （不真过滤——重打包 PDF 风险大且不必要）。
# - extractor: "pypdf"（默认，快）或 "pdfplumber"（慢但表格/复杂版式质量高，
#   会把表格转成 markdown 嵌入文本流）。
#
# 入口：_read_pdf。它读 ContextVar 后分发到 _read_pdf_native_blocks /
# _read_pdf_text。如果调用者在脱离 LLM 调用上下文（CLI 脚本 / 单测）调
# read_file_tool，ContextVar 为默认 False → 走 text 路径，这也是用户默认体验。

def _normalize_pdf_pages(
    pages: Any,
) -> Tuple[Optional[List[int]], Optional[str]]:
    """归一 pages 参数。返 (pages_normalized, error_msg_or_None)。

    None / [] → 视为"读全文"（return None, None）。
    list[int] 且全部 ≥ 1 → 排序去重返。
    其他形态 → 返 error_msg。
    """
    if pages is None:
        return None, None
    if not isinstance(pages, list):
        return None, f"Invalid pages parameter: {pages!r}; expected list[int] (1-indexed)"
    if not pages:
        return None, None
    try:
        normalized = sorted({int(p) for p in pages})
    except (ValueError, TypeError):
        return None, f"Invalid pages parameter: {pages!r}; expected list[int] (1-indexed)"
    if any(p < 1 for p in normalized):
        return None, (
            f"Invalid pages parameter: pages must be >= 1 (1-indexed), got {pages!r}"
        )
    return normalized, None


def _read_pdf(
    orig_path: str,
    resolved: Path,
    offset: int = 1,
    limit: int = 2000,
    pages: Optional[List[int]] = None,
    extractor: str = "pypdf",
) -> str:
    """PDF 入口路由函数。

    读 _provider_supports_pdf_native_var：
    - True → _read_pdf_native_blocks（pages 当 hint，Claude 看完整 PDF）
    - False → _read_pdf_text（pages/extractor 实际生效，按选定页和提取器抽文本）
    """
    try:
        from backend.llm_client import _provider_supports_pdf_native_var
        is_native = _provider_supports_pdf_native_var.get()
    except Exception:
        # 防御：var import 失败（罕见）→ 走 text 路径兜底
        is_native = False

    if is_native:
        return _read_pdf_native_blocks(orig_path, resolved, pages=pages)
    return _read_pdf_text(
        orig_path, resolved, offset=offset, limit=limit, pages=pages, extractor=extractor
    )


def _read_pdf_native_blocks(
    orig_path: str,
    resolved: Path,
    pages: Optional[List[int]] = None,
) -> str:
    """Anthropic 原生路径：PDF → base64 → document block（跟图片路径同构）。

    Claude 3.5+ 原生支持 PDF document block（官方上限 32MB / 100 pages）。
    保守先 5MB cap，超限直接拒 —— 让模型走"换小 PDF"或"切换非 Anthropic provider"。
    返回协议跟 _read_image_as_blocks 一致：JSON 含 _content_blocks（文本+document block）。

    pages 参数（Q1）：当作 hint 加在 summary_text 里。Claude 看完整 PDF 但被引导
    重点关注指定页（不真过滤——重打包 PDF 风险大、Claude 自己也能看页号）。
    """
    import base64 as _b64

    if not resolved.exists():
        return json.dumps({"error": f"File not found: {orig_path}", **_suggest_path(resolved)})
    if not resolved.is_file():
        return json.dumps({"error": f"Not a file: {orig_path}"})

    pages_normalized, page_err = _normalize_pdf_pages(pages)
    if page_err:
        return json.dumps({"error": page_err})

    try:
        size = resolved.stat().st_size
    except OSError as e:
        return json.dumps({"error": f"Cannot stat '{orig_path}': {e}"})

    if size > _PDF_INLINE_MAX_BYTES:
        size_mb = size / (1024 * 1024)
        cap_mb = _PDF_INLINE_MAX_BYTES / (1024 * 1024)
        return json.dumps({
            "error": (
                f"PDF too large to inline ({size_mb:.1f} MB > {cap_mb:.0f} MB cap for Anthropic native). "
                f"Use a smaller PDF or switch to a non-Anthropic provider for text extraction fallback."
            )
        })

    try:
        with open(resolved, "rb") as f:
            raw = f.read()
    except OSError as e:
        return json.dumps({"error": f"Cannot read PDF '{orig_path}': {e}"})

    b64 = _b64.b64encode(raw).decode("ascii")
    pages_hint = ""
    if pages_normalized:
        pages_hint = f" User requested focus on pages: {pages_normalized}."
    # 跟 _read_image_as_blocks 同样 —— 不写"Claude 能看到"这种确定性话术，
    # 避免误调用场景（如 ContextVar 意外为 True 但后端其实是伪 Anthropic）让模型瞎编。
    summary_text = (
        f"PDF attachment: {resolved} (application/pdf, {size} bytes). "
        f"Native Anthropic document block; Claude can parse layout / tables / images directly."
        + pages_hint
    )

    meta: Dict[str, Any] = {
        "path": str(resolved),
        "size_bytes": size,
        "media_type": "application/pdf",
        "route": "native",
    }
    if pages_normalized:
        meta["pages_hint"] = pages_normalized

    payload = {
        "_content_blocks": [
            {"type": "text", "text": summary_text},
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": b64,
                },
            },
        ],
        "_meta": meta,
    }
    return json.dumps(payload, ensure_ascii=False)


def _read_pdf_text(
    orig_path: str,
    resolved: Path,
    offset: int = 1,
    limit: int = 2000,
    pages: Optional[List[int]] = None,
    extractor: str = "pypdf",
) -> str:
    """非 Anthropic 路径：pypdf / pdfplumber 抽文本 → cat -n 格式。

    复用 read_file_tool 的 offset/limit 语义：PDF 全文抽出按行切。
    扫描版 PDF（抽不到文本）→ 友好提示引导切 Claude / 本地 ocrmypdf（不打包本地 OCR 引擎）。
    加密 PDF → 拒（不支持密码）。
    单页解析失败不会让整个 PDF 报错——该页插入 [Page X: extraction failed: ...] 占位。

    pages 参数（Q1）：list[int] (1-indexed)，None/[]=读全文。越界页静默跳过 +
    在 _meta.invalid_pages 标记。
    extractor 参数（Q2）："pypdf"（默认，快）或 "pdfplumber"（慢，表格转 markdown）。
    """
    if not resolved.exists():
        return json.dumps({"error": f"File not found: {orig_path}", **_suggest_path(resolved)})
    if not resolved.is_file():
        return json.dumps({"error": f"Not a file: {orig_path}"})

    pages_normalized, page_err = _normalize_pdf_pages(pages)
    if page_err:
        return json.dumps({"error": page_err})

    if extractor not in ("pypdf", "pdfplumber"):
        return json.dumps({
            "error": (
                f"Invalid pdf_extractor: {extractor!r}; expected 'pypdf' (default, fast) "
                f"or 'pdfplumber' (slower, better tables / complex layouts)."
            )
        })

    if extractor == "pdfplumber":
        full_text, n_pages, invalid_pages, err_resp = _extract_pdf_with_pdfplumber(
            resolved, pages_normalized
        )
    else:
        full_text, n_pages, invalid_pages, err_resp = _extract_pdf_with_pypdf(
            resolved, pages_normalized
        )
    if err_resp is not None:
        return err_resp

    # 扫描版 PDF：抽不到任何文本（不管哪个 extractor）
    if not full_text.strip() or _is_scanned_pdf_text(full_text):
        return json.dumps({
            "error": (
                f"PDF '{orig_path}' appears to be scanned (image-only, no extractable text "
                f"from {n_pages} pages with {extractor}). Local OCR engines are not bundled.\n"
                f"Options:\n"
                f"  1. Switch to a Claude model (e.g. claude-sonnet-4) — Claude reads scanned "
                f"PDFs natively via vision.\n"
                f"  2. OCR locally first (e.g. `ocrmypdf input.pdf output.pdf`, Adobe Acrobat, "
                f"or ABBYY) and read the resulting text PDF."
            ),
            "_meta": {
                "path": str(resolved),
                "n_pages": n_pages,
                "scanned": True,
                "route": "text",
                "extractor": extractor,
            },
        })

    return _slice_pdf_text_result(
        resolved, full_text, offset=offset, limit=limit, n_pages=n_pages,
        extractor=extractor, pages_filter=pages_normalized, invalid_pages=invalid_pages,
    )


def _is_scanned_pdf_text(full_text: str) -> bool:
    """判定抽取出来的文本是不是"实际为空"（即扫描版 PDF）。

    full_text 里会有 `--- Page N ---` 分隔符 + `[Page N: extraction failed: ...]`
    占位，把它们去掉之后还是空 → 扫描版。
    """
    cleaned_lines = []
    for line in full_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("--- Page ") and stripped.endswith("---"):
            continue
        if stripped.startswith("[Page ") and "extraction failed" in stripped:
            continue
        cleaned_lines.append(stripped)
    return not cleaned_lines


def _extract_pdf_with_pypdf(
    resolved: Path,
    pages: Optional[List[int]],
) -> Tuple[Optional[str], int, List[int], Optional[str]]:
    """pypdf 抽文本路径。返 (full_text, n_pages, invalid_pages, error_resp_or_None)。"""
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError:
        return None, 0, [], json.dumps({
            "error": "PDF support requires pypdf. Install with: pip install pypdf>=5.0,<6.0"
        })

    try:
        reader = PdfReader(str(resolved))
        if reader.is_encrypted:
            return None, 0, [], json.dumps({
                "error": (
                    f"PDF '{resolved}' is encrypted. Decryption with password not supported. "
                    f"Decrypt manually before reading."
                )
            })

        n_pages = len(reader.pages)
        if pages:
            target_indices = [p - 1 for p in pages if 1 <= p <= n_pages]
            invalid_pages = [p for p in pages if p < 1 or p > n_pages]
        else:
            target_indices = list(range(n_pages))
            invalid_pages = []

        page_texts: List[Tuple[int, str]] = []
        for idx in target_indices:
            page = reader.pages[idx]
            try:
                page_text = page.extract_text() or ""
            except Exception as page_exc:
                page_text = (
                    f"[Page {idx + 1}: extraction failed: "
                    f"{type(page_exc).__name__}: {page_exc}]"
                )
            page_texts.append((idx + 1, page_text))

        full_text = "\n\n".join(
            f"--- Page {pn} ---\n{text}" for pn, text in page_texts
        )
        return full_text, n_pages, invalid_pages, None
    except PdfReadError as e:
        return None, 0, [], json.dumps({"error": f"Cannot parse PDF '{resolved}': {e}"})
    except Exception as e:
        return None, 0, [], json.dumps({
            "error": f"Unexpected error reading PDF '{resolved}': {type(e).__name__}: {e}"
        })


def _extract_pdf_with_pdfplumber(
    resolved: Path,
    pages: Optional[List[int]],
) -> Tuple[Optional[str], int, List[int], Optional[str]]:
    """pdfplumber 抽文本 + 表格路径（Q2，表格转 markdown）。

    返 (full_text, n_pages, invalid_pages, error_resp_or_None)。
    pdfplumber 比 pypdf 慢 3-5 倍，但能识别表格结构（用 extract_tables() 拿
    list[list[str]]，转 markdown 表格嵌入文本流）。表格密集 PDF 强烈推荐用它。
    """
    try:
        import pdfplumber
    except ImportError:
        return None, 0, [], json.dumps({
            "error": (
                "PDF table extraction requires pdfplumber. "
                "Install with: pip install pdfplumber>=0.11,<0.12"
            )
        })

    try:
        with pdfplumber.open(str(resolved)) as pdf:
            n_pages = len(pdf.pages)
            if pages:
                target_indices = [p - 1 for p in pages if 1 <= p <= n_pages]
                invalid_pages = [p for p in pages if p < 1 or p > n_pages]
            else:
                target_indices = list(range(n_pages))
                invalid_pages = []

            page_chunks: List[Tuple[int, str]] = []
            for idx in target_indices:
                page = pdf.pages[idx]
                # 抽 text
                try:
                    page_text = page.extract_text() or ""
                except Exception as page_exc:
                    page_text = (
                        f"[Page {idx + 1}: text extraction failed: "
                        f"{type(page_exc).__name__}]"
                    )

                # 抽 tables → markdown（pdfplumber 的卖点）
                tables_md = ""
                try:
                    tables = page.extract_tables() or []
                    if tables:
                        md_parts: List[str] = []
                        for ti, table in enumerate(tables, start=1):
                            md_parts.append(f"\n[Table {ti} on page {idx + 1}:]")
                            md_parts.append(_table_to_markdown(table))
                        tables_md = "\n".join(md_parts)
                except Exception as table_exc:
                    tables_md = (
                        f"\n[Tables on page {idx + 1} extraction failed: "
                        f"{type(table_exc).__name__}]"
                    )

                chunk = f"--- Page {idx + 1} ---\n{page_text}{tables_md}"
                page_chunks.append((idx + 1, chunk))

            full_text = "\n\n".join(c for _, c in page_chunks)
            return full_text, n_pages, invalid_pages, None
    except Exception as e:
        return None, 0, [], json.dumps({
            "error": (
                f"Unexpected error reading PDF '{resolved}' with pdfplumber: "
                f"{type(e).__name__}: {e}"
            )
        })


def _table_to_markdown(table: List[List[Optional[str]]]) -> str:
    """list[list[str]] → markdown 表格。空 cell 用 '' 占位。

    pdfplumber.extract_tables() 返 list[list[list[str|None]]]（每页可能多张表）；
    本函数处理单张表（list[list[str|None]]）。第一行当 header，后续行当数据。
    """
    if not table:
        return ""
    rows = [[(c or "").replace("\n", " ").strip() for c in (row or [])] for row in table]
    if not rows or not rows[0]:
        return ""
    n_cols = max((len(r) for r in rows), default=0)
    if n_cols == 0:
        return ""
    rows = [r + [""] * (n_cols - len(r)) for r in rows]

    md = ["| " + " | ".join(rows[0]) + " |"]
    md.append("| " + " | ".join(["---"] * n_cols) + " |")
    for row in rows[1:]:
        md.append("| " + " | ".join(row) + " |")
    return "\n".join(md)


def _slice_pdf_text_result(
    resolved: Path,
    full_text: str,
    offset: int,
    limit: int,
    n_pages: int,
    extractor: str,
    pages_filter: Optional[List[int]] = None,
    invalid_pages: Optional[List[int]] = None,
) -> str:
    """共用尾段：按行切 + 包装 result + meta。pypdf / pdfplumber 路径都用。"""
    lines = full_text.splitlines()
    total_lines = len(lines)
    start_idx = max(0, offset - 1)
    end_idx = min(total_lines, start_idx + limit)
    selected_lines = lines[start_idx:end_idx]

    formatted = [f"{i}\t{line}" for i, line in enumerate(selected_lines, start=offset)]
    content = "\n".join(formatted)
    truncated = end_idx < total_lines

    result: Dict[str, Any] = {
        "path": str(resolved),
        "content": content,
        "total_lines": total_lines,
        "n_pages": n_pages,
        "showing_lines": (
            f"{offset}-{offset + len(selected_lines) - 1}" if selected_lines else "0"
        ),
        "_meta": {
            "path": str(resolved),
            "n_pages": n_pages,
            "extracted_by": extractor,
            "route": "text",
        },
    }
    if pages_filter:
        result["_meta"]["pages_filter"] = pages_filter
    if invalid_pages:
        result["_meta"]["invalid_pages"] = invalid_pages
        result["warning"] = (
            f"Some pages out of range: {invalid_pages}; n_pages={n_pages}"
        )
    if truncated:
        result["truncated"] = True
        result["hint"] = (
            f"PDF text has {total_lines} lines (across {n_pages} pages). "
            f"Use offset and limit to read more."
        )

    return json.dumps(result, ensure_ascii=False)


# ============================================================================
# Office 文档读取（2026-05-12 Q3 方案）
# ============================================================================
#
# .docx / .pptx：抽段落 + 表格（python-docx / python-pptx）→ cat -n 格式。
# 跟 PDF 不同：office 格式没有 Anthropic 原生 document block 支持（Anthropic
# 只接 PDF/image/text），所以所有 provider 都走 text 路径。
#
# 表格转 markdown 复用 PDF 路径的 _table_to_markdown helper。
# 切片复用 read_file_tool 的 offset/limit 语义。

def _docx_table_to_list(table) -> List[List[str]]:
    """python-docx Table → list[list[str]]（用于复用 _table_to_markdown）。"""
    return [
        [(cell.text or "").replace("\n", " ").strip() for cell in row.cells]
        for row in table.rows
    ]


def _pptx_table_to_list(table) -> List[List[str]]:
    """python-pptx Table → list[list[str]]（用于复用 _table_to_markdown）。"""
    return [
        [(cell.text or "").replace("\n", " ").strip() for cell in row.cells]
        for row in table.rows
    ]


def _slice_office_text_result(
    resolved: Path,
    full_text: str,
    offset: int,
    limit: int,
    extracted_by: str,
    extra_meta: Optional[Dict[str, Any]] = None,
) -> str:
    """共用尾段：office 文档按行切 + 包装 result + meta。

    跟 _slice_pdf_text_result 同形态但 _meta 字段适配 office（无 n_pages 概念，
    extra_meta 携带 paragraph_count / n_slides 等）。
    """
    lines = full_text.splitlines()
    total_lines = len(lines)
    start_idx = max(0, offset - 1)
    end_idx = min(total_lines, start_idx + limit)
    selected_lines = lines[start_idx:end_idx]

    formatted = [f"{i}\t{line}" for i, line in enumerate(selected_lines, start=offset)]
    content = "\n".join(formatted)
    truncated = end_idx < total_lines

    meta: Dict[str, Any] = {
        "path": str(resolved),
        "extracted_by": extracted_by,
        "route": "text",
    }
    if extra_meta:
        meta.update(extra_meta)

    result: Dict[str, Any] = {
        "path": str(resolved),
        "content": content,
        "total_lines": total_lines,
        "showing_lines": (
            f"{offset}-{offset + len(selected_lines) - 1}" if selected_lines else "0"
        ),
        "_meta": meta,
    }
    if truncated:
        result["truncated"] = True
        result["hint"] = (
            f"Document text has {total_lines} lines. "
            f"Use offset and limit to read more."
        )

    return json.dumps(result, ensure_ascii=False)


def _read_docx(
    orig_path: str,
    resolved: Path,
    offset: int = 1,
    limit: int = 2000,
) -> str:
    """读 .docx → python-docx 抽段落 + 表格 → cat -n 格式。

    所有 provider 同路径（无 Anthropic native 支持）。
    输出按顺序拼接：段落文本 → 各表格 markdown。
    """
    if not resolved.exists():
        return json.dumps({"error": f"File not found: {orig_path}", **_suggest_path(resolved)})
    if not resolved.is_file():
        return json.dumps({"error": f"Not a file: {orig_path}"})

    # lazy import
    try:
        from docx import Document
    except ImportError:
        return json.dumps({
            "error": (
                "DOCX support requires python-docx. "
                "Install with: pip install python-docx>=1.0,<2.0"
            )
        })

    try:
        doc = Document(str(resolved))

        # 段落（跳过空段，保留缩进 / 列表 prefix 由 python-docx 自己处理）
        para_texts: List[str] = []
        for para in doc.paragraphs:
            text = para.text
            if text.strip():
                para_texts.append(text)
        para_count = len(para_texts)

        # 表格 → markdown
        table_chunks: List[str] = []
        for ti, table in enumerate(doc.tables, start=1):
            try:
                rows = _docx_table_to_list(table)
                if rows:
                    table_chunks.append(f"\n[Table {ti}]")
                    table_chunks.append(_table_to_markdown(rows))
            except Exception as table_exc:
                table_chunks.append(
                    f"\n[Table {ti} extraction failed: {type(table_exc).__name__}]"
                )

        full_text = "\n".join(para_texts)
        if table_chunks:
            full_text = full_text + "\n" + "\n".join(table_chunks)
    except Exception as e:
        return json.dumps({
            "error": (
                f"Cannot parse DOCX '{orig_path}': {type(e).__name__}: {e}. "
                f"Make sure it's a valid .docx file (not .doc — old binary format not supported)."
            )
        })

    if not full_text.strip():
        return json.dumps({
            "error": (
                f"DOCX '{orig_path}' contains no extractable text "
                f"(empty document or only images / embedded objects)."
            ),
            "_meta": {
                "path": str(resolved),
                "extracted_by": "python-docx",
                "route": "text",
                "paragraph_count": 0,
                "table_count": len(doc.tables) if "doc" in locals() else 0,
            },
        })

    return _slice_office_text_result(
        resolved, full_text, offset=offset, limit=limit,
        extracted_by="python-docx",
        extra_meta={
            "paragraph_count": para_count,
            "table_count": len(doc.tables),
        },
    )


def _read_pptx(
    orig_path: str,
    resolved: Path,
    offset: int = 1,
    limit: int = 2000,
) -> str:
    """读 .pptx → python-pptx 抽幻灯片文本 + 表格 → cat -n 格式。

    每张幻灯片用 `--- Slide N ---` 分隔，跟 PDF 的 `--- Page N ---` 同款。
    所有 provider 同路径（无 Anthropic native 支持）。
    """
    if not resolved.exists():
        return json.dumps({"error": f"File not found: {orig_path}", **_suggest_path(resolved)})
    if not resolved.is_file():
        return json.dumps({"error": f"Not a file: {orig_path}"})

    # lazy import
    try:
        from pptx import Presentation
    except ImportError:
        return json.dumps({
            "error": (
                "PPTX support requires python-pptx. "
                "Install with: pip install python-pptx>=1.0,<2.0"
            )
        })

    try:
        prs = Presentation(str(resolved))
        n_slides = len(prs.slides)
        slide_chunks: List[str] = []

        for i, slide in enumerate(prs.slides, start=1):
            chunk: List[str] = [f"--- Slide {i} ---"]
            for shape in slide.shapes:
                # 文本框
                if shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        if para.text.strip():
                            chunk.append(para.text)
                # 表格
                if shape.has_table:
                    try:
                        rows = _pptx_table_to_list(shape.table)
                        if rows:
                            chunk.append(f"\n[Table on slide {i}]")
                            chunk.append(_table_to_markdown(rows))
                    except Exception as table_exc:
                        chunk.append(
                            f"\n[Table on slide {i} extraction failed: "
                            f"{type(table_exc).__name__}]"
                        )

            slide_chunks.append("\n".join(chunk))

        full_text = "\n\n".join(slide_chunks)
    except Exception as e:
        return json.dumps({
            "error": (
                f"Cannot parse PPTX '{orig_path}': {type(e).__name__}: {e}. "
                f"Make sure it's a valid .pptx file (not .ppt — old binary format not supported)."
            )
        })

    # 检查是否只有 slide markers 没实际内容
    actual_lines = [
        ln for ln in full_text.splitlines()
        if ln.strip() and not (ln.startswith("--- Slide ") and ln.endswith("---"))
    ]
    if not actual_lines:
        return json.dumps({
            "error": (
                f"PPTX '{orig_path}' contains no extractable text "
                f"(all slides empty or only images)."
            ),
            "_meta": {
                "path": str(resolved),
                "extracted_by": "python-pptx",
                "route": "text",
                "n_slides": n_slides,
            },
        })

    return _slice_office_text_result(
        resolved, full_text, offset=offset, limit=limit,
        extracted_by="python-pptx",
        extra_meta={"n_slides": n_slides},
    )


def _read_xlsx(
    orig_path: str,
    resolved: Path,
    offset: int = 1,
    limit: int = 2000,
) -> str:
    """读 .xlsx → openpyxl 抽各 sheet → markdown 表格 → cat -n 格式（2026-05-12 Q4）。

    每个 sheet 用 `--- Sheet: NAME ---` 分隔（跟 PDF page / PPTX slide 同款）。
    每个 sheet 内全部数据当一张表 → 复用 _table_to_markdown。
    用 data_only=True：取计算值而不是公式字符串（agent 看到 "100" 比 "=A1+B1" 更有用）。
    用 read_only=True：流式读，大文件不爆内存。
    """
    if not resolved.exists():
        return json.dumps({"error": f"File not found: {orig_path}", **_suggest_path(resolved)})
    if not resolved.is_file():
        return json.dumps({"error": f"Not a file: {orig_path}"})

    # lazy import
    try:
        from openpyxl import load_workbook
    except ImportError:
        return json.dumps({
            "error": (
                "XLSX support requires openpyxl. "
                "Install with: pip install openpyxl>=3.1,<4.0"
            )
        })

    wb = None
    try:
        # data_only=True：只读公式计算结果（不要 "=A1+B1" 这种）
        # read_only=True：streaming reader，省内存（大表格能撑）
        wb = load_workbook(str(resolved), data_only=True, read_only=True)
        n_sheets = len(wb.sheetnames)
        sheet_names = list(wb.sheetnames)
        sheet_chunks: List[str] = []
        total_rows = 0

        for sheet_name in sheet_names:
            ws = wb[sheet_name]
            chunk: List[str] = [f"--- Sheet: {sheet_name} ---"]

            try:
                rows_data: List[List[str]] = []
                for row in ws.iter_rows(values_only=True):
                    # 全空行跳过（避免 sheet 末尾大量空白行）
                    if not row or all(c is None for c in row):
                        continue
                    rows_data.append([
                        "" if c is None else str(c).replace("\n", " ").strip()
                        for c in row
                    ])
                total_rows += len(rows_data)

                if rows_data:
                    chunk.append(_table_to_markdown(rows_data))
                else:
                    chunk.append("[empty sheet]")
            except Exception as sheet_exc:
                chunk.append(
                    f"[Sheet '{sheet_name}' extraction failed: "
                    f"{type(sheet_exc).__name__}: {sheet_exc}]"
                )

            sheet_chunks.append("\n".join(chunk))

        full_text = "\n\n".join(sheet_chunks)
    except Exception as e:
        return json.dumps({
            "error": (
                f"Cannot parse XLSX '{orig_path}': {type(e).__name__}: {e}. "
                f"Make sure it's a valid .xlsx file (not .xls — old binary format not supported)."
            )
        })
    finally:
        if wb is not None:
            try:
                wb.close()
            except Exception:
                pass

    # 检查是否所有 sheet 都空（只有 marker 没数据）
    actual_lines = [
        ln for ln in full_text.splitlines()
        if ln.strip()
        and not (ln.startswith("--- Sheet: ") and ln.endswith(" ---"))
        and ln.strip() != "[empty sheet]"
    ]
    if not actual_lines:
        return json.dumps({
            "error": (
                f"XLSX '{orig_path}' contains no extractable data "
                f"(all {n_sheets} sheets are empty)."
            ),
            "_meta": {
                "path": str(resolved),
                "extracted_by": "openpyxl",
                "route": "text",
                "n_sheets": n_sheets,
                "sheet_names": sheet_names,
            },
        })

    return _slice_office_text_result(
        resolved, full_text, offset=offset, limit=limit,
        extracted_by="openpyxl",
        extra_meta={
            "n_sheets": n_sheets,
            "sheet_names": sheet_names,
            "total_rows": total_rows,
        },
    )


# ============================================================================
# Jupyter Notebook 读取（2026-05-12 R2 方案）
# ============================================================================
#
# .ipynb 是 JSON，直接当文本读会让模型看到一堆 nbformat 元数据 / base64 图像 /
# JSON 转义 \n，token 浪费严重。本函数用 nbformat 解析 cells，每个 cell 按类型
# 渲染成 markdown / 代码块，输出做截短占位。
#
# 设计：
# - markdown cell：原文（让模型直接读 markdown）
# - code cell：```python\nsource\n``` + 简短 outputs
# - raw / 未知 cell：原文 + cell type 标记
#
# Outputs（code cell）：
# - stream（stdout/stderr）：保留前 _IPYNB_OUTPUT_LINE_CAP 行
# - execute_result/display_data：优先 text/plain；text/html 截短；image 占位
# - error：清掉 ANSI 转义后保留 ename/evalue + 头几行 traceback
#
# 切片复用 _slice_office_text_result。

_IPYNB_OUTPUT_LINE_CAP = 50    # 单个 output 最多保留行数
_IPYNB_HTML_CHAR_CAP = 200     # text/html output 占位预览字符数
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")  # 清 nbformat traceback 的 ANSI 颜色码


def _format_notebook_output(output: dict) -> str:
    """单个 nbformat output → 文本占位 / 截短摘要。"""
    out_type = output.get("output_type", "")
    if out_type == "stream":
        text = output.get("text", "")
        if isinstance(text, list):
            text = "".join(text)
        lines = text.splitlines()
        name = output.get("name", "stdout")
        if len(lines) > _IPYNB_OUTPUT_LINE_CAP:
            head = "\n".join(lines[:_IPYNB_OUTPUT_LINE_CAP])
            return (
                f"[stream:{name}]\n{head}\n"
                f"... ({len(lines) - _IPYNB_OUTPUT_LINE_CAP} more lines truncated)"
            )
        return f"[stream:{name}]\n{text.rstrip()}"
    if out_type in ("execute_result", "display_data"):
        data = output.get("data", {}) or {}
        plain = data.get("text/plain")
        if plain:
            if isinstance(plain, list):
                plain = "".join(plain)
            lines = plain.splitlines()
            if len(lines) > _IPYNB_OUTPUT_LINE_CAP:
                head = "\n".join(lines[:_IPYNB_OUTPUT_LINE_CAP])
                return (
                    f"[output]\n{head}\n"
                    f"... ({len(lines) - _IPYNB_OUTPUT_LINE_CAP} more lines truncated)"
                )
            return f"[output]\n{plain.rstrip()}"
        html = data.get("text/html")
        if html:
            if isinstance(html, list):
                html = "".join(html)
            return f"[html output, {len(html)} chars] {html[:_IPYNB_HTML_CHAR_CAP]}..."
        for mime in ("image/png", "image/jpeg", "image/svg+xml", "image/gif"):
            if mime in data:
                payload = data[mime]
                size = len(payload) if isinstance(payload, str) else 0
                return f"[image output: {mime}, ~{size} chars base64]"
        mimes = list(data.keys())
        return f"[output: {mimes}]"
    if out_type == "error":
        ename = output.get("ename", "?")
        evalue = output.get("evalue", "")
        tb = output.get("traceback", [])
        if isinstance(tb, list) and tb:
            tb_clean = [_ANSI_RE.sub("", line) for line in tb[:5]]
            return f"[error: {ename}: {evalue}]\n" + "\n".join(tb_clean)
        return f"[error: {ename}: {evalue}]"
    return f"[output: {out_type}]"


def _read_ipynb(
    orig_path: str,
    resolved: Path,
    offset: int = 1,
    limit: int = 2000,
) -> str:
    """读 .ipynb → nbformat 抽 cells → cat -n 格式。

    所有 provider 同路径（无 native ipynb 支持）。
    每 cell 按类型渲染成 markdown 或 ```python``` 代码块，outputs 截短占位。
    """
    if not resolved.exists():
        return json.dumps({"error": f"File not found: {orig_path}", **_suggest_path(resolved)})
    if not resolved.is_file():
        return json.dumps({"error": f"Not a file: {orig_path}"})

    try:
        import nbformat
    except ImportError:
        return json.dumps({
            "error": (
                "Jupyter notebook support requires nbformat. "
                "Install with: pip install nbformat"
            )
        })

    try:
        nb = nbformat.read(str(resolved), as_version=4)
    except Exception as e:
        return json.dumps({
            "error": (
                f"Cannot parse notebook '{orig_path}': {type(e).__name__}: {e}. "
                f"Make sure it's a valid .ipynb file."
            )
        })

    cells = nb.get("cells", []) or []
    n_cells = len(cells)
    metadata = nb.get("metadata", {}) or {}
    kernel = (
        (metadata.get("kernelspec", {}) or {}).get("name")
        or (metadata.get("language_info", {}) or {}).get("name")
        or "unknown"
    )

    if n_cells == 0:
        return json.dumps({
            "error": f"Notebook '{orig_path}' has no cells.",
            "_meta": {
                "path": str(resolved),
                "extracted_by": "nbformat",
                "route": "text",
                "n_cells": 0,
                "kernel": kernel,
            },
        })

    code_count = 0
    md_count = 0
    raw_count = 0
    chunks: List[str] = []
    for ci, cell in enumerate(cells, start=1):
        cell_type = cell.get("cell_type", "")
        source = cell.get("source", "")
        if isinstance(source, list):
            source = "".join(source)
        if cell_type == "markdown":
            md_count += 1
            chunks.append(f"# === Cell {ci}: markdown ===")
            chunks.append(source.rstrip())
        elif cell_type == "code":
            code_count += 1
            exec_count = cell.get("execution_count")
            header = f"# === Cell {ci}: code"
            if exec_count is not None:
                header += f" (exec={exec_count})"
            header += " ==="
            chunks.append(header)
            chunks.append("```python")
            chunks.append(source.rstrip())
            chunks.append("```")
            for out in cell.get("outputs", []) or []:
                try:
                    chunks.append(_format_notebook_output(out))
                except Exception as out_exc:
                    chunks.append(f"[output extraction failed: {type(out_exc).__name__}]")
        else:
            raw_count += 1
            chunks.append(f"# === Cell {ci}: {cell_type or 'raw'} ===")
            chunks.append(source.rstrip())
        chunks.append("")  # cell 间空行

    full_text = "\n".join(chunks).rstrip() + "\n"

    return _slice_office_text_result(
        resolved, full_text, offset=offset, limit=limit,
        extracted_by="nbformat",
        extra_meta={
            "n_cells": n_cells,
            "code_count": code_count,
            "markdown_count": md_count,
            "raw_count": raw_count,
            "kernel": kernel,
        },
    )


# ============================================================================
# 专用格式 handler registry（2026-05-12 R1）
# ============================================================================
#
# 把 `if suffix == ".xxx":` 链改成查表，新增文件类型只需注册一行：
# - PDF 是特殊路径（需要透传 pages/pdf_extractor 两个特定参数）
# - 其他都是同 (path, resolved, offset, limit) 签名
# - image 走 _read_image_as_blocks（返 vision block 不是 text，不走 registry）
_FILE_TYPE_HANDLERS: Dict[str, Callable[..., str]] = {
    ".pdf": _read_pdf,
    ".docx": _read_docx,
    ".pptx": _read_pptx,
    ".xlsx": _read_xlsx,
    ".ipynb": _read_ipynb,
}


def read_file_tool(
    path: str,
    offset: int = 1,
    limit: int = 2000,
    pages: Optional[List[int]] = None,
    pdf_extractor: str = "pypdf",
) -> str:
    """读取文件内容 — 路由到各专用 handler（image / pdf / docx / pptx / xlsx）或普通文本。"""
    # 类型 coerce 由 clean_registry._dispatch 统一处理（按 input_schema 把 LLM
    # 传的 "1" 字符串转 int）。这里只做业务约束 clamp：offset / limit 必须 ≥ 1，
    # 否则切片越界。clean_registry 失败时会保留原值，所以仍需 isinstance 防御。
    if not isinstance(offset, int) or offset < 1:
        offset = 1
    if not isinstance(limit, int) or limit < 1:
        limit = 2000

    try:
        # 设备文件检查
        if _is_blocked_device(path):
            return json.dumps({
                "error": f"Cannot read '{path}': this is a device file"
            })

        # read 路径专用解析：agent_dir 找不到时 fallback 到 cwd（项目根），
        # 兼容 subagent / LLM 用相对路径读项目源码（read_file('backend/main.py')）
        resolved = _resolve_read_path(path)

        # 图片：base64 inline 给 vision 模型看（handover_image_read_2026-05-11.md §3.1）
        # 注意必须先于二进制检查（PNG/JPG 也在 BINARY_EXTENSIONS 里）
        # 单独处理：返回的是 vision block 数组而非 cat -n 文本，跟 registry 形态不一致
        if is_image(str(resolved)):
            return _read_image_as_blocks(path, resolved)

        # 专用格式 dispatch（2026-05-12 R1）：从 if 链改成 registry 查表
        # - PDF 走双路径 (Anthropic native / pypdf / pdfplumber)，需透传 pages + extractor
        # - office 三者 (docx/pptx/xlsx) 同签名 (offset, limit)
        # 新增文件类型：在 _FILE_TYPE_HANDLERS 加一行即可。
        handler = _FILE_TYPE_HANDLERS.get(resolved.suffix.lower())
        if handler is _read_pdf:
            return handler(
                path, resolved, offset=offset, limit=limit,
                pages=pages, extractor=pdf_extractor,
            )
        if handler is not None:
            return handler(path, resolved, offset=offset, limit=limit)

        # 二进制文件检查
        if has_binary_extension(str(resolved)):
            ext = resolved.suffix.lower()
            return json.dumps({
                "error": f"Cannot read '{path}': binary file type {ext}"
            })

        # 敏感路径检查
        sensitive_warning = _check_sensitive_path(str(resolved))
        if sensitive_warning:
            logger.warning(sensitive_warning)

        # 读取文件
        if not resolved.exists():
            # P1#2 自愈提示：模糊匹配父目录里的同名近似文件，把候选写进 hint
            # 这样 LLM 看到 "File not found" + "did you mean: foo.py / bar.py" 能直接续上正确路径
            return json.dumps({"error": f"File not found: {path}", **_suggest_path(resolved)})

        if not resolved.is_file():
            # P1#2 自愈提示：路径是目录时列出前几个子项，引导 LLM 改用 read_file/glob_search
            return json.dumps({"error": f"Not a file: {path}", **_suggest_dir(resolved)})

        with open(resolved, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()

        total_lines = len(lines)
        start_idx = max(0, offset - 1)
        end_idx = min(total_lines, start_idx + limit)

        selected_lines = lines[start_idx:end_idx]

        formatted_lines = []
        truncated_lines = []
        for i, line in enumerate(selected_lines, start=offset):
            text = line.rstrip()
            if len(text) > _READ_FILE_MAX_LINE_CHARS:
                original_len = len(text)
                text = (
                    text[:_READ_FILE_MAX_LINE_CHARS]
                    + f"...[line truncated: original {original_len} chars, kept {_READ_FILE_MAX_LINE_CHARS}]"
                )
                truncated_lines.append({"line": i, "original_chars": original_len})
            formatted_lines.append(f"{i}\t{text}")

        content = "\n".join(formatted_lines)
        content_truncated = False
        original_content_chars = len(content)
        if original_content_chars > _DEFAULT_MAX_READ_CHARS:
            note = (
                f"\n\n[read_file content truncated: original {original_content_chars} chars, "
                f"kept {_DEFAULT_MAX_READ_CHARS} chars. Use narrower offset/limit or grep for specifics.]"
            )
            keep = max(0, _DEFAULT_MAX_READ_CHARS - len(note))
            content = content[:keep] + note
            content_truncated = True

        truncated = end_idx < total_lines

        result = {
            "path": str(resolved),
            "content": content,
            "total_lines": total_lines,
            "showing_lines": f"{offset}-{offset + len(selected_lines) - 1}",
        }

        if truncated or truncated_lines or content_truncated:
            result["truncated"] = True
            hints = []
            if truncated:
                hints.append(f"File has {total_lines} lines. Use offset and limit to read more.")
            if truncated_lines:
                result["truncated_lines"] = truncated_lines[:20]
                result["truncated_line_count"] = len(truncated_lines)
                hints.append(
                    f"{len(truncated_lines)} shown line(s) exceeded {_READ_FILE_MAX_LINE_CHARS} chars and were shortened."
                )
            if content_truncated:
                result["original_content_chars"] = original_content_chars
                result["content_budget_chars"] = _DEFAULT_MAX_READ_CHARS
                hints.append("Returned content hit the read_file character budget; use a narrower range.")
            result["hint"] = " ".join(hints)

        return json.dumps(result, ensure_ascii=False)

    except Exception as e:
        logger.exception(f"Error reading file {path}")
        return json.dumps({"error": str(e)})


def write_file_tool(path: str, content: str) -> str:
    """
    写入文件

    Args:
        path: 文件路径
        content: 文件内容

    Returns:
        JSON 格式的结果
    """
    try:
        resolved = _resolve_path(path)

        # 敏感路径检查
        sensitive_warning = _check_sensitive_path(str(resolved))
        if sensitive_warning:
            return json.dumps({"error": sensitive_warning})

        # 先读 before（文件不存在视为创建，before="" 给 file_diff 走 is_create 分支）
        before_content = ""
        if resolved.exists() and resolved.is_file():
            try:
                with open(resolved, 'r', encoding='utf-8', errors='replace') as f:
                    before_content = f.read()
            except Exception:
                # 读不出来不阻塞写，diff 退化为"全增"视图
                before_content = ""

        # 创建父目录
        resolved.parent.mkdir(parents=True, exist_ok=True)

        # 写入文件
        with open(resolved, 'w', encoding='utf-8') as f:
            f.write(content)

        lines_written = content.count('\n') + (1 if content and not content.endswith('\n') else 0)

        # 注入 sentinel diff 载荷给前端 FileDiffCard 渲染。新建文件时 before="" →
        # is_create=True，前端展示"新文件 + 全绿"。覆盖写时按 hunk 红/绿展示。
        diff_payload = build_file_diff_payload(
            path=str(resolved),
            before=before_content,
            after=content,
            extra={
                "success": True,
                "lines_written": lines_written,
            },
        )
        return json.dumps(diff_payload, ensure_ascii=False)

    except Exception as e:
        if _is_expected_write_exception(e):
            return json.dumps({"error": f"Permission denied: {path}"})
        logger.exception(f"Error writing file {path}")
        return json.dumps({"error": str(e)})


def patch_tool(
    mode: str = "replace",
    path: str = None,
    old_string: str = None,
    new_string: str = None,
    replace_all: bool = False
) -> str:
    """
    文件内容替换

    Args:
        mode: 替换模式（目前只支持 "replace"）
        path: 文件路径
        old_string: 要替换的字符串
        new_string: 新字符串
        replace_all: 是否替换所有匹配

    Returns:
        JSON 格式的结果
    """
    try:
        if mode != "replace":
            return json.dumps({"error": f"Unsupported mode: {mode}"})

        if not path or not old_string:
            return json.dumps({"error": "Missing required parameters: path, old_string"})

        resolved = _resolve_path(path)

        if not resolved.exists():
            return json.dumps({"error": f"File not found: {path}"})

        # 读取文件
        with open(resolved, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()

        # 检查是否存在
        if old_string not in content:
            return json.dumps({"error": f"String not found in file: {old_string[:50]}..."})

        # 检查唯一性
        occurrences = content.count(old_string)
        if occurrences > 1 and not replace_all:
            return json.dumps({
                "error": f"String appears {occurrences} times. Use replace_all=true to replace all."
            })

        # 执行替换
        if replace_all:
            new_content = content.replace(old_string, new_string)
        else:
            new_content = content.replace(old_string, new_string, 1)

        # 写回文件
        with open(resolved, 'w', encoding='utf-8') as f:
            f.write(new_content)

        # 注入 sentinel diff 载荷给前端 FileDiffCard 渲染红/绿 hunk。
        # 同时保留原 success/path/replacements 字段（LLM 解析向后兼容）。
        diff_payload = build_file_diff_payload(
            path=str(resolved),
            before=content,
            after=new_content,
            extra={
                "success": True,
                "replacements": occurrences if replace_all else 1,
            },
        )
        return json.dumps(diff_payload, ensure_ascii=False)

    except Exception as e:
        logger.exception(f"Error patching file {path}")
        return json.dumps({"error": str(e)})


_RG_MATCH_RE = re.compile(r'^([A-Za-z]:)?(.*?):(\d+):(.*)$')
_RG_CTX_RE = re.compile(r'^([A-Za-z]:)?(.*?)-(\d+)-(.*)$')


def _which(cmd: str) -> Optional[str]:
    """Cross-platform shutil.which, but cached lightly via local lookup."""
    import shutil
    return shutil.which(cmd)


_RG_BIN_NAME = "rg.exe" if os.name == "nt" else "rg"


def _rg_arch_tag() -> str:
    """Return Node.js-style arch-os tag for vendored ripgrep lookup.

    对齐 Claude Code / VSCode 的目录约定：`<arch>-<platform>`，如：
      x64-win32 / arm64-win32 / x64-darwin / arm64-darwin / x64-linux / arm64-linux
    这样多机器 clone 项目过来不用重新编译，按 arch 自动选对二进制。
    """
    import platform
    import sys
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64", "x64"):
        arch = "x64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    elif machine in ("i386", "i686", "x86"):
        arch = "ia32"
    else:
        arch = machine
    if sys.platform == "win32":
        plat = "win32"
    elif sys.platform == "darwin":
        plat = "darwin"
    else:
        plat = "linux"
    return f"{arch}-{plat}"


def _find_rg() -> Optional[str]:
    """Locate ripgrep. PATH > PyInstaller _MEIPASS > project-bundled vendor/ripgrep/<arch-os>/.

    查找顺序：
      1) 系统 PATH（开发机装了 rg 优先用）
      2) sys._MEIPASS（PyInstaller 单 exe 运行时；liuruyan-backend.spec 把当前架构 rg
         按 vendor/ripgrep/<arch-os>/ 路径打进 datas，运行时解压到这里）
      3) <project_root>/vendor/ripgrep/<arch-os>/（开发期从源码跑）
    """
    p = shutil.which("rg")
    if p:
        return p
    arch_tag = _rg_arch_tag()
    candidates: List[Path] = []

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "vendor" / "ripgrep" / arch_tag / _RG_BIN_NAME)

    here = Path(__file__).resolve()
    project_root = here.parents[3]
    candidates.append(project_root / "vendor" / "ripgrep" / arch_tag / _RG_BIN_NAME)

    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def _run_search_argv(argv: List[str], timeout: int = 60) -> Tuple[int, str, str]:
    """Run a search command via argv list (no shell). Returns (exit_code, stdout, stderr).

    Replaces the legacy `_local_env.execute(shell=True)` path which:
      1) breaks on patterns containing single quotes (`it's`, `dict['k']`)
      2) breaks on paths with spaces / 中文（项目根 D:\\AI_Project\\智能体\\柳如烟 必炸）
      3) opens shell injection via f-string interpolation
    """
    try:
        run_kwargs: Dict[str, Any] = {
            "shell": False,
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": timeout,
        }
        if os.name == "nt":
            run_kwargs["creationflags"] = 0x08000000
        result = subprocess.run(argv, **run_kwargs)
        return result.returncode, result.stdout or "", result.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"Search timed out after {timeout}s"
    except FileNotFoundError as e:
        return 127, "", f"Command not found: {e}"
    except Exception as e:
        return 1, "", str(e)


def _parse_rg_content_lines(stdout: str, with_context: bool) -> List[Dict[str, Any]]:
    """Parse rg/grep `file:line:content` output, Windows drive-letter safe."""
    out: List[Dict[str, Any]] = []
    for line in stdout.split('\n'):
        if not line or line == "--":
            continue
        m = _RG_MATCH_RE.match(line)
        if m:
            out.append({
                "path": (m.group(1) or "") + m.group(2),
                "line": int(m.group(3)),
                "content": m.group(4)[:500],
                "kind": "match",
            })
            continue
        if with_context:
            m = _RG_CTX_RE.match(line)
            if m:
                out.append({
                    "path": (m.group(1) or "") + m.group(2),
                    "line": int(m.group(3)),
                    "content": m.group(4)[:500],
                    "kind": "context",
                })
    return out


# 通用噪声目录"底线排除"集——任何项目都不该搜的编译产物 / 包管理器缓存。
# 哪怕 search_ignored=True 也排除这些（避免搜进几百 MB 的 build artifact）。
# 加目录前的判断：1) 跨语言/跨框架普遍存在；2) 内容是机器生成或第三方代码，对 agent
# 推理无价值；3) 体量大到会污染上下文 / 拖慢搜索。
# 不要加项目特有目录（如 _research）——那些应交给项目自己的 .gitignore 管理。
_DEFAULT_EXCLUDE_DIRS = {
    # VCS 元数据
    ".git", ".hg", ".svn",
    # 包管理器
    "node_modules",
    # Python 缓存 / 虚拟环境
    "__pycache__", ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache",
    # 通用 build / 编译产物
    "dist", "build", "out", "target",  # target = Rust/Java; out = Next.js/通用
    # 前端框架缓存
    ".next", ".turbo", ".cache", ".parcel-cache",
    # 测试覆盖率产物
    "coverage", ".nyc_output",
}


def _normalize_includes(
    includes: Optional[List[str]],
    file_glob: Optional[str],
) -> List[str]:
    """Merge legacy file_glob (single str) into includes list. Deduped, order-preserving."""
    merged: List[str] = []
    if file_glob:
        merged.append(file_glob)
    if includes:
        for g in includes:
            if isinstance(g, str) and g and g not in merged:
                merged.append(g)
    return merged


def _glob_match(path_str: str, basename: str, glob: str) -> bool:
    """fnmatch 语义，对 basename / 正常化后的 rel path 双试。支持 ** 推广。"""
    import fnmatch
    norm = path_str.replace("\\", "/")
    if fnmatch.fnmatch(basename, glob):
        return True
    if fnmatch.fnmatch(norm, glob):
        return True
    if "**" in glob and fnmatch.fnmatch(norm, glob.replace("**/", "")):
        return True
    return False


def _includes_pass(rel_path: str, basename: str, includes: List[str]) -> bool:
    """Apply rg-style includes with `!`-prefix negation. Empty list = pass-through."""
    if not includes:
        return True
    positives = [g for g in includes if not g.startswith("!")]
    negatives = [g[1:] for g in includes if g.startswith("!") and len(g) > 1]
    if positives and not any(_glob_match(rel_path, basename, g) for g in positives):
        return False
    if any(_glob_match(rel_path, basename, g) for g in negatives):
        return False
    return True


def _python_grep_fallback(
    pattern: str,
    root: Path,
    includes: List[str],
    case_sensitive: bool,
    fixed_strings: bool,
    output_mode: str,
    context_lines: int = 0,
) -> Tuple[List[Dict[str, Any]], List[str], Dict[str, int]]:
    """Pure-Python fallback when rg is unavailable.

    语义与 rg 对齐：默认排除 .git/node_modules/__pycache__ 等，多 glob includes
    支持 `!反包含`，fixed_strings/case_sensitive/context_lines 全部尊重。
    不用 grep——grep 路径在 Git for Windows 上遇 MSYS exit code 与中文路径问题。
    """
    flags = 0 if case_sensitive else re.IGNORECASE
    if fixed_strings:
        regex = re.compile(re.escape(pattern), flags)
    else:
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            raise ValueError(f"Invalid regex: {e}") from e

    matches: List[Dict[str, Any]] = []
    files_with_match: List[str] = []
    counts: Dict[str, int] = {}
    ctx = max(0, int(context_lines or 0))

    walk_target = root if root.is_dir() else root.parent
    root_abs = str(walk_target)

    for cur, dirs, files in os.walk(walk_target):
        dirs[:] = [d for d in dirs if d not in _DEFAULT_EXCLUDE_DIRS and not d.startswith(".")]
        for fname in files:
            full = os.path.join(cur, fname)
            try:
                rel = os.path.relpath(full, root_abs)
            except ValueError:
                # Windows 跨盘 / 设备文件（\\.\nul 等）relpath 会抛 ValueError
                rel = full
            if not _includes_pass(rel, fname, includes):
                continue
            try:
                with open(full, "r", encoding="utf-8", errors="ignore") as fh:
                    lines = fh.readlines() if ctx > 0 else None
                    file_count = 0
                    if ctx > 0 and lines is not None:
                        hit_idx = [i for i, ln in enumerate(lines) if regex.search(ln)]
                        file_count = len(hit_idx)
                        if output_mode == "content":
                            emitted: set = set()
                            for i in hit_idx:
                                lo = max(0, i - ctx)
                                hi = min(len(lines), i + ctx + 1)
                                for j in range(lo, hi):
                                    key = (full, j)
                                    if key in emitted:
                                        continue
                                    emitted.add(key)
                                    matches.append({
                                        "path": full,
                                        "line": j + 1,
                                        "content": lines[j].rstrip("\n")[:500],
                                        "kind": "match" if j == i or j in hit_idx else "context",
                                    })
                    else:
                        for i, line in enumerate(fh if lines is None else lines, 1):
                            if regex.search(line):
                                file_count += 1
                                if output_mode == "content":
                                    matches.append({
                                        "path": full,
                                        "line": i,
                                        "content": line.rstrip("\n")[:500],
                                        "kind": "match",
                                    })
                    if file_count > 0:
                        files_with_match.append(full)
                        counts[full] = file_count
            except (OSError, UnicodeDecodeError):
                continue
    return matches, files_with_match, counts


def _summarize_search_results(
    all_results: List[Dict[str, Any]],
    target: str,
) -> Dict[str, Any]:
    """对 grep 结果出"摘要 + 按命中数排序的 top files"（对标 Devin grep top-N）。

    在全量结果上算（不是分页 page），让模型一眼看清规模 + 哪些文件命中最密集，
    宽查询时不必把整个 matches 列表读进上下文。空结果返 {}。
    支持三种 result 形态：
      - content 模式：{path,line,content,kind} → 数 kind=="match"
      - count 模式：  {path,count}            → 直接取 count
      - filename/files 模式：{path,type}      → 每文件计 1
    """
    if not all_results:
        return {}
    counts: Dict[str, int] = {}
    for r in all_results:
        path = r.get("path")
        if not path:
            continue
        if "count" in r and isinstance(r.get("count"), int):
            counts[path] = counts.get(path, 0) + r["count"]
        elif r.get("kind") == "context":
            # context 行不计入命中数（但保证文件出现在表里）
            counts.setdefault(path, 0)
        else:
            counts[path] = counts.get(path, 0) + 1

    file_count = len(counts)
    match_total = sum(counts.values())
    top_files = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]

    summary: Dict[str, Any] = {
        "summary": f"Found {match_total} match(es) across {file_count} file(s)",
        "file_count": file_count,
    }
    # filename 模式没有"命中数"语义，top_files 只在 content/count 下有意义
    if target != "filename":
        summary["top_files"] = [{"path": p, "count": c} for p, c in top_files]
    return summary


def search_tool(
    pattern: str,
    target: str = "content",
    path: str = ".",
    includes: Optional[List[str]] = None,
    file_glob: Optional[str] = None,
    case_sensitive: bool = False,
    fixed_strings: bool = False,
    output_mode: str = "content",
    context_lines: int = 0,
    offset: int = 0,
    limit: int = 50,
    search_ignored: bool = False,
) -> str:
    """
    搜索文件内容或文件名（对齐 Cascade grep_search 语义）。

    引擎策略：rg（PATH 或项目自带 vendor/ripgrep/） → 纯 Python fallback。
    不再走 grep——Git for Windows 的 MSYS grep 在中文路径 / Windows 反斜杠下
    会返回 1536 之类的非标准退出码，已彻底剥离。

    默认行为（search_ignored=False）：rg 尊重项目 .gitignore + 不搜隐藏文件。
    底线噪声目录（_DEFAULT_EXCLUDE_DIRS：node_modules / target / dist / .git ...）
    始终被排除。这是任何项目的"干净默认值"——agent 拿到的结果不被 build 产物或
    第三方源码污染。

    需要搜被 .gitignore 屏蔽的目录（如 _research/、_archive/、生成的 docs/ 等）
    或隐藏文件 / 目录（.windsurf/、.claude/）时，传 search_ignored=True。

    Args:
        pattern: content 模式默认正则；fixed_strings=True 走字面匹配
        target: "content" | "filename"
        path: 搜索路径（文件或目录）
        includes: glob 列表，支持 `!` 反包含。例：["*.py", "!**/vendor/*"]
        file_glob: 单个 glob（legacy，已被 includes 取代；会被并入 includes）
        case_sensitive: 默认 False
        fixed_strings: True = 当字面字符串搜
        output_mode: "content" | "files" | "count"
        context_lines: >0 时返回上下文行
        offset/limit: 分页
        search_ignored: 默认 False（rg 尊重 .gitignore，结果干净）；True 时
            覆盖 .gitignore 并搜隐藏文件——用于翻 _research/、_archive/ 等
            git ignored 但内容有价值的目录。底线噪声目录始终排除，不受此参数影响。
            注意：Python fallback（rg 不可用时）总是搜全部，此参数对 fallback 无效。
    """
    if not isinstance(offset, int) or offset < 0:
        offset = 0
    if not isinstance(limit, int) or limit < 1:
        limit = 50
    if limit > 500:
        limit = 500
    FILENAME_HARD_CAP = 5000

    include_list = _normalize_includes(includes, file_glob)

    try:
        resolved = _resolve_read_path(path)
        if not resolved.exists():
            return json.dumps({"error": f"Path not found: {path}"})

        all_results: List[Dict[str, Any]] = []
        capped = False

        if target == "filename":
            import fnmatch
            pattern_lower = pattern.lower() if not case_sensitive else pattern
            walk_target = resolved if resolved.is_dir() else resolved.parent
            root_abs = str(walk_target)
            for root, dirs, files in os.walk(walk_target):
                dirs[:] = [d for d in dirs if d not in _DEFAULT_EXCLUDE_DIRS and not d.startswith(".")]
                for filename in files:
                    full_path = os.path.join(root, filename)
                    try:
                        rel = os.path.relpath(full_path, root_abs)
                    except ValueError:
                        rel = full_path
                    if not _includes_pass(rel, filename, include_list):
                        continue
                    check_name = filename if case_sensitive else filename.lower()
                    if pattern_lower in check_name:
                        all_results.append({"path": full_path, "type": "file"})
                        if len(all_results) >= FILENAME_HARD_CAP:
                            capped = True
                            break
                if capped:
                    break

        elif target == "content":
            mode = (output_mode or "content").lower()
            if mode in ("files_only", "files_with_matches", "filenames"):
                mode = "files"
            if mode not in ("content", "files", "count"):
                mode = "content"

            with_context = isinstance(context_lines, int) and context_lines > 0
            search_engine: Optional[str] = None
            search_error: Optional[str] = None

            rg_bin = _find_rg()
            if rg_bin:
                argv: List[str] = [rg_bin, "--line-number", "--no-heading", "--with-filename"]
                # 默认尊重项目 .gitignore——这是任何项目维护好的"什么不该搜"语义。
                # search_ignored=True 时覆盖 .gitignore + 搜隐藏文件，用于翻 _research/、
                # .windsurf/ 这种被忽略但内容有价值的目录。
                # 底线噪声（_DEFAULT_EXCLUDE_DIRS）任何模式下都排除——避免 search_ignored=True
                # 时把 node_modules/target/dist 等几百 MB 编译产物搜进 agent context。
                if search_ignored:
                    argv.append("--no-ignore-vcs")
                    argv.append("--hidden")
                # Windows 项目里常见 nul/aux/con 这种保留名"设备文件"，rg 试图 open
                # 它们会喷 "函数不正确 (os error 1)"。--no-messages 压住这类 IO 警告，
                # rc 处理逻辑仍兜底 stdout 优先。
                argv.append("--no-messages")
                for exc in _DEFAULT_EXCLUDE_DIRS:
                    argv.extend(["--glob", f"!**/{exc}/**"])
                if not case_sensitive:
                    argv.append("--ignore-case")
                if fixed_strings:
                    argv.append("--fixed-strings")
                if with_context:
                    argv.extend(["-C", str(int(context_lines))])
                for g in include_list:
                    argv.extend(["--glob", g])
                if mode == "files":
                    argv.append("--files-with-matches")
                elif mode == "count":
                    argv.append("--count")
                argv.extend(["--", pattern, str(resolved)])
                rc, stdout, stderr = _run_search_argv(argv, timeout=60)
                # MSYS / busybox rg 包装层偶尔把 waitpid 状态左移 8 位返回
                if rc > 255 and rc % 256 == 0:
                    rc = rc >> 8
                search_engine = "rg"
                # rg exit code 语义：0=有匹配 / 1=无匹配 / 2=错误（含个别文件打不开）。
                # 但 rc=2 时 stdout 可能仍有部分有效结果——如 _research/ 里的 Windows nul
                # 设备文件让 rg 报 "函数不正确"，stderr 喷错但 stdout 已经搜出几百条匹配。
                # 不能简单 reject——否则用户搜大目录命中任何怪文件就整个失败。
                # 策略：rc!=0/1 时若 stdout 为空才视为 fatal；否则 log 警告继续 parse。
                if rc not in (0, 1) and not stdout.strip():
                    search_error = (stderr or f"rg exit {rc}").strip()
                else:
                    if rc not in (0, 1) and (stderr or "").strip():
                        logger.warning(
                            "rg partial failure (rc=%s): %s",
                            rc, stderr.strip()[:500]
                        )
                    if mode == "files":
                        for line in stdout.split("\n"):
                            if line.strip():
                                all_results.append({"path": line, "type": "file"})
                    elif mode == "count":
                        for line in stdout.split("\n"):
                            if not line.strip():
                                continue
                            head, _, tail = line.rpartition(":")
                            if head and tail.isdigit():
                                all_results.append({"path": head, "count": int(tail)})
                    else:
                        all_results.extend(_parse_rg_content_lines(stdout, with_context))
            else:
                search_engine = "python"
                try:
                    py_matches, py_files, py_counts = _python_grep_fallback(
                        pattern, resolved, include_list, case_sensitive, fixed_strings,
                        mode, context_lines if with_context else 0,
                    )
                except ValueError as e:
                    search_error = str(e)
                else:
                    if mode == "files":
                        all_results.extend({"path": p, "type": "file"} for p in py_files)
                    elif mode == "count":
                        all_results.extend({"path": p, "count": c} for p, c in py_counts.items())
                    else:
                        all_results.extend(py_matches)

            if search_error:
                return json.dumps({
                    "error": f"Search failed ({search_engine}): {search_error}",
                    "pattern": pattern,
                    "engine": search_engine,
                }, ensure_ascii=False)

        total = len(all_results)
        page = all_results[offset:offset + limit]
        has_more = total > offset + limit or capped

        out = {
            "pattern": pattern,
            "target": target,
            "results": page,
            "count": len(page),
            "total_count": total,
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
        }

        # 摘要 + 按命中数 top files（对标 Devin grep "Found N matches across M files
        # (showing top P by match count)"）。让模型对宽查询先看汇总，不必翻完整列表。
        # 在全量 all_results 上算（不是当前 page），summary 才反映真实规模。
        summary_info = _summarize_search_results(all_results, target)
        if summary_info:
            out.update(summary_info)

        if total == 0:
            # zero-result：给 refinement 引导，避免模型对着空结果反复瞎试同一 pattern。
            out["hint"] = (
                "No matches. Try: broaden the pattern, drop case_sensitive, set fixed_strings=true "
                "for literal text, widen includes/path, or set search_ignored=true to search "
                "gitignored/hidden dirs."
            )
        if has_more:
            out["next_offset"] = offset + limit
            out["hint"] = f"More results available. Re-call with offset={offset + limit} to continue."
        if capped:
            # 显式提示总数被截断；LLM 知道 total_count 不是真总量
            out["truncated_at"] = FILENAME_HARD_CAP
            out["hint"] = (
                f"Filename scan stopped at {FILENAME_HARD_CAP} matches to bound memory. "
                f"Narrow pattern or use file_glob to filter."
            )
        return json.dumps(out, ensure_ascii=False)

    except Exception as e:
        logger.exception(f"Error searching: {pattern}")
        return json.dumps({"error": str(e)})


# ============================================================================
# 工具注册（供 Hermes registry 使用）
# ============================================================================

# clean_registry 用 handler(**all_args) 展开调用，会把 agent_id/session_id/llm_client 等
# context 参数也一起塞进来。底层 tool 函数的签名不接受这些额外 kwargs，所以 handler
# 必须用 **kwargs 兜住后主动过滤，否则会抛 TypeError。
_CONTEXT_KEYS = {"agent_id", "session_id", "llm_client", "trace_id"}


def _drop_context(kwargs):
    return {k: v for k, v in kwargs.items() if k not in _CONTEXT_KEYS}


# 历史上这里维护了 _PATH_ALIASES / _PATTERN_ALIASES + _normalize_aliases 辅助函数。
# 2026-05-11 上提到 clean_registry 统一管（参看 _PATH_ALIAS_MAP / _PATTERN_ALIAS_MAP
# + _normalize_aliases_and_validate）。dispatch 在调 handler 前已经把 alias 翻译为
# 标准 key 且拒了未知字段，所以 handler 只负责调底层 tool 函数。


def _handle_read_file(**kwargs):
    return read_file_tool(**_drop_context(kwargs))


def _handle_write_file(**kwargs):
    return write_file_tool(**_drop_context(kwargs))


def _handle_patch(**kwargs):
    return patch_tool(**_drop_context(kwargs))


def _handle_grep(**kwargs):
    # pattern 没传（且别名也未命中） → schema 的 required 会走 strict 门 拒，
    # 这里 handler 不需再提前 fast-fail。
    return search_tool(**_drop_context(kwargs))
