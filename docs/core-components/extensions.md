# Mechanism to code

Part of [core components](../core-components.md). The six
[mechanisms](../mechanisms.md), by where each lives in the tree.

| mechanism | core-side implementation | provider-side twin |
|---|---|---|
| [Identity is an Ed25519 keypair](../mechanisms/identity.md) | `identity.py` (verification, challenges, `SoukIdentity`); roster rules in `core.py`'s `_Roster` | `souk_provider_sdk.identity` (keypair, signers, payload builders) |
| [Actor chain](../mechanisms/actor-chain.md) | `identity.py` (`verify_actor_chain`, chain builders) | `souk_provider_sdk` (`sign_hop`, `verify_chain`) |
| [Runs and cancels are requests](../mechanisms/requests.md) | `broker.py` (three-valued offer, cancel relay, observed outcomes) | `SoukLink.offer` / `Refusal` in `souk_provider_sdk` |
| [Provider quality counters](../mechanisms/quality.md) | `live_roster.py` counters; snapshots via `RunBroker.quality` / `KyokRelay.quality` | — (observed, not reported by providers) |
| [Keep your own key](../mechanisms/kyok.md) | `kyok.py` (bindings, tokens, relay) + `protocols/kyok.py` (call authorization, relay envelope) | `souk_llm_provider_sdk` (link, handler, `CompletionRefused`) |
| [Responsibility chains](../mechanisms/responsibility-chains.md) | **not implemented** — design record only | — |

The byte-level agreement between the two columns is
[`contract-vectors.json`](../contract-vectors.json); neither column
imports the other.
