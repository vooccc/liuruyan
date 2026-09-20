"""v2 · F-1' · v1 subagent kind 注册到 v2 registry

设计：_docs/architecture_v2/01_master_plan.md Phase F + handover_v2_phase_f_protocol_landed.md

把 v1 已有的 subagent kind 登记到 v2 ``AgentKindRegistry``——
让 registry 反映真实状态（``/healthz`` 之类未来可查"已注册哪些 kind"）。

当前保留为 subagent-only placeholder 注册 helper；worker / worker_plan factories 已下线。
"""

from __future__ import annotations

import logging

from liuruyan.subagents import AgentKind, get_default_registry

log = logging.getLogger(__name__)


# ── placeholder factories（F-3+ 替换）──────────────────────────────────


async def _placeholder_subagent_factory(*args, **kwargs):
    raise NotImplementedError(
        "subagent kind placeholder factory; F-3 will wire v1 subagent.engine.spawn_subagent here"
    )
    yield


# ── 注册 ────────────────────────────────────────────────────────────────


_DEFAULT_KINDS = (
    AgentKind(
        kind="subagent",
        factory=_placeholder_subagent_factory,
        max_concurrent=3,                # subagent 阻塞主对话
        default_timeout_s=300.0,         # 5 min 短任务
        can_cancel=True,
        metadata={"v1_module": "backend.subagent.engine", "phase": "F-1' placeholder"},
    ),
)


def register_v1_subagents(*, overwrite: bool = False) -> list[str]:
    """把 v1 subagent placeholder kind 登记到 v2 registry。

    返已注册的 kind 名列表。

    多次调用 idempotent：默认 ``overwrite=False`` 时遇到已注册的 kind 静默
    跳过；显式 ``overwrite=True`` 时强制替换（生产很少用，主要给测试 /
    F-2 真 factory 上线时切换）。

    异常隔离：单个 kind 注册失败不阻断其它（log + 继续）。
    """
    registry = get_default_registry()
    registered: list[str] = []
    for kind in _DEFAULT_KINDS:
        try:
            existing = registry.get(kind.kind)
            if existing is not None and not overwrite:
                # 已注册 → 跳过 placeholder
                log.debug(
                    "[v2:subagent_bootstrap] kind %r already registered; skipping placeholder",
                    kind.kind,
                )
                continue
            registry.register(kind, overwrite=overwrite)
            registered.append(kind.kind)
            log.debug(
                "[v2:subagent_bootstrap] registered kind=%r placeholder factory",
                kind.kind,
            )
        except Exception:
            log.exception(
                "[v2:subagent_bootstrap] failed to register kind=%r",
                kind.kind,
            )

    if registered:
        log.info(
            "[v2:subagent_bootstrap] registered %d kind(s): %s",
            len(registered), ", ".join(registered),
        )
    return registered


__all__ = ["register_v1_subagents"]
