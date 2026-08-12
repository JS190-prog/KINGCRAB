# AutoResearch + GStack design transfer

Updated for KINGCRAB `0.7.0`.

This is an engineering synthesis, not a claim that KINGCRAB is a copy of
either project. The source facts below were checked against the primary
repositories:

- [karpathy/autoresearch](https://github.com/karpathy/autoresearch)
- [autoresearch/program.md](https://github.com/karpathy/autoresearch/blob/master/program.md)
- [garrytan/gstack](https://github.com/garrytan/gstack)
- [gstack review skill](https://github.com/garrytan/gstack/blob/main/review/SKILL.md)
- [gstack ship skill](https://github.com/garrytan/gstack/blob/main/ship/SKILL.md)

## What the two systems teach

### AutoResearch: a bounded experimental loop

The repository deliberately makes the experiment small and comparable: one
agent edits one in-scope file, runs a fixed wall-clock experiment, reads a
metric, logs the result, and keeps or discards the change. The program also
requires a baseline and treats crashes/timeouts as explicit outcomes. Its
important idea is not the GPU or the training code. It is the control loop:

```text
baseline -> one change -> fixed budget -> measured result
         -> keep if better / discard if not -> durable log
```

KINGCRAB transfer:

- Every orchestration has a declared child budget and a bounded concurrency.
- Every child has one objective and one project/write scope.
- A child is not marked active until `crabd` really dispatches it.
- Completion is decided from the child runtime state, not from a planned role.
- Failed, cancelled and interrupted runs remain inspectable and resumable only
  after an explicit user action.
- The equivalent of `results.tsv` is the SQLite event stream plus the durable
  `.crabagent/orchestrations/orch-*.json` fan-out state.

What is intentionally not copied: an unattended infinite loop. A coding or
ontology mission can run only inside a user-started orchestration and a
bounded runtime lease. This makes the loop auditable and prevents accidental
API spend.

### GStack: a reviewable workflow surface

GStack presents specialized skills as a connected sequence. Planning creates
an artifact that later review and QA steps consume; review checks scope and
quality; shipping runs verification gates. The practical lesson is that an
agent tool needs operational verbs, not only a chat box:

```text
plan -> implementation -> review -> QA -> ship
```

KINGCRAB transfer:

- `KING` owns the goal and orchestration plan.
- `QUEEN` owns the observed ontology context and evidence slots.
- `WORKER` owns a bounded implementation or read-only subtask.
- `SOLDIER` owns waste, scope and evidence gates.
- `ORACLE` owns the accepted conclusion or the explicit block.
- The same contract is visible as TUI commands, CLI commands and mobile API
  operations.

GStack's browser/QA emphasis also gives KINGCRAB a useful mobile rule: a
mobile panel must be tested as a real client against a live runtime, not judged
from a static mock or a JSON handoff.

What is intentionally not copied: assumptions about a particular host agent,
Claude skill directory or GitHub workflow. KINGCRAB keeps the role contract in
its own runtime and can later bind equivalent skills from Codex, MCP or CLI
providers.

## KINGCRAB competitive architecture

```text
                         phone / browser
                              |
                     bearer-token mobile API
                              |
TUI / crab CLI  ----------- crabd ----------- SQLite event log
                              |
             +----------------+----------------+
             |                                 |
       one KING session                 multi-orchestration
             |                          fan-out / fan-in
             v                                 |
 KING -> QUEEN -> WORKER/SOLDIER -> ORACLE     +-- project scope lease
             |                                 +-- bounded concurrency
             +------ observed OpenCrab MCP ----+-- durable child receipts
```

The differentiator is not “more agents.” It is one control plane that makes
the following claims testable:

| Capability | Direct chat failure mode | KINGCRAB contract |
| --- | --- | --- |
| Context | Full catalog or stale labels in prompt | Bounded observed MCP receipt and ontology slots |
| Work fan-out | Hidden parallel calls and duplicate edits | Session-scoped children with project collision guard |
| Cost | Provider choice is implicit | SOLTELU route and turn count are persisted |
| Quality | A fluent answer looks complete | SOLDIER gate and Oracle verdict are separate |
| Recovery | Closing UI loses control | `crabd` owns state; TUI/mobile reconnect |
| Mobile | Second chat runtime drifts from desktop | Authenticated projection over the same daemon |
| Learning | No durable experiment memory | Events, orchestration state and outcome artifacts |

## Current vertical slice

`0.7.0` implements the first operational slice:

- `crab orchestrate plan` persists child missions without invoking a model.
- `crab orchestrate run` starts bounded child work through the existing
  `crabd` dispatcher. Distinct project folders may run concurrently; the same
  folder is held to one active child.
- `crab orchestrate status` reads durable state after a fresh process.
- `crab orchestrate stop` interrupts running child sessions and cancels pending
  children.
- `crab mobile pair` creates an owner-only local bearer token.
- `crab mobile start` serves a small mobile command deck and JSON API over the
  same daemon. The default bind is loopback; LAN binding is explicit.
- Mobile snapshots remove internal KING/QUEEN/WORKER/SOLDIER handoffs unless
  they are marked user-facing, while keeping the Oracle conclusion.

The implementation does not claim hosted-grade mobile security yet. LAN mode
is for a trusted network only. TLS, device revocation, short-lived sessions,
event cursors and a hosted relay are the next security phase.

## Validation matrix

The next benchmark should compare the same objective through four routes:

| Route | Primary question | Required proof |
| --- | --- | --- |
| Direct | Does one provider turn solve it? | provider receipt + artifact |
| Single colony | Does ontology gating improve quality/cost? | evidence slots + Oracle result |
| Multi-orchestration | Does bounded fan-out improve throughput? | child event timeline + no scope collision |
| Mobile-controlled | Can a phone guide the same run? | authenticated request + same mission ID |

Go/no-go thresholds for a production-facing release:

- zero unobserved “active” workers in the panel;
- zero cross-child writes to the same project scope;
- every accepted Oracle result linked to evidence or a verified workspace
  artifact;
- a stopped orchestration leaves no child in a false running state after
  reconnect;
- mobile control never creates a second provider session for a refresh;
- repeated runs report actual token usage or `unknown`, never an estimate.

## Open gaps

- A real crash-resumable lease table is still needed for OS-level recovery.
- Child mission IDs should be linked directly to orchestration rows instead of
  being inferred from the child session's latest mission.
- The mobile gateway needs TLS/device revocation before exposure outside a
  trusted LAN.
- A true GStack-like review artifact chain should be added for code changes:
  plan, scope review, QA receipt, and ship decision.
- A measured multi-session throughput benchmark is still pending; the current
  deterministic test proves persistence and fan-in, not provider speedup.
