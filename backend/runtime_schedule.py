import os
from dataclasses import dataclass
from typing import Iterable

from .runtime_budget import AggregateRuntimeBudget, RuntimeBudgetNotice


def _read_int_env(*names: str, default: int) -> int:
    for name in names:
        raw = os.getenv(name)
        if raw is None or raw == "":
            continue
        try:
            return max(1, int(raw))
        except ValueError:
            continue
    return default


@dataclass(frozen=True)
class RuntimeConvergencePlan:
    notice_kind: str
    notice_message: str
    prompt_message: str
    notice_payload: dict | None = None


@dataclass(frozen=True)
class RuntimeSchedulerLimits:
    max_concurrent_workers: int
    max_running_workers_per_session: int
    max_enqueued_workers_per_session: int


@dataclass(frozen=True)
class WorkerScheduleCandidate:
    task_id: str
    parent_session_id: str
    priority: int
    queue_order: int
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkerDependencySnapshot:
    task_id: str
    status: str
    error: str | None = None
    cancelled: bool = False


@dataclass(frozen=True)
class WorkerDependencyState:
    ready: bool
    waiting_on: tuple[str, ...]
    failed_dependencies: tuple[WorkerDependencySnapshot, ...]


@dataclass(frozen=True)
class WorkerPlanStepInput:
    step_id: str
    description: str
    priority: int = 0
    timeout: int = 300
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkerPlanStep:
    step_id: str
    description: str
    priority: int
    timeout: int
    depends_on: tuple[str, ...]
    stage: int


def build_budget_convergence_plan(
    budget: AggregateRuntimeBudget,
) -> RuntimeConvergencePlan | None:
    notice: RuntimeBudgetNotice | None = budget.build_near_limit_notice()
    if not notice:
        return None
    return RuntimeConvergencePlan(
        notice_kind=notice.kind,
        notice_message=notice.message,
        prompt_message=notice.prompt,
        notice_payload=notice.payload,
    )


def get_runtime_scheduler_limits() -> RuntimeSchedulerLimits:
    return RuntimeSchedulerLimits(
        max_concurrent_workers=_read_int_env(
            "WORKER_MAX_CONCURRENT",
            "RUNTIME_MAX_CONCURRENT_WORKERS",
            default=4,
        ),
        max_running_workers_per_session=_read_int_env(
            "WORKER_MAX_RUNNING_PER_SESSION",
            "WORKER_MAX_PER_SESSION",
            "RUNTIME_MAX_WORKERS_PER_SESSION",
            default=4,
        ),
        max_enqueued_workers_per_session=_read_int_env(
            "WORKER_MAX_ENQUEUED_PER_SESSION",
            "RUNTIME_MAX_ENQUEUED_WORKERS_PER_SESSION",
            default=8,
        ),
    )


def resolve_worker_dependencies(
    candidate: WorkerScheduleCandidate,
    dependency_snapshots: dict[str, WorkerDependencySnapshot],
) -> WorkerDependencyState:
    waiting_on: list[str] = []
    failed_dependencies: list[WorkerDependencySnapshot] = []

    for dependency_id in candidate.depends_on:
        dependency = dependency_snapshots.get(dependency_id)
        if dependency is None:
            failed_dependencies.append(
                WorkerDependencySnapshot(
                    task_id=dependency_id,
                    status="missing",
                    error="Dependency worker not found.",
                )
            )
            continue

        if dependency.status == "completed":
            continue

        if dependency.cancelled or dependency.status == "failed":
            failed_dependencies.append(dependency)
            continue

        waiting_on.append(dependency_id)

    return WorkerDependencyState(
        ready=not waiting_on and not failed_dependencies,
        waiting_on=tuple(waiting_on),
        failed_dependencies=tuple(failed_dependencies),
    )


def normalize_worker_plan_steps(
    steps: Iterable[WorkerPlanStepInput],
) -> list[WorkerPlanStep]:
    normalized_inputs: list[WorkerPlanStepInput] = []
    steps_by_id: dict[str, WorkerPlanStepInput] = {}

    for raw_step in steps:
        step_id = raw_step.step_id.strip()
        if not step_id:
            raise ValueError("Worker plan step_id cannot be empty.")
        if step_id in steps_by_id:
            raise ValueError(f"Duplicate worker plan step_id: {step_id}")

        description = raw_step.description.strip()
        if not description:
            raise ValueError(f"Worker plan step {step_id} must have a description.")

        seen_dependencies: set[str] = set()
        normalized_dependencies: list[str] = []
        for dependency_id in raw_step.depends_on:
            value = dependency_id.strip()
            if not value or value in seen_dependencies:
                continue
            if value == step_id:
                raise ValueError(f"Worker plan step {step_id} cannot depend on itself.")
            seen_dependencies.add(value)
            normalized_dependencies.append(value)

        normalized_step = WorkerPlanStepInput(
            step_id=step_id,
            description=description,
            priority=raw_step.priority,
            timeout=raw_step.timeout,
            depends_on=tuple(normalized_dependencies),
        )
        normalized_inputs.append(normalized_step)
        steps_by_id[step_id] = normalized_step

    if not normalized_inputs:
        raise ValueError("Worker plan must contain at least one step.")

    for step in normalized_inputs:
        for dependency_id in step.depends_on:
            if dependency_id not in steps_by_id:
                raise ValueError(
                    f"Worker plan step {step.step_id} depends on unknown step {dependency_id}."
                )

    stage_cache: dict[str, int] = {}
    visiting: set[str] = set()

    def resolve_stage(step_id: str) -> int:
        if step_id in stage_cache:
            return stage_cache[step_id]
        if step_id in visiting:
            raise ValueError(f"Worker plan contains a dependency cycle at step {step_id}.")

        visiting.add(step_id)
        step = steps_by_id[step_id]
        if not step.depends_on:
            stage = 0
        else:
            stage = max(resolve_stage(dependency_id) for dependency_id in step.depends_on) + 1
        visiting.remove(step_id)
        stage_cache[step_id] = stage
        return stage

    normalized_steps: list[WorkerPlanStep] = []
    for step in normalized_inputs:
        normalized_steps.append(
            WorkerPlanStep(
                step_id=step.step_id,
                description=step.description,
                priority=step.priority,
                timeout=step.timeout,
                depends_on=step.depends_on,
                stage=resolve_stage(step.step_id),
            )
        )

    return normalized_steps


def select_runtime_dispatches(
    candidates: Iterable[WorkerScheduleCandidate],
    limits: RuntimeSchedulerLimits,
    *,
    running_total: int,
    running_by_session: dict[str, int],
) -> list[str]:
    remaining_global_slots = max(0, limits.max_concurrent_workers - running_total)
    if remaining_global_slots <= 0:
        return []

    selected: list[str] = []
    projected_running_by_session = dict(running_by_session)
    ordered_candidates = sorted(
        candidates,
        key=lambda candidate: (-candidate.priority, candidate.queue_order, candidate.task_id),
    )

    for candidate in ordered_candidates:
        if remaining_global_slots <= 0:
            break

        session_running = projected_running_by_session.get(candidate.parent_session_id, 0)
        if session_running >= limits.max_running_workers_per_session:
            continue

        selected.append(candidate.task_id)
        projected_running_by_session[candidate.parent_session_id] = session_running + 1
        remaining_global_slots -= 1

    return selected


WorkerSchedulerLimits = RuntimeSchedulerLimits
get_worker_scheduler_limits = get_runtime_scheduler_limits
select_worker_dispatches = select_runtime_dispatches
