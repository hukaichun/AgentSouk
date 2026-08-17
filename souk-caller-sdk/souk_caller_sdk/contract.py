"""What this package requires of whoever wires it up, written down so they
can check it.

The same device as `souk_provider_sdk.contract`, and for the same reason. A
caller cannot import souk — that is the boundary — and this package does not
reach the other way either: a completion arrives as `PendingCompletion`, this
package's own type, and answers leave through a `CallerLink`. So souk's field
names and method arities are the integrator's business, not this package's.

That trade has been paid for once already on the provider side. The loop there
read `run.run_id` and `run.run_input` off whatever souk handed it; souk handed
over its own dispatch object, whose input field is `input_json`, and the first
real provider died with an `AttributeError` on its first run — with nothing red
anywhere. One adapter that knowingly names both sides beats two codebases
silently assuming each other.

Stated as data rather than as classes to introspect, so the other side can
assert against it without constructing anything.

**Deliberately not the `/ws/kyok` frame table.** A frame is a transport's
business, and this package has no transport; the frames belong to whichever
repo serves them (see the gateway's docs/server-mode.md) and should be checked
against *that* repo's own contract. What is here is only what a caller and
souk agree on regardless of what carries it.
"""

from __future__ import annotations

# What a caller puts in a run's `metadata` to offer KYOK for that run, and
# nothing else. souk reads exactly this (`protocols.agui.build_forwarded_props`)
# and mints a token only when it is present, so a caller that never sets it is
# simply not offering KYOK — the feature is opt-in on both sides independently.
#
# Fixed at `repo.create_run`/`reopen_run` time and not mutable afterwards,
# which is what stops a third party attaching their own session to a run they
# did not start.
RUN_METADATA_KEY = "kyok"
RUN_METADATA_FIELDS = frozenset({"sessionId"})

# Every field of one queued completion as this package receives it. An adapter
# fills these from whatever its own side calls them — souk's `poll` answers
# with `{"requestId": ..., "body": ...}`, which is not these names, and
# `InProcessLink` is the one module that knows both.
PENDING_COMPLETION_FIELDS = frozenset({"request_id", "body"})

# `CallerLink`, and with what. Two methods is the whole port: claim one
# completion, stream one answer.
#
# **This is not a mirror of souk's API and must not become one.** The test for
# admitting a third is whether a caller's *bridge* needs it to serve a
# completion and has no other way to get it. Starting runs, reading threads and
# browsing the roster are the caller's other relationship with souk, over its
# public surface, and belong there.
LINK_METHODS = {
    "claim": ("session_id", "wait_seconds"),
    "answer": ("request_id", "chunks"),
}

# The caller's own half, stated for symmetry: a `CompletionSource` is any
# callable taking one OpenAI-shaped request body and returning an async
# iterator of OpenAI-shaped streaming chunks.
#
# A callable, not a class and not a library. Which model, which vendor, which
# key, what it costs and whether to refuse are the caller's alone — that is the
# entire point of Keep Your Own Key, and a package that picked an LLM client
# for you would be making the one decision it exists to leave you.
COMPLETION_SOURCE_ARGS = ("body",)

# What a bridge sends when its own completion source fails, so the waiting
# provider fails fast instead of sitting out the relay's timeout. souk reads
# this key and nothing else off a chunk (`KyokAdapter.respond`).
ERROR_CHUNK_KEY = "error"
