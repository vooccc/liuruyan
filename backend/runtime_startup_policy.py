from typing import Any, Dict, Optional


def should_trigger_plan_startup_fallback(plan_detail: Optional[Dict[str, Any]]) -> bool:
    steps = (plan_detail or {}).get("steps") or []
    if not steps:
        return True
    return not any(
        (step.get("status") or "") not in ("pending", "queued")
        for step in steps
    )
