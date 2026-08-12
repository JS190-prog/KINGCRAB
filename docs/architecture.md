# CrabAgent architecture v0.7.0

## Runtime boundary

The private daemon is the single writer for active missions. It owns the SQLite event log,
mission state transitions, task attempts, leases, receipts, artifacts,
checkpoints, approvals and budget observations. `crab` is the only public
command and its Textual panel is a replaceable client.

The CLI can read the database while the runtime is offline, but mission execution
is routed through the private daemon. This keeps a panel exit separate from mission life.

```text
crab Textual TUI
      | local JSON over Unix socket
      v
private durable runtime
      | SQLite events, tasks, attempts, leases, receipts
      v
  +---+-------------------------------+
  |                                   |
  v                                   v
OpenCrab MCP                      Codex App Server
(read-only context receipt)       (authenticated local session)
                                      |
                                      v
                              workspace artifacts
                                      |
                                      v
                               ORACLE verification
```

## Truth contract

Every displayed state must come from a persisted observation:

- A worker is live only after an invocation receipt exists.
- Token use is `unknown` until measured by a provider response.
- A task is complete only after its output contract is satisfied.
- A mission is complete only after ORACLE records a verification decision.
- Planned model routes are never reported as executed routes.

## Colony identity

"Same species" is implemented as protocol compatibility rather than model
identity. A joining agent must use the same colony protocol, event schema, task
contract and ontology grammar. Provider and model names are not identity proof.
This gives the initial Codex-only runtime low coordination cost without blocking
future adapters that can faithfully translate the shared contracts.

## Durable records

The schema persists sessions, messages, input queues, runtime requests,
missions, task slots, role assignments, attempts,
leases, events, artifacts, evidence references, approvals, checkpoints, budgets
and tool receipts. Events are append-only; tables are materialized operational
state.

## Conversation continuity

`session_id` is the CrabAgent conversation identity and `codex_thread_id` is its
Codex counterpart. A direct chat turn and a colony mission both reuse that same
thread. The ID is written before role execution begins, not after mission
completion. A fresh runtime performs `thread/resume`; failed resume is a visible
error and never an implicit `thread/start`.

### User-facing conversation boundary

KING, QUEEN, WORKER and SOLDIER still write their complete handoffs, prompts
and receipts to the durable mission store. The panel does not present those
coordination packets as ordinary chat messages. It shows the user's input,
friendly system state, and the Oracle's user-facing conclusion; live role
state remains visible in the activity line and colony tree. This keeps the
conversation readable without weakening replay or auditability.

Auto interaction routing keeps ordinary conversation on a single direct turn.
An execution request is first compiled into a deterministic `GoalPlan`. The
plan selects only the roles that the goal needs: a clear file change uses a
local KING gate, one WORKER turn and a local ORACLE gate; an ontology question
adds QUEEN retrieval and evidence-backed ORACLE review; external or risky work
adds SOLDIER scouting and stronger routes. The user can override this with
Chat or Colony mode, while `crab run` always means Colony.

## Role authority

- KING may define goals and task graphs but cannot silently rewrite evidence.
- QUEEN may prepare ontology context but cannot change the mission objective.
- WORKER may execute only its task contract and cannot publish a final result.
- SOLDIER may stop or quarantine waste; permanent deletion needs policy approval.
- ORACLE may accept or reject evidence-backed artifacts but cannot invent claims.

## Initial provider boundary

Only Codex routes exist. SOLTELU requests one of `gpt-5.6-sol`,
`gpt-5.6-terra`, or `gpt-5.6-luna` with an explicit effort. There are no hidden
fallback providers. If a profile alias is unsupported, the runtime records the
failure and retries on the authenticated Codex session default. The App Server
turn and usage notifications are persisted as observed receipts.

WORKER runs a bounded Codex turn when a deliverable must change. ORACLE runs a
Codex turn only when semantic, external, strategic or high-risk verification is
needed; otherwise it is a persisted local artifact/receipt gate. KING invokes
its strongest route only when architecture, production, security or other
complexity signals are present. QUEEN receives an ontology context when
ontology, OpenCrab, RAG, evidence or schema signals are present; exact
list/count/status lookups use a local evidence projection, while bounded
semantic questions use Terra medium and strategic questions use Terra high.
Bounded ontology-backed writes also keep QUEEN local: it collects the observed
MCP receipt and hands the exact action to WORKER, so a second model turn is not
spent paraphrasing the same evidence. Strategic, graph or semantic writes
still retain model Queen review.
SOLDIER starts with a deterministic
local waste-loop patrol and escalates to a model only for external scouting.
This is the token-efficiency boundary: role existence and provider invocation
are separate facts and every skipped turn is recorded as a local gate.

Codex turn lifetime is activity-aware. CrabAgent does not impose a short
absolute role deadline: streamed output, tool activity, usage notifications and
approval requests refresh the idle clock. Only a user interrupt, session close,
explicit hard deadline, or prolonged complete silence stops a turn.

## Goal-centric kinetic workflow

`GoalPlan` is persisted as `goal_plan.json` and as a `goal_plan_compiled` event
before execution. It contains the mission kind, ontology spaces, evidence gate,
required stages, estimated model turns and context budget. The plan does not
pretend to know pack contents. When the plan requires ontology context, the
runtime calls the configured OpenCrab MCP `opencrab_query` before the Queen
branch and persists the response as `mcp_context_receipt`. Exact lookup goals
then use a local Queen projection; comparison, recommendation, graph and
semantic goals spend the Queen model turn. A bounded evidence brief is also a
local projection when no interpretation signal is present.

Goal intent is split before role execution. `lookup` projects observed items,
`explain` and `research` answer directly from evidence, `plan` requires an
ordered executable path, and `execute` requires a bounded workspace change.
Words such as "summarize" or "workflow" alone do not force a future action;
words such as design, build, recommend or execute do. This prevents an answer
request from becoming an internal KING coordination instruction.

When the user has selected one or more OpenCrab packs or projects, that
selection becomes an active ontology dependency for actionable, lookup,
research and planning prompts. Automatic mode therefore enters the colony
route and performs the bounded MCP read instead of merely appending pack
labels to a direct Codex prompt. Casual conversation stays in chat, and an
explicit chat mode remains the escape hatch.

The Queen receives only a bounded projection of that persisted receipt:
evidence IDs and text, retrieval metadata, pack scope and the artifact ID.
Duplicate evidence chunks and low-relevance nearby results are removed from
the model-facing projection, while the full receipt remains persisted. The
projection preserves the relevant source sentence when it contains a complete
workflow sequence. The Queen's response is an interpretation layer, not a new
evidence source. In explain/research mode, `NEXT_ACTION` is normalized to
`STOP` before both the panel and the local Oracle receive the handoff.

Before that projection is made, the local route compiler chooses an
`evidence_first` or `graph_path` contract. It records the requested ontology
spaces, relation bias, bounded MCP call count and claim requirements. The
collector then builds a small evidence/graph packet with usable-evidence
coverage and typed node-to-node paths. A graph mission cannot pass from Queen
to Oracle on evidence alone when no actual bounded path was observed. A
UUID-only or generic `mentions` topology is recorded as `graph_gate=weak`, not
as a verified evidence-to-outcome route; the local Soldier/Oracle gate stops
it until node labels and a preferred semantic relation are observed. The
partial paths are still persisted in `oracle_blocked.json` with a concrete
repair action, so a blocked graph is inspectable rather than silently lost.

Every adaptive mission also compiles `goal_graph.json`. The graph is the
operational contract for evidence slots, decision slots, action/output and the
verification loop. When KING uses a model, its bounded `king_plan.json` is
parsed into subgoals, constraints, success checks and a next action. Those
fields are passed to QUEEN, WORKER and ORACLE, and the subgoals refine the next
OpenCrab query before it runs. The plan is advisory: the user goal, graph scope
and direct MCP receipt remain authoritative.

The next boundary is `ontology_execution_contract.json`. It is a slot ledger
compiled from the goal graph and the observed MCP receipt, not from model
prose. Required evidence slots record the exact observed evidence IDs, source
and captured-text coverage; decision slots record whether the Queen supplied
the requested path, claims, references or bounded action. The local Queen
projection and model Queen both update the same ledger. SOLDIER stops before
Oracle when a required slot is missing, and Oracle model synthesis is refused
until both coverage and decision gates pass. This makes the ontology path
operational: a pack is no longer merely context attached to a prompt; it must
fill named work slots before a result can be accepted.

`0.6.27` adds the kinetic runtime contract. `kinetic_workflow.json` is compiled
from the goal plan before execution; `kinetic_workflow_state.json` records each
operator transition after execution. The workflow is not a second model plan:
it is a deterministic state machine with explicit inputs, outputs, role,
write scope, MCP budget and gate. The runtime therefore exposes a real
ontology-to-action path rather than only a role-labelled transcript.

The collector also preserves typed MCP catalog rows as `observed_items`. A
pack/project listing is governed by `observation_gate=pass` and may have zero
content evidence. It remains deliberately `claim_gate=blocked` until source
backed chunks are returned. This prevents a catalog title from becoming a
false claim while allowing a fast, useful lookup to reach Oracle.

KING subgoal Workers carry the slot IDs they are responsible for and project
only the corresponding observed evidence. Their receipts include
`evidence_slot_ids` and `slot_coverage`, so a higher Worker count does not
silently multiply the same full context or provider calls. The worker limit is
therefore a capacity control, not a promise that one Worker exists per pack or
per ontology node.

If the Queen handoff fails only its section or evidence-citation gate, SOLDIER
may run one repair pass before a workspace write or final publication. The pass
reuses the same MCP receipt and does not increase the token allowance. If the
budget is already spent, KINGCRAB stops and records the reason instead of
silently looping.

For a model-backed strategic ontology goal, KING may discover a small bounded
set of subgoals after its turn. The runtime persists those as `king_subgoal`
Worker slots before ORACLE, subject to the configured `fixed` or `auto` worker
limit. These Workers are explicitly `serial_read_only`: they project matching
items from the observed MCP receipt, create `worker_subgoal_*.json` receipts,
and spend no additional provider turn. They cannot edit the workspace or
create new evidence. ORACLE sees each receipt and checks that every admitted
slot produced one before publishing.

```text
goal -> ontology spaces -> OpenCrab MCP context receipt
     -> local lookup/evidence brief OR Queen interpretation
     -> bounded worker action -> receipts -> Oracle verdict
```

If the MCP response has no evidence or cannot be observed, the ontology
mission stops before spending the Queen turn. A model-created JSON handoff can
be stored as a convenience artifact, but it is never the authority for an
ontology claim.

The one exception is a typed metadata lookup: when MCP returns `observed_items`
for a lookup and no evidence chunks, Queen projects the item IDs, titles and
source references directly. This is not content synthesis and does not relax
the claim gate. The Oracle may accept the list only through the separate
metadata observation contract.

The stop is itself durable: the local ORACLE writes `oracle_blocked.json` with
the observed gate, exact reason and next action, but does not set an accepted
Oracle result. This keeps the user-facing conclusion useful without weakening
the evidence contract. Explicit pack selections also use the route's bounded
`package_limit`; requested, queried and omitted counts are preserved in the
receipt.

Selected packs are context, not workers. A full catalog is never placed into a
model prompt, and the prior role output is compacted to a bounded handoff.
The prompt itself is bounded by the goal's context budget; each Codex receipt
records prompt and context character counts for later direct-vs-colony
comparison.

Selection is an active dependency, not decorative UI state. If packs or
projects are selected and the objective is knowledge-shaped investigative,
lookup, recommendation, strategy, or ontology work, the compiler adds QUEEN
and the runtime performs a bounded OpenCrab MCP read before any model
interpretation. A generic code change with selected context stays on
`KING -> WORKER -> ORACLE`; it enters `KING -> QUEEN -> SOLDIER -> WORKER ->
ORACLE` only when the request actually needs the selected knowledge. A casual
prompt remains direct chat. An explicit `chat` mode is the deliberate bypass.
This keeps the user's goal authoritative while preventing a selected pack from
being shown to the model without its evidence ever being read.

When OpenCrab is configured but the user has not selected a pack, the same
compiler routes knowledge-shaped goals such as strategy, recommendation,
comparison, research and lookup through `workspace_auto`. Plain code edits do
not trigger an unnecessary OpenCrab read. The configuration check is local
only; the MCP receipt remains the authority and can still block the mission.

The implicit-context regression at `/tmp/kingcrab-suite-20260804-implicit-v2`
uses only `내 사업 전략을 추천해줘` with one selected pack. The adaptive route
observed one `opencrab_query`, completed its structured Queen handoff and
local gates, and measured 24,870 total / 1,159 billable input tokens versus
29,695 / 4,764 for the direct baseline. Structural quality was 100 versus 63;
both results were accepted. This proves the routing contract for this case,
not universal cost superiority.

## Superiority test

KINGCRAB is not considered better because it has more roles. The executable
benchmark runs the same objective twice with the same selected context:

```text
direct: one Codex turn + one bounded OpenCrab read
adaptive: compiled KING/QUEEN/WORKER/SOLDIER/ORACLE route
```

The comparison requires both result gates to pass, observed provider usage,
matching evidence scope and an ontology ledger where the goal requires one.
Missing evidence, missing usage or a rejected result yields `inconclusive`, not
an efficiency claim. `crab benchmark-run` is plan-only unless `--execute` is
explicitly supplied.

Observed Fable5xGLM5.2 paired runs are preserved at
`/tmp/kingcrab-ab-20260804-v13/results` and
`/tmp/kingcrab-ab-20260804-v14/results`. Both direct-first and
adaptive-first completed the same objective with the same bounded package
context. With both paths on the bounded Terra medium route, direct observed
29,525 and 29,672 total tokens; KINGCRAB observed 24,514 and 29,210. The
adaptive path persisted the ontology ledger, structured Queen handoff,
Soldier judge checks and accepted Oracle result. Billable input remained
cache-sensitive, so the measured conclusion is still
`adaptive_total_token_advantage_cache_sensitive`, not a general cost
superiority claim.

Write-goal fairness is now enforced by the harness. A write benchmark creates
two clean copies of the source tree before either route starts, then records
the real added/modified/deleted files in a `workspace_change_receipt`. The
local Oracle rejects a write that only has a model response and no observed
source-tree delta. A live code A/B run at
`/tmp/kingcrab-write-results-20260804-b/run/comparison.json` completed with
both paths changing exactly one file and KINGCRAB receiving the accepted local
Oracle gate. Its total-token result was also marked cache-sensitive rather
than overstated as a provider-cost win.

The pre-contract-quality live matrix on 2026-08-04 used the same selected
Fable5xGLM5.2 pack scope for both paths. It is retained as a historical
structural comparison, not as the current acceptance policy:

| Case | KINGCRAB total tokens | Direct total tokens | KINGCRAB structural score | Direct score | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| Code write | 27,995 | 28,736 | 60 | 53 | both accepted; cache-sensitive |
| Semantic ontology | 24,663 | 25,173 | 100 | 63 | both accepted; efficiency advantage |
| Exact ontology lookup | 0 | 24,500 | 100 | 63 | both accepted; no model turn |
| Graph ontology | 24,656 | 29,438 | 100 | 63 | historical structural-only pass |

These are observed cases, not a universal benchmark claim. The exact lookup
zero is a persisted local projection, not an estimate. The historical graph
run observed bounded nodes, typed edges and paths, but the MCP response
exposed only `from_id`/`to_id`; the runtime preserved those endpoints as
`unresolved_endpoint` rather than inventing labels.

The follow-up graph run at `/tmp/kingcrab-suite-20260804-v6/run` exercised the
new contract gate. It observed 11 paths but zero resolved semantic paths, so
KINGCRAB received a structural score of 100 and a goal-contract score of 90,
with `graph_semantic_gate=weak`; the A/B result was correctly
`inconclusive`, not a superiority claim. This is a real integration gap in
the graph API response, not a model-quality failure.

Every observed snapshot now carries `goal_contract_quality` beside the older
`structural_quality`. The former checks goal-specific shape, authority,
citations, graph answerability and the final gate; it still does not judge
whether a cited source or conclusion is factually true.

The latest same-goal A/B runs are preserved under `/tmp` and use the same
Fable5xGLM5.2 package scope. The bounded evidence brief now uses a local Queen
projection and still passes the same evidence, contract and Oracle gates:

| Case | KINGCRAB total | Direct total | KINGCRAB billable input | Direct billable input | Structural |
| --- | ---: | ---: | ---: | ---: | ---: |
| Evidence brief | 0 | 29,831 | 0 | 5,067 | 100 vs 63 |
| Semantic workflow synthesis | 24,839 | 29,751 | 1,186 | 5,095 | 100 vs 63 |
| Exact bounded write | 26,338 | 28,625 | 959 | 1,192 | 60 vs 53 |
| Ontology-backed write | 30,590 | 29,729 | 1,035 | 1,257 | 100 vs 20 |

All three cases had accepted results and `adaptive_efficiency_advantage`. The
evidence brief produced four relevant source citations versus two in the direct
answer, while the semantic route produced a structured Queen handoff and local
Soldier/Oracle gates around its single model turn. These are bounded
observations, not a universal claim about every prompt. The graph run remains
separately gated because graph answerability depends on the MCP response
shape; the later production-shaped response is recorded below.

The same semantic objective was then run in reverse order at
`/tmp/kingcrab-suite-20260804-semantic-df/run`: KINGCRAB measured 24,625 versus
29,700 total tokens and 4,048 versus 5,084 billable input tokens, with the same
accepted result and 100 versus 63 structural score. This crossover is evidence
against a single cache-order explanation, but it remains a bounded observation.

The graph collector was then verified against the production-shaped MCP
response at `/tmp/kingcrab-suite-20260804-graph-v4`. The response returned a
singleton node from `opencrab_get_node_context` and put external endpoint IDs,
spaces, confidence and `evidence_refs` inside nested edge properties. The
collector now preserves both UUIDs for joining and those external fields for
provenance. The live result had `graph_gate=pass`, 12 labelled nodes, 24 typed
edges, 7 bounded paths and 7 evidence-backed paths. KINGCRAB used 24,782 total
tokens versus 29,493 for the direct path, with 1,196 versus 5,497 billable
input tokens. Both result gates passed. This is a bounded graph integration
measurement; it does not replace semantic review of the source claims.

The current graph collector closes that earlier endpoint gap without widening
the model context. It asks the bounded edge listing for 24 rows, ranks
evidence-backed preferred relations before generic `mentions` edges, and uses
the two endpoint-context calls on the preferred edges first. The live rerun at
`/tmp/kingcrab-suite-20260804-graph-v9` reached `graph_gate=pass` with six MCP
calls, 25,006 versus 29,297 total tokens, 1,271 versus 5,391 billable input
tokens and structural quality 100 versus 63. The resulting graph packet is
still capped; unresolved peripheral topology remains visible as a gap rather
than being promoted to a semantic path.

The three-case regression at `/tmp/kingcrab-suite-20260804-goal-v10` accepted
all goal contracts. Its aggregate verdict is intentionally `inconclusive`:
the semantic case is cache-sensitive, while the graph case shows an observed
efficiency advantage and the ontology-backed write shows an observed quality
advantage. This measures the current cases only and is not a universal claim.

The ontology-backed write case is preserved at
`/tmp/kingcrab-suite-20260804-ontology-write-v4`. The adaptive route changed
exactly one file, cited the observed evidence, and passed the local Oracle
contract. The direct route also changed one file, but its response omitted the
required evidence citation and was therefore not accepted by the benchmark.
This is recorded as `adaptive_quality_advantage`; it is not presented as a
universal cost advantage.

## Next vertical phase

1. Add repeated crossover suites across exact lookup, semantic ontology,
   graph and code-writing goals, with answer-quality grading and graph-path
   reference answers.
2. Add heartbeat expiry and resumable leases after an operating-system crash.
3. Add write-capable OpenCrab evidence actions behind QUEEN authorization. The
   current adapter is read-only for mission context and preserves access-scope
   provenance.
4. Add SOLDIER scouting through policy-bound Crawl4AI and insane-search.
5. Add structured multi-question and fine-grained permission forms.
6. Implement the mobile command deck in three gates: a local-LAN paired PWA,
   revocable device sessions with event cursors, then an optional hosted
   gateway. Snapshot, pack selection, evidence inspection, transcript paging
   and Now/Wait/Stop requests must reuse the same daemon; the mobile client
   must never create a second colony runtime.

## `0.7.0` control-plane phase

The AutoResearch/GStack review is recorded in
[`docs/research-autoresearch-gstack.md`](research-autoresearch-gstack.md). The
implementation adds two clients around the same runtime:

- `orchestration.plan/run/status/stop` persists a bounded fan-out plan in
  `.crabagent/orchestrations/` and dispatches each child through the existing
  `_dispatch` path. A project root is a scheduling scope, so two child sessions
  cannot write the same folder at once.
- `crab mobile start` serves an authenticated local HTTP panel/API over the
  existing `DaemonClient`. It does not create a second Codex bridge, Queen or
  Worker. Refreshes are read-only and internal role handoffs are projected out
  of the mobile transcript unless they are explicitly user-facing.

The coordinator is intentionally not a cloud scheduler yet. After an OS crash,
an in-flight orchestration becomes `interrupted`; the user must run it again.
This is safer than silently replaying a provider call or a workspace edit.
