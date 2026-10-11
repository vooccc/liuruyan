"""Remote Access 子系统（M8a）——远程访问 + 单端口 + PWA。

聚合导出：
    - ``router``                FastAPI APIRouter，挂到主 app
    - ``maybe_mount_frontend``  把 frontend/dist 条件挂载到根路径 /
    - ``_remote_host_default``  给 main.py argparse 读默认监听 host

入口设计：``backend/main.py`` 在 router 挂载完毕后追加一段：

    try:
        from backend.remote import router as _remote_router, maybe_mount_frontend
        app.include_router(_remote_router)
        maybe_mount_frontend(app)
    except Exception as _remote_exc:
        print(f"[Remote] routes registration failed (non-fatal): {_remote_exc}")

并在启动段 argparse 处：

    from backend.remote import _remote_host_default
    parser.add_argument("--host", default=os.environ.get("LIURUYAN_HOST") or _remote_host_default())

砍除路径：见 ``远程安全隔离规范`` §三。
设计文档：``docs/REMOTE_SPEC.md``。
"""
from __future__ import annotations

from .routes import router
from .static import _remote_host_default, maybe_mount_frontend

__all__ = ["router", "_remote_host_default", "maybe_mount_frontend"]
