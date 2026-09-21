from backend.runtime_startup_policy import should_trigger_plan_startup_fallback


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_plan_startup_fallback_only_when_no_step_has_started() -> None:
    assert_true(
        should_trigger_plan_startup_fallback(
            {
                "steps": [
                    {"step_id": "entry_scan", "status": "queued"},
                    {"step_id": "logic_analyze", "status": "pending"},
                ]
            }
        ),
        "all-pending/queued plans should still trigger startup fallback",
    )

    assert_true(
        not should_trigger_plan_startup_fallback(
            {
                "steps": [
                    {"step_id": "entry_scan", "status": "running"},
                    {"step_id": "logic_analyze", "status": "queued"},
                ]
            }
        ),
        "a plan with at least one running step should not trigger startup fallback",
    )

    assert_true(
        not should_trigger_plan_startup_fallback(
            {
                "steps": [
                    {"step_id": "entry_scan", "status": "completed"},
                    {"step_id": "logic_analyze", "status": "queued"},
                ]
            }
        ),
        "dependency-blocked downstream steps should not cause fallback after an upstream step started",
    )


def main() -> None:
    test_plan_startup_fallback_only_when_no_step_has_started()
    print("runtime-startup-regression: ok")
    print("- plan startup fallback only fires when no step has started")


if __name__ == "__main__":
    main()
