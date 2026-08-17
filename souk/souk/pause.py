from typing import Any


def interrupt_outcome_of(event: dict) -> list[dict[str, Any]] | None:
    if event.get("type") != "RUN_FINISHED":
        return None
    outcome = event.get("outcome")
    if not isinstance(outcome, dict) or outcome.get("type") != "interrupt":
        return None
    return outcome.get("interrupts") or []


def is_resuming(active_run: dict[str, Any] | None, resume: list[dict[str, Any]] | None) -> bool:
    return (
        active_run is not None
        and bool(resume)
        and active_run["status"] == "input-required"
    )
