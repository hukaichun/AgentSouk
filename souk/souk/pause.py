from typing import Any


def interrupt_outcome_of(event: dict) -> list[dict[str, Any]] | None:
    """Returns the list of interrupts (possibly empty) if `event` is a
    RUN_FINISHED with an interrupt outcome, else None — including for a
    RUN_FINISHED with a plain success outcome or no outcome at all."""
    if event.get("type") != "RUN_FINISHED":
        return None
    outcome = event.get("outcome")
    if not isinstance(outcome, dict) or outcome.get("type") != "interrupt":
        return None
    return outcome.get("interrupts") or []
