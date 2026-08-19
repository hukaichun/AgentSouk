# Bibliography notes for the responsibility-chains paper

> Working notes, not a document of record. Each entry carries the claim it
> is meant to support, organized by the paper's planned sections. Two
> honesty notes: (1) classical entries' bibliographic details (venues,
> years, page numbers) are from memory and MUST be verified against the
> actual publications before any citation ships; (2) entries marked
> **read-before-writing** shape our own claims and need a full read, not
> an abstract skim. arXiv links verified live 2026-08-19.
>
> Paper storyline and outline: see the project memory / conversation of
> 2026-08-19; thesis: a minimal neutral intermediary (records, retains,
> verifies its own verbs, never adjudicates) under which open authority/
> timing/intervention questions become derivable.

## §2.1 Agent interoperability protocols and surveys

| Entry | Supports |
|---|---|
| Survey of agent interoperability protocols: MCP/ACP/A2A/ANP — [arXiv:2505.02279](https://arxiv.org/abs/2505.02279) | The authoritative landscape survey; background framing |
| Comparative study of MCP and A2A — [arXiv:2607.23884](https://arxiv.org/html/2607.23884v1) | Empirical interop state of the art |
| MCP × A2A framework study — [arXiv:2506.01804](https://arxiv.org/abs/2506.01804) | Same |
| Security analysis of agentic AI communication protocols — [arXiv:2511.03841](https://arxiv.org/pdf/2511.03841) | Documented protocol gaps, security angle |
| A2A spec (v1.0/v1.1 dev), AG-UI spec, A2UI — official docs | Primary sources |
| a2aproject/A2A Epic #1992; google/adk-python #3276; ag-ui-protocol/ag-ui #2148 (URLs + access dates) | The motivating evidence: gap lists are time-axis only; the authority axis is absent |

## §2.2 Classical multi-agent systems

The likely reviewer community; this section is the handshake with it.

| Entry | Supports |
|---|---|
| Smith, *The Contract Net Protocol* (IEEE Trans. Computers, 1980) | Ancestor of delegation/task allocation; souk's offer/decline/refuse broker is its descendant |
| Finin et al., *KQML* (CIKM 1994); FIPA-ACL specifications | Two generations of ACLs; the "states defined, authority not" pattern begins here |
| Singh, commitment protocols (1998–); Yolum & Singh (AAMAS 2002) | Break/extend declarations read as commitments; Strabo's Langshaw is this school |
| Esteva et al., electronic institutions / ISLANDER (2001–); AMELI | **Academic ancestor of mechanism/policy separation** — institution provides the rule space, participants stay autonomous |
| Hewitt, actor model (1973) | Opaque message-passing roots |
| *Agentifying Agentic AI* — [arXiv:2511.17332](https://arxiv.org/html/2511.17332v2) (WMAC @ AAAI 2026) | The bridge manifesto: MAS community reconnecting classical concepts to LLM agents; citing it addresses our target readers directly |

## §2.3 Delegation, authorization, identity

| Entry | Supports |
|---|---|
| Lampson, Abadi, Burrows, Wobber, *Authentication in Distributed Systems: Theory and Practice* (ACM TOCS, 1992); Abadi et al., a calculus for access control (1993) | **The speaks-for relation — theoretical ancestor of actor chains**; must-cite |
| Birgisson et al., *Macaroons* (NDSS 2014); Biscuit; UCAN; SPIFFE/SPIRE; W3C DIDs | The attenuating-capability-token lineage (AIP's foundations; cite along its bibliography) |
| Dennis & Van Horn (1966); Miller et al., *Capability Myths Demolished* (2003) | Capability-systems tradition |
| Blaze, Feigenbaum, Lacy, *PolicyMaker* (IEEE S&P 1996); KeyNote | Decentralized trust management precursors |
| **AIP** — [arXiv:2603.24775](https://arxiv.org/abs/2603.24775) (+ same author's LDP, Provenance Paradox) | The near neighbor. Settled differentiation: *AIP governs what flows down the chain (capability, spending limits); responsibility chains govern what flows back up (escalation, funding attribution, visibility, blame).* AIP has no break concept (chains only extend), no HITL/interrupt/visibility coverage; cost = limit/self-report vs our attribution/funding. Its stated limitation (no revocation, TTL-only) contrasts with rule zero |
| IETF drafts: AIMS, WIMSE, Agentic JWT, SCIM-for-agents | Industry standardization pulse |

## §2.4 HITL, mixed initiative, interruptibility

The theorem-3 (interjection) conversation partners; richest 2026 harvest.

| Entry | Supports |
|---|---|
| Horvitz, *Principles of Mixed-Initiative User Interfaces* (CHI 1999) | HCI ancestry of turn-taking initiative |
| Scerri, Pynadath, Tambe, adjustable autonomy (JAIR 2002) | MAS formalization of authority transfer between humans and agents — precursor concept to responsibility chains |
| Orseau & Armstrong, *Safely Interruptible Agents* (UAI 2016) | RL-theoretic interruption; interlocutor for cancel-as-request |
| **InterruptBench** — [arXiv:2604.00892](https://arxiv.org/abs/2604.00892) | **Read-before-writing.** Formalizes three interruption types — addition, revision, retraction — nearly isomorphic to our queue lane / reply lane / withdrawal; also shows LLMs handle interruptions poorly = demand evidence for protocol-level discipline. Decide: adopt their taxonomy or contrast |
| *Are Large Reasoning Models Interruptible?* — [arXiv:2510.11713](https://arxiv.org/html/2510.11713v4) | Model-level interruption is unreliable → protocol-level discipline needed |
| AgentScope 1.0 — [arXiv:2508.16279](https://arxiv.org/pdf/2508.16279) | Framework-level real-time steering (pausing the ReAct loop) — the single-framework, intra-box counterpart to our cross-box interjection |
| **Will the Agent Recuse, and Will It Stop?** — [arXiv:2606.06460](https://arxiv.org/html/2606.06460v3) | **Read-before-writing.** Measures agent compliance with mid-flight halt directives — empirical backing for "cancellation is a request, not a command"; turns our four-line outcome table from philosophy into engineering for measured reality |
| *How to Steer Your Multi-Agent System* — [arXiv:2605.23023](https://arxiv.org/pdf/2605.23023) | Human collaborative planning over MAS |
| Human-agent collaboration survey — [arXiv:2505.00753](https://arxiv.org/abs/2505.00753); TRiSM — [arXiv:2506.04133](https://arxiv.org/html/2506.04133v3) | Survey-level context |

## §2.5 Durable execution and transactional semantics

| Entry | Supports |
|---|---|
| Garcia-Molina & Salem, *Sagas* (SIGMOD 1987) | Compensation semantics ancestor — A2A's #2124 ("canceled is not compensated") is rediscovering it |
| van der Aalst et al., workflow patterns (~2003); BPMN user tasks | Workflow prehistory of HITL |
| Durable-execution engines (Temporal et al., industry literature); Atomix — [arXiv:2602.14849](https://arxiv.org/pdf/2602.14849); SagaLLM | "State outlives connections" as industry consensus — axiom 3's backing |
| Always-On Agents survey — [arXiv:2606.30306](https://arxiv.org/pdf/2606.30306) | Persistent state/governance survey, directly adjacent |
| Concurrency anomalies in multi-agent LLM systems — [arXiv:2606.17182](https://arxiv.org/pdf/2606.17182) | Academic counterpart of our per-thread serialization problem |

## §2.6 Agent economies, discovery, trust

| Entry | Supports |
|---|---|
| SoK: Blockchain agent-to-agent payments — [arXiv:2604.03733](https://arxiv.org/pdf/2604.03733); A2A + x402 + ledger identities — [arXiv:2507.19550](https://arxiv.org/abs/2507.19550); Five attacks on x402 — [arXiv:2605.11781](https://arxiv.org/html/2605.11781v1) | On-chain cost-attribution lane (contrast with KYOK's off-chain attribution) |
| ERC-8004 empirical study — [arXiv:2606.26028](https://arxiv.org/html/2606.26028) | Academic counterpart of the A2A trust-evidence discussions (#1631) |
| *From Logic Monopoly to Social Contract* — [arXiv:2603.25100](https://arxiv.org/pdf/2603.25100) | **Read-before-writing.** Title suggests our mechanism/policy philosophy — check for convergence or collision before we claim the framing |

## §7 Federation precedents (one sentence each, from domain knowledge)

SMTP, XMPP, Matrix, ActivityPub — outbound-connection + federated-identity precedents supporting the `agent@souk` direction.

## Pre-writing checklist

1. Verify every classical entry's exact bibliographic details.
2. Full-read the three flagged papers (InterruptBench; Will the Agent Recuse; Logic Monopoly).
3. Re-run all arXiv links; note versions cited.
4. Sweep arXiv cs.MA current listings once more the week of submission — this space moves weekly.
