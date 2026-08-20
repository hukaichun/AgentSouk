from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RosterChanged:
    pass


@dataclass(frozen=True)
class LlmRosterChanged:
    pass


@dataclass(frozen=True)
class RunStatusChanged:

    run_id: str
    status: str


ChangeEvent = RosterChanged | LlmRosterChanged | RunStatusChanged
