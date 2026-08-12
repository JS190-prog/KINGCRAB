# Mobile command deck

## Local implementation in `0.7.0`

The first usable bridge is now included. It is intentionally local-first and
uses the same `crabd` Unix socket as the desktop TUI:

```bash
cd /path/to/KINGCRAB
crab mobile pair --workspace /path/to/project
crab mobile start --workspace /path/to/project
```

Open the printed URL on a phone on the same trusted network and enter the
printed pairing token. The default bind is `127.0.0.1`; use
`--host 0.0.0.0` only when the computer and phone are on a trusted LAN. The
gateway serves a small mobile command deck and these authenticated endpoints:

| Endpoint | Purpose |
| --- | --- |
| `GET /v1/snapshot` | User-facing transcript, current mission and runtime state |
| `POST /v1/prompt` | Submit `start`, `wait` or `now` to the existing session |
| `POST /v1/interrupt` | Interrupt the existing session |
| `POST /v1/approval` | Resolve an observed runtime request |
| `GET /v1/orchestrations` | Read persisted multi-session runs |
| `POST /v1/orchestrations/plan` | Create a bounded fan-out plan |
| `POST /v1/orchestrations/run` | Start a planned run |
| `POST /v1/orchestrations/stop` | Stop the coordinator and active children |

`/health` is a non-mutating runtime probe. All `/v1/*` requests require the
bearer token. The token is stored at `.crabagent/mobile-pair.json` with
owner-only permissions on supported filesystems. The first bridge does not
claim TLS or internet safety; do not port-forward it.

Mobile KINGCRAB should be a client of the same durable runtime, not a second
agent implementation and not a phone-sized copy of the terminal.

## Product boundary

```text
Mobile panel
    | authenticated event stream
    v
CrabAgent gateway
    | one active mission writer
    v
Local or hosted crabd
    +-- Codex session
    +-- OpenCrab MCP context
    +-- SQLite receipts and artifacts
```

The mobile client never receives Codex or OpenCrab credentials. It receives
the minimum observed state needed to guide a mission: current goal, role
status, activity, token observation, selected ontology scope, evidence refs,
approval requests and Oracle verdict.

The phone is a command deck, not a second reasoning runtime. Reading a
snapshot, changing a project selection, opening an evidence receipt, or
refreshing the activity stream must not call an LLM. Only a submitted goal or
an explicitly approved runtime action may create a provider turn.

The mobile advantage is fast operational control, not a smaller chat window:
the user can see the compiled goal, selected ontology scope, evidence quality,
role activity and token observation before spending another turn. A goal
preview should show `explain`, `plan`, `research` or `execute`, the expected
roles, the evidence gate and the estimated model turns. The user confirms a
plan or execute route; explain/research can return directly from the observed
context when the local cache is fresh.

## Why this is better on mobile

Mobile should make the ontology-first difference visible in a few seconds.
The first card is a goal preview, not an empty chat box: it shows the compiled
route, selected project and pack scope, expected roles, evidence gate, graph
answerability and estimated model turns. A user can remove a noisy pack, keep a
single relevant path, and run again without asking a model to rediscover the
workspace. This is the mobile version of token efficiency.

The second card is the live decision surface. It shows one of `WAITING FOR
INPUT`, `RUNNING`, `NEEDS APPROVAL`, `BLOCKED BY EVIDENCE` or `ORACLE READY`,
along with the latest event time and event cursor. The user can stop a mission
from this card and later reconnect to the same durable mission. A blocked graph
must expose the missing endpoint or evidence gate; it must not look like a
successful answer simply because a model produced prose.

The third card is the evidence drawer. It contains the selected pack and
project labels, source refs, bounded path rows and the Oracle receipt. It is
read-only by default. A write or MCP action opens an approval sheet with the
exact request ID, workspace scope, proposed effect and the `Approve` or
`Decline` decision that will be persisted.

The mobile onboarding follows the low-friction desktop sequence: pair the
already configured Codex/CrabAgent runtime first, then connect the OpenCrab
workspace through a revocable device session. The phone does not ask the user
to paste provider or MCP secrets. If the desktop runtime is not paired, the
mobile app can show the setup checklist but cannot pretend that a model,
quota, MCP server or ontology context is available.

## Mobile-first workflows

The first screen should answer three questions immediately:

- What goal is active?
- Which role is waiting, working or blocked?
- What decision does the user need to make now?

Primary actions:

- Continue or stop a mission.
- Add an instruction with `Now` or `Wait`.
- Approve or decline an MCP/tool request.
- Inspect the Queen's selected packs and evidence paths.
- Open the Oracle conclusion and its receipts.
- Switch project and start a new conversation.

Recommended mobile information hierarchy:

```text
NOW     current decision, blocked request or Oracle result
PATH    KING goal -> QUEEN evidence -> WORKER/SOLDIER activity -> ORACLE gate
EVIDENCE selected packs, source refs, graph quality and gaps
HISTORY paginated transcript and prior mission receipts
```

This keeps the high-value decision visible on a small screen while making the
ontology path inspectable. Pack/project selection is a local state change and
should update the preview without calling Codex; only pressing `Run now` may
create a provider turn.

The full transcript is secondary on mobile. It is virtualized and paginated;
the operational timeline and evidence path remain visible without loading the
entire conversation.

## Connection sequence

1. The desktop or local CLI starts `crabd` and creates a local bearer token.
2. The phone opens the printed local URL and enters the token once.
3. The gateway reads and writes only through the existing `crabd` client.
4. The phone receives a read-only snapshot first.
5. Prompt, approval and interrupt actions are sent to the same durable session.

No public Unix socket is exposed. A future hosted gateway must use TLS,
per-device revocation, short-lived access tokens, replay protection and an
explicit workspace scope. The first implementation should be a local-network
pairing mode before any public relay is shipped.

## Runtime contract

The mobile stream should carry the same events already persisted locally:

- `goal_plan_compiled`
- `opencrab_context_observed`
- `codex_activity`
- `token_usage_observed`
- `runtime_request`
- `local_gate_recorded`
- `soldier_stop_gate`
- `oracle_local_verdict`
- `mission completed/failed/cancelled`

The server remains authoritative. Reconnecting the phone requests a snapshot
plus events after the last event ID; it does not replay or restart a mission.

The first snapshot should be small and versioned:

```json
{
  "session_id": "...",
  "goal_plan": {"kind": "...", "stages": [], "ontology_mode": "..."},
  "mission": {"status": "...", "objective": "..."},
  "roles": {"KING": {}, "QUEEN": {}, "WORKER": {}, "SOLDIER": {}, "ORACLE": {}},
  "ontology": {"selected_projects": [], "selected_packs": [], "evidence_count": 0},
  "quality": {"structural": {}, "goal_contract": {}},
  "budget": {"tokens_observed": 0, "observation_source": "..."},
  "last_event_id": 0
}
```

`tokens_observed: 0` is valid only when a persisted local route proves that
no model turn occurred. Unknown provider usage stays `unknown`; the mobile
panel must never replace it with an estimate. A reconnect applies only events
after `last_event_id`, while a stale command is rejected with the current
session revision so a second phone cannot duplicate work.

The quality projection is intentionally split. `structural` answers whether
the colony actually produced the persisted receipts and gates. `goal_contract`
answers whether this particular goal's required output shape, evidence,
graph-answerability and Oracle decision were satisfied. A weak graph or
missing semantic endpoint is shown as `partial` or `blocked`; it is never
silently displayed as a successful conclusion.

Mobile command semantics are explicit:

- `now`: enqueue a safe-boundary instruction and request an interrupt when the
  current runtime allows it.
- `wait`: persist the instruction for the next completed mission boundary.
- `stop`: create a durable cancellation request; it is not a client-side hide.
- `approve` and `decline`: bind the decision to the observed request ID and
  workspace scope.

## Phased build

### Phase A: responsive local panel

The local HTTP bridge, live snapshot, Now/Wait/Stop controls and a minimal
responsive browser panel are implemented. The next increment is event-cursor
reconnect and paginated evidence/mission views. Native wrappers are deferred
until push notifications or device authentication is a demonstrated need.

### Phase B: secure pairing

Add device registration, revocation, encrypted transport, workspace scope and
event cursors. Add push notifications only for blocked approvals, Oracle
completion, failure and idle timeout.

### Phase C: remote runtime

Allow `crabd` to run on a workstation or server with an explicit user-owned
gateway. Add artifact download, branch creation and mobile project selection.

The mobile client should not run a second Queen, Worker or Oracle. That would
split receipts and make the result less trustworthy. One runtime, many panels
is the correct topology.
