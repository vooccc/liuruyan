"""柳如烟 ACP server stdio entry。

启动：
    python -m backend.acp.server

关键设计（@docs/acp_integration_design.md §5.3.1）：
- stdout 留给 ACP JSON-RPC 帧；所有日志/print 重定向到 stderr
- ACP SDK 用底层 fd 1（asyncio.streams 包 sys.stdout），不会被 sys.stdout = sys.stderr 影响；
  但业务代码里的 print() 会被重定向，避免污染协议帧
- Ctrl+C 优雅退出
"""

from __future__ import annotations

import asyncio
import logging
import sys


class _StdoutShimForACP:
    """伪装成 sys.stdout 的代理：text 写走 stderr，.buffer 留给 ACP SDK 写 JSON 帧。

    背景（Phase 3 踩坑）：
    - backend 业务代码到处是 `print(...)`，Phase 3 接 Agent.chat 后调用链很深无法逐一审计
    - print() 走 sys.stdout.write → 跟 ACP 的 sys.stdout.buffer.write(json_bytes) 共享 fd 1
    - 在 Windows 上 print 还会用 GBK 编码，碎帧 + 编码混杂 → 客户端 JSON 解析挂
    - Phase 1 试过 `sys.stdout = sys.stderr`，但 ACP SDK 的 `sys.stdout.buffer.write` 也跟着跑去 stderr 导致客户端收不到响应
    - 最终方案：只重定向 text 通道（print 走的 .write()），保留 binary 通道（.buffer）给 ACP

    注意：极少数代码用 os.write(1, data) 绕过 sys.stdout，本 shim 不拦；目前 backend 没这种用法。
    """

    def __init__(self, real_stdout):
        # 保留原 stdout 的 binary buffer 给 ACP SDK
        self.buffer = real_stdout.buffer
        self._real = real_stdout

    def write(self, text):
        # text 走 stderr，跟 logging 一起
        return sys.stderr.write(text)

    def flush(self):
        try:
            sys.stderr.flush()
        except Exception:
            pass

    def fileno(self):
        # 部分库通过 fileno() 拿原 fd，仍指 fd 1（绕过 shim）；
        # 不主动暴露给业务，但保留给 SDK 兜底
        return self._real.fileno()

    def isatty(self):
        return False

    def writable(self):
        return True

    def __getattr__(self, name):
        # 兜底：未识别属性走原 stdout（避免某些库读 .encoding / .closed 等 attribute 时炸）
        return getattr(self._real, name)


def _setup_stdout_isolation() -> None:
    """把 sys.stdout 换成 shim，让 print 走 stderr 但 ACP SDK 仍写 fd 1。

    注：fileno()/buffer 仍指原 fd 1，所以任何用 sys.stdout.buffer 或 sys.stdout.fileno()
    再 os.write 的代码（包括 ACP SDK）都还正常输出到 fd 1。
    """
    if not hasattr(sys.stdout, "buffer"):
        # 极少数环境（嵌入解释器）没 buffer 属性，跳过
        return
    sys.stdout = _StdoutShimForACP(sys.stdout)  # type: ignore[assignment]
    # stderr 强制 UTF-8（业务 print 现在走 stderr，避免 Windows GBK 编码乱码）
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass


def _setup_logging() -> None:
    """所有日志 → stderr，留出 stdout 给 ACP JSON-RPC。"""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    # ⚠️ 注意：**不能** sys.stdout = sys.stderr。
    # ACP SDK 在 stdio.py:_StdoutTransport.write 里调 `sys.stdout.buffer.write(data)`，
    # 是动态查找 sys.stdout，赋值会把 ACP JSON-RPC 帧也拐到 stderr 上，导致 client 收不到响应。
    # 业务代码里的 print() 默认走 sys.stdout，目前 Phase 1 不接业务代码所以安全；
    # 后续接 LLMClient/工具循环时，需要审计调用链里的 print，全部改成 logger.info/debug。
    # 如果实在需要捕获 print，方案是用 contextlib.redirect_stdout(sys.stderr) 包住 run_agent
    # 之外的逻辑，而 ACP 自己跑在原始 stdout 上。

    # 降噪：依赖库的 noisy log
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)


async def _amain() -> None:
    """异步主入口。"""
    import acp  # 延迟 import，确保 logging 已配置

    from .agent import LiuruyanACPAgent

    logger = logging.getLogger(__name__)
    logger.info("Starting 柳如烟 ACP server (Phase 1)")
    logger.info("ACP SDK version check: PROTOCOL_VERSION=%s", acp.PROTOCOL_VERSION)

    agent = LiuruyanACPAgent()

    try:
        # stdio 缓冲调到 16MB：默认 64KB 单条 JSON-RPC 行装不下大 prompt / read_text_file 响应，
        # 会触发 LimitOverrunError 把整条 ACP 连接打断。
        await acp.run_agent(agent, stdio_buffer_limit_bytes=16 * 1024 * 1024)
    except asyncio.CancelledError:
        logger.info("ACP run cancelled")
        raise
    except Exception:
        logger.exception("ACP agent crashed")
        raise


def main() -> None:
    """同步主入口。先配 logging（必须最先），然后跑 asyncio。"""
    # ⚠️ 顺序敏感：stdout 隔离必须最先，否则中间任何 import 时的 print 都会污染 ACP 帧
    _setup_stdout_isolation()
    _setup_logging()
    logger = logging.getLogger(__name__)

    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        logger.info("Shutting down (KeyboardInterrupt)")
        sys.exit(0)
    except Exception:
        logger.exception("Fatal error in ACP server")
        sys.exit(1)


if __name__ == "__main__":
    main()
