"""Protocol translation: AG-UI and A2A, as pure adapters.

Core, not an optional extra, and for the same reason `pydantic-ai` ships its
AG-UI adapter in the library rather than as a plugin: the mapping *is* the
semantic model, and it has to be usable in-process. Wanting A2A task
semantics against a local agent should not require standing up an HTTP
server.

Nothing here touches a framework. No `Request`, no `Response`, no
`EventSourceResponse` appears in any signature — an adapter takes and returns
plain data, raises souk.errors, and leaves every decision about sockets,
status codes and response framing to whoever serves it. That absence is the
line that keeps protocol translation out of the serving layer, and it is
checked by tests/test_core_is_network_free.py.

What lives here is the hard-won part: the mapping decisions that would
otherwise be re-derived, differently and often wrongly, by every integrator —
A2A's Task.id being souk's run_id and staying stable across pause/resume
rounds, contextId being thread_id, an unrecognized AG-UI threadId minting a
real thread instead of 404ing. See souk-no-forced-protocol-deviation: these
exist so a standard AG-UI or A2A client never has to deviate from its own
spec to talk to souk.
"""
