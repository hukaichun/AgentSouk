"""souk's own state, as models rather than bare dicts.

`docs/library-architecture.md`'s "Typed data, and where typing stops" has
committed to this for a while — *anything souk constructs or owns is a
pydantic model* — and named today's situation a wart rather than a choice.
These are the query methods' half of that promise.

The point is not type annotations for their own sake. Souk's field names were
only ever written down in `repo.py`'s row-building, so every consumer learned
them by reading it: the gateway (now a separate repository) had a hand-written
model listing exactly these keys, kept in step by nobody. Naming them where
the data is produced means a rename breaks at the source instead of somewhere
downstream that guessed.

**Where typing stops is unchanged.** The relayed event stream stays
`dict`-shaped on purpose — a provider running a newer AG-UI than souk must
not have its events rejected by souk's copy of the enum. That reasoning is in
the same doc section and is not what these models are about; these describe
rows souk itself writes.

Two agent models rather than one, because the roster and the record are
genuinely different questions. `AgentSummary` answers "what is on offer" — a
public listing, with `online` computed against the staleness window.
`AgentRecord` answers "what did this agent register" — the card and metadata
souk stored, with no derived fields. Merging them would mean one model whose
half the fields are absent depending on which call produced it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AgentSummary(BaseModel):
    """One row of the public roster — see `Souk.list_agents`.

    `online` is derived, not stored: it is `last_seen_at` measured against
    `CoreSettings.online_window_seconds` at the moment of the query, so two
    calls a minute apart can legitimately disagree.
    """

    agent_id: str
    name: str
    description: str = ""
    skills: list[dict[str, Any]] = Field(default_factory=list)
    joined_at: datetime
    last_seen_at: datetime
    online: bool
    # Whoever holds this key owns this agent (see repo.register_agents).
    # `provider_name` is the optional storefront label for that key — None
    # means that key never set one, not that it was cleared.
    public_key: str
    provider_name: str | None = None


class AgentRecord(BaseModel):
    """One agent as souk stored it — see `Souk.get_agent`."""

    agent_id: str
    name: str
    agent_card: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    joined_at: datetime
    last_seen_at: datetime


class RunRecord(BaseModel):
    """One run — see `Souk.get_run`.

    Deliberately narrower than the row it comes from. Runs are stored in
    `thread_history` alongside messages, so `select(thread_history)` also
    handed back `id`, `kind`, `message_id` and `message_json` — the columns
    that make that sharing work, and meaningless as facts about a run. They
    were never read by anything (checked across the repo, not assumed), and
    a caller that started to would be depending on where souk keeps runs
    rather than on what a run is.
    """

    run_id: str
    thread_id: str
    agent_id: str
    # "ag-ui" | "a2a" — which protocol started this run.
    protocol: str
    status: str
    input_json: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    # None until the run is claimed / reaches each of these points. A paused
    # ('input-required') run has no completed_at on purpose — it is not done,
    # it is waiting; see the CHECK constraint in souk/schema.py.
    started_at: datetime | None = None
    completed_at: datetime | None = None
    last_activity_at: datetime | None = None
