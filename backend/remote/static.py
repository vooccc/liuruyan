"""Remote Access：StaticFiles 挂载 helper + host 默认值解析。

两件事：

1. ``_remote_host_default()`` ——
   ``backend/main.py`` 启动段（argparse ``--host`` 默认值）调用。
   读 ``remote.json``；enabled=false / 文件不存在 → 返回 "127.0.0.1"。
   这是"远程访问开关"实际生效的钩子点。

2. ``maybe_mount_frontend(app)`` ——
   把 ``frontend/dist/`` 挂载到 FastAPI 根路径 ``/``，让单端口同时服务 API + 前端 UI。
   找不到 dist → skip + log，不抛异常（自动兼容 dev 模式 vite 工作流）。

   路径解析优先级：
   - ``LIURUYAN_FRONTEND_DIST`` 环境变量（最强，方便 ops 自定义部署）
   - PyInstaller bundled：``sys._MEIPASS / "frontend_dist"``
   - 源码模式：项目根 ``frontend/dist/``

   ⚠️ 调用顺序约束：必须在所有 ``app.include_router()`` 之后调用，
      否则 StaticFiles 挂在 ``/`` 会吞掉所有 API 路径。

设计文档：``_docs/REMOTE_M8A.md`` §4.5
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI

from . import config as _config

logger = logging.getLogger(__name__)


# 模块级缓存：mount 状态供 /remote/status 端点查询用
_mount_state: dict = {"mounted": False, "dist_path": None, "skip_reason": None}


def _remote_host_default() -> str:
    """给 ``main.py`` argparse default 用：根据 ``remote.json`` 决定监听 host。

    enabled=false / 文件不存在 / 文件损坏 → 返回 "127.0.0.1"（保险默认）。
    enabled=true → 返回 config.host 字段（通常是 "0.0.0.0"）。

    ⚠️ 命令行 ``--host`` 显式传值 / ``LIURUYAN_HOST`` 环境变量 优先级更高，
       由 main.py 那边的 ``os.environ.get(...) or _remote_host_default()`` 控制。
    """
    try:
        cfg = _config.load_config()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Remote] _remote_host_default 读取失败，回退 127.0.0.1：%s", exc)
        return _config.DEFAULT_HOST
    if not cfg.enabled:
        return _config.DEFAULT_HOST
    return cfg.host


def _resolve_dist_dir() -> Optional[Path]:
    """按优先级解析 ``frontend/dist/`` 目录。找不到返 None（调用方 skip 挂载）。"""
    # 1. 环境变量优先（自定义部署、CI、容器场景）
    env_path = os.environ.get("LIURUYAN_FRONTEND_DIST")
    if env_path:
        p = Path(env_path).expanduser().resolve()
        if (p / "index.html").is_file():
            return p
        # 配了但是不存在 → 给个警告，但不要 raise（让 skip 接管）
        logger.warning("[Remote] LIURUYAN_FRONTEND_DIST=%r 未找到 index.html，忽略此设置", env_path)

    # 2. PyInstaller bundled（生产打包）：sys._MEIPASS/frontend_dist
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = Path(meipass) / "frontend_dist"
        if (bundled / "index.html").is_file():
            return bundled

    # 3. 源码模式：项目根 frontend/dist
    #    backend/remote/static.py → backend/remote → backend → 项目根
    project_root = Path(__file__).resolve().parent.parent.parent
    src_dist = project_root / "frontend" / "dist"
    if (src_dist / "index.html").is_file():
        return src_dist

    return None


def maybe_mount_frontend(app: FastAPI) -> None:
    """条件挂载 ``frontend/dist/`` 到 FastAPI 根路径 ``/``。

    - 找不到 dist → skip + log（dev 模式 vite 跑 :5173 时这是正常路径）
    - 找到了 → ``app.mount("/", StaticFiles(directory=..., html=True), name="frontend")``
    - ``html=True`` 让根路径自动返 ``index.html``，前端 SPA 路由由前端 router 接管
    - 必须在所有 ``include_router`` 之后调用——FastAPI 路由是先注册先匹配，
      具体路径（如 /api/*、/gateway/*）会优先于 StaticFiles 的 ``/``

    参数：
        app: 主 FastAPI 实例

    无返回值。挂载状态写入模块级 ``_mount_state``，供 /remote/status 查询。
    """
    from fastapi.staticfiles import StaticFiles  # 延迟 import，避免在测试 fixture 里就触发

    dist = _resolve_dist_dir()
    if dist is None:
        _mount_state.update(
            {"mounted": False, "dist_path": None, "skip_reason": "dist_not_found"}
        )
        logger.info("[Remote] frontend/dist 未找到，跳过 StaticFiles 挂载（dev 模式正常路径）")
        return

    try:
        app.mount(
            "/",
            StaticFiles(directory=str(dist), html=True),
            name="frontend",
        )
    except Exception as exc:  # noqa: BLE001
        _mount_state.update(
            {"mounted": False, "dist_path": str(dist), "skip_reason": f"mount_error: {exc}"}
        )
        logger.error("[Remote] StaticFiles 挂载失败：%s", exc)
        return

    _mount_state.update({"mounted": True, "dist_path": str(dist), "skip_reason": None})
    logger.info("[Remote] frontend/dist 已挂载到 /：%s", dist)


def get_mount_state() -> dict:
    """供 /remote/status 端点读取挂载结果（深拷贝避免外部改动污染）。"""
    return dict(_mount_state)


__all__ = ["_remote_host_default", "maybe_mount_frontend", "get_mount_state"]
