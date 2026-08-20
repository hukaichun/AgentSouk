from __future__ import annotations

from typing import Any


class LiveRoster:
    """The live table a dispatch host keeps: which connection serves each ref, and per-identity counters of what funduq observed.

    RunBroker brokers runs — queues, offers, negotiated acceptance — and
    KyokRelay relays completions — pass-through, no queue, fast-fail. They
    are different machines and deliberately keep different names; the
    roster aspect of each is the same machine, and it lives here once so
    the two can't drift apart — they did while it was two hand-kept
    copies (the LLM side lost its withdraw step). Semantics: one
    connection per role (a re-attach under the same ref replaces it),
    withdrawal by ref, counters recorded and never judged.

    Counter names are fixed per host at construction: the *subjects* each
    host can observe differ; the bookkeeping doesn't.
    """

    def __init__(self, counter_names: tuple[str, ...]) -> None:
        self._links: dict[Any, Any] = {}
        self._counter_names = counter_names
        self._counters: dict[str, dict[str, int]] = {}

    def attach(self, mapping: dict[Any, Any]) -> None:
        self._links.update(mapping)

    def withdraw(self, refs: list[Any]) -> None:
        for ref in refs:
            self._links.pop(ref, None)

    def serving(self, ref: Any) -> Any | None:
        return self._links.get(ref)

    def served_by(self, public_key: str) -> list[Any]:
        return [ref for ref in self._links if ref.provider_key == public_key]

    def note(self, public_key: str, counter: str) -> None:
        if counter not in self._counter_names:
            raise ValueError(f"unknown counter {counter!r}")
        bucket = self._counters.setdefault(
            public_key, dict.fromkeys(self._counter_names, 0)
        )
        bucket[counter] += 1

    def count(self, public_key: str, counter: str) -> int:
        return self._counters.get(public_key, {}).get(counter, 0)

    def counters(self) -> dict[str, dict[str, int]]:
        return {key: dict(bucket) for key, bucket in self._counters.items()}
