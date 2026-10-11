"""v2 · B-10 · ThreadDataMiddleware

把 5 个虚拟挂载点（workspace / uploads / outputs / agent-data / skills）+
对应的物理路径写入 ``state["thread_data"]``，让后续 mw 03 / mw 05 / 工具入口
都能拿到稳定的 prompt 字符串和真实的物理路径。

参考：
- docs/architecture_v2/02_runtime_core.md § 六（数据目录布局 + 虚拟路径）
- docs/architecture_v2/03_middlewares.md § 02

钩子：``before_agent``

输入：从 ContextVar / RunContext 拿 user_id / agent_id / session_id；
       base 通过 env ``LIURUYAN_DATA_DIR`` 兜底。

输出（state["thread_data"]）：

    {
        # agent prompt 看到的稳定虚拟路径（mw 03/05/14 + prompt 拼装层用）
        "workspace_path":   "/mnt/user-data/workspace" or None,
        "uploads_path":     "/mnt/user-data/uploads"   or None,
        "outputs_path":     "/mnt/user-data/outputs"   or None,
        "agent_data_path":  "/mnt/agent-data",
        "skills_path":      "/mnt/skills",

        # 物理路径（工具入口最外层把虚拟翻译成这些；测试 / mw 12 也直接用）
        "physical_workspace":   "/abs/.../workspace" or None,
        "physical_uploads":     "/abs/.../uploads"   or None,
        "physical_outputs":     "/abs/.../outputs"   or None,
        "physical_agent_data":  "/abs/.../data",
        "physical_skills":      "/abs/skills",
        "physical_base":        "/abs/.../threads/tid" or None,

        # 元数据
        "user_id":    "uid",
        "agent_id":   "aid",
        "session_id": "tid" or None,
        "base":       "/abs/data/dir",
    }

关键决策：

1. **lazy_init** — 永远不预创建物理目录。第一次写文件时由 ``atomic_write_json``
   等调用方 ``mkdir(parents=True, exist_ok=True)`` 现场建。原因：登录页 / 主聊天
   选角面板这种 short-lived run 不该留空目录残骸；一律按"用到才建"。

2. **session_id 缺失时降级** — ``user-data/*`` 三个挂载点写 None
   （而不是 raise）；``agent-data`` / ``skills`` 不需要 session_id 仍正常填。
   理由：早期 run（IM 入口握手期、subagent fan-out 头几毫秒）可能还没分配
   session；硬 raise 会杀整个 run。下游 mw / 工具看到 ``physical_workspace
   is None`` 就知道不能写工作区，自己降级即可。

3. **异常 swallow** — 跟 03_middlewares § 一契约一致；只有 ContextSetup 例外。
   失败时仍写一个**最小占位**（只含 user_id / agent_id），让后续 mw 看到
   ``thread_data`` 字段存在不至于 KeyError。

4. **不调 to_virtual** — 虚拟路径是常量字面量，直接硬编码（``virtual_path.py``
   也是这么定义的，重复一次更直观，避免 import 反向开销）。物理路径走
   ``from_virtual``，自然走 virtual_path 的穿越校验。
"""

from __future__ import annotations

import logging
from typing import Any

from ..sandbox.virtual_path import (
    VirtualPathContext,
    VirtualPathError,
    from_context,
    from_virtual,
    get_default_base,
)
from .base import AgentMiddleware, RunContext, ThreadState

log = logging.getLogger(__name__)


# 5 个虚拟挂载点常量。跟 sandbox.virtual_path._VIRTUAL_MOUNTS 对齐；
# 这里再写一次是为了避免反向 import 私有常量。
_VP_WORKSPACE = "/mnt/user-data/workspace"
_VP_UPLOADS = "/mnt/user-data/uploads"
_VP_OUTPUTS = "/mnt/user-data/outputs"
_VP_AGENT_DATA = "/mnt/agent-data"
_VP_SKILLS = "/mnt/skills"


class ThreadDataMiddleware(AgentMiddleware):
    name = "thread_data"

    async def before_agent(
        self, state: ThreadState, ctx: RunContext
    ) -> dict[str, Any] | None:
        try:
            vpc = self._build_vpc(ctx)
            thread_data = self._compute_thread_data(vpc, ctx)
            return {"thread_data": thread_data}
        except Exception:
            log.exception(
                "ThreadDataMiddleware swallowed exception (run=%s user=%s agent=%s session=%s)",
                ctx.run_id, ctx.user_id, ctx.agent_id, ctx.session_id,
            )
            # 兜底：写最小占位让下游不要 KeyError
            return {
                "thread_data": {
                    "user_id": ctx.user_id,
                    "agent_id": ctx.agent_id,
                    "session_id": ctx.session_id,
                    "_error": "thread_data_compute_failed",
                }
            }

    # ── 内部 ────────────────────────────────────────────────────────────

    @staticmethod
    def _build_vpc(ctx: RunContext) -> VirtualPathContext:
        """优先用 ctx 的字段；ContextVar 兜底（ContextSetup 已先跑）。

        显式传 ctx.user_id / ctx.agent_id 是为了：即便 ContextSetup 没 set 成功
        （理论上不会，因为它是 STRICT），从 RunContext 也能拿到一致值。
        session_id 显式传 ctx.session_id（可能 None）。
        base 默认从 LIURUYAN_DATA_DIR env。
        """
        return from_context(
            user_id=ctx.user_id,
            agent_id=ctx.agent_id,
            session_id=ctx.session_id,
            base=get_default_base(),
        )

    @staticmethod
    def _compute_thread_data(
        vpc: VirtualPathContext, ctx: RunContext
    ) -> dict[str, Any]:
        # agent-data / skills 永远可解（不需要 session_id）
        physical_agent_data = str(from_virtual(_VP_AGENT_DATA, vpc))
        physical_skills = str(from_virtual(_VP_SKILLS, vpc))

        # user-data/* 需要 session_id；缺失时三个一起降级 None
        physical_workspace: str | None = None
        physical_uploads: str | None = None
        physical_outputs: str | None = None
        physical_base: str | None = None
        workspace_path: str | None = None
        uploads_path: str | None = None
        outputs_path: str | None = None

        if vpc.session_id:
            try:
                physical_workspace = str(from_virtual(_VP_WORKSPACE, vpc))
                physical_uploads = str(from_virtual(_VP_UPLOADS, vpc))
                physical_outputs = str(from_virtual(_VP_OUTPUTS, vpc))
                # physical_base = thread root（不含 workspace 等子目录）
                # 不调 from_virtual（没有 /mnt/user-data 顶层挂载点），
                # 直接复用 base 拼路径。
                physical_base = str(
                    vpc.base
                    / "users"
                    / vpc.user_id
                    / "agents"
                    / vpc.agent_id
                    / "threads"
                    / vpc.session_id
                )
                workspace_path = _VP_WORKSPACE
                uploads_path = _VP_UPLOADS
                outputs_path = _VP_OUTPUTS
            except VirtualPathError:
                # 极罕见：from_virtual 配 session_id 仍 raise（如 OS 拒绝 resolve）。
                # 三个一起降级 None，agent-data / skills 仍可用。
                log.warning(
                    "ThreadDataMiddleware: user-data/* resolve failed (session=%s); "
                    "falling back to None",
                    vpc.session_id,
                )

        return {
            # virtual（agent prompt 用）
            "workspace_path": workspace_path,
            "uploads_path": uploads_path,
            "outputs_path": outputs_path,
            "agent_data_path": _VP_AGENT_DATA,
            "skills_path": _VP_SKILLS,
            # physical（工具入口 / mw 12 用）
            "physical_workspace": physical_workspace,
            "physical_uploads": physical_uploads,
            "physical_outputs": physical_outputs,
            "physical_agent_data": physical_agent_data,
            "physical_skills": physical_skills,
            "physical_base": physical_base,
            # 元数据
            "user_id": vpc.user_id,
            "agent_id": vpc.agent_id,
            "session_id": vpc.session_id,
            "base": str(vpc.base),
        }


__all__ = ["ThreadDataMiddleware"]
