# KINGCRAB current-state handoff

Updated for `0.7.0`. This document is the restart point for the next
development session.

## What is working now

- `crab` is a client of a durable local runtime. Closing the panel does not
  erase a mission.
- A goal is compiled before provider work. The plan records the mission kind,
  roles, ontology mode, evidence gate, estimated model turns and token target.
- OpenCrab context is accepted only from an observed MCP response. The local
  receipt, not a model-written handoff, is the authority for evidence.
- Queen retrieval is bounded and cached. Exact lookup and evidence briefs can
  finish through local gates with zero model turns; semantic, recommendation,
  comparison and graph work can escalate to Codex.
- KING, QUEEN, WORKER, SOLDIER and ORACLE are real persisted task stages. A
  role is not shown as active unless an invocation or local gate receipt exists.
- A completed live mission publishes one `goal_outcome.json` artifact with the
  verdict, evidence counts, graph/claim gates, observed usage and next action.
- A failed or cancelled mission can be retried with lineage. Retry is blocked
  after an observed workspace change unless the user explicitly uses
  `--force`.
- Korean request-word normalization now removes terms such as `팩을`,
  `조회해서` and `찾아줘` before the bounded ontology query is built.
- Every adaptive mission now persists a deterministic `goal_graph.json`. It
  turns the objective into evidence slots, decision slots, an action/output
  contract and an explicit `retrieve -> draft/execute -> judge -> publish or
  stop` loop. Providers receive only its bounded operational projection.
- KING now emits a bounded `king_plan.json` packet. Its subgoals, constraints,
  success checks and next action are passed to later roles, while the user goal,
  goal graph and direct MCP receipt remain authoritative.
- If a model Queen handoff fails only its structure/citation gate, SOLDIER may
  request exactly one pre-write repair using the same evidence IDs. The repair
  does not enlarge the mission token budget; a spent budget becomes a durable
  stop instead of another hidden call.
- Strategic model-backed ontology goals can now materialize up to the
  configured Worker limit as `king_subgoal` slots after SOLDIER. They are
  persisted before ORACLE, run as serial read-only evidence projections, and
  add no provider turn or workspace write. The `subgoal_plan.json` and
  `worker_subgoal_*.json` receipts make the fan-out observable.
- Every ontology mission now persists `ontology_execution_contract.json`.
  Required evidence slots are filled only from the observed MCP receipt, and
  decision slots are filled only from the Queen's structured output. A missing
  slot blocks Soldier/Oracle instead of allowing a plausible conclusion. Each
  King subgoal Worker records its slot IDs and the evidence IDs it actually
  projected.
- QUEEN then promotes that contract into an identity-bound `ontology_ledger`
  revision. The store persists and hash-validates the ledger on readback;
  SOLDIER and ORACLE require the matching `mission_id`, `goal_graph_id` and
  revision before continuing. A bounded Queen-handoff repair creates and
  persists a new ledger revision before ORACLE.
- Every mission now persists `kinetic_workflow.json` before execution and
  `kinetic_workflow_state.json` after execution. The latter records actual
  `goal_bind`, `scope_lock`, retrieval, slot binding, decision, patrol,
  execution and verification transitions; planned roles are not mistaken for
  observed work.
- Typed OpenCrab `items`/`packs`/`projects` rows are preserved as
  `observed_items`. Metadata-only lookup can complete without a model turn and
  through `observation_gate=pass`, while `claim_gate` remains blocked until
  source-backed content evidence exists.
- AutoResearch/GStack-inspired control-plane work is executable. A bounded
  orchestration can fan out to existing project sessions, serialize children
  sharing a folder, persist child state and aggregate an Oracle-level result
  without treating planned roles as active work.
- `crab mobile pair` and `crab mobile start` provide the first local-LAN
  authenticated mobile panel/API over the same `crabd` runtime. Mobile
  refresh and snapshot projection are read-only; prompt, approval and
  interrupt calls reuse the existing session.

## Quick manual test

From the project directory:

```bash
cd /path/to/KINGCRAB
python3 -m pytest -q
python3 -m compileall -q src tests
python3 -m crabagent.cli --version
python3 -m crabagent.cli init --workspace /tmp/kingcrab-manual
python3 -m crabagent.cli run --demo --workspace /tmp/kingcrab-manual \
  "deterministic ontology colony check"
python3 -m crabagent.cli inspect --workspace /tmp/kingcrab-manual --latest --json
python3 -m crabagent.cli replay --workspace /tmp/kingcrab-manual --latest
crab

# Multi-session coordination
crab orchestrate plan --child "SESSION_A::Inspect the API" --child "SESSION_B::Review the UI"
crab orchestrate run ORCH_ID
crab orchestrate status ORCH_ID

# Same-runtime mobile deck
crab mobile pair
crab mobile start
```

The deterministic demo uses no provider and proves the durable kernel. For a
real configured session, open `crab`, submit a small goal, then inspect the
Oracle conclusion. Useful panel commands are `/goal`, `/ontology`, `/mcp`,
`/cli`, `/retry`, `/stop`, `/new`, `/project` and `/panels`.

For a failed or cancelled mission with no workspace change:

```bash
python3 -m crabagent.cli retry --workspace /tmp/kingcrab-manual
```

Use `--force` only when the workspace change is understood and intentionally
belongs to the retry. The runtime will otherwise refuse to risk a duplicate
edit.

## Where to inspect the truth

Each live mission stores receipts under:

```text
<workspace>/.crabagent/artifacts/<mission-id>/
```

The most useful records are `goal_plan.json`, `goal_graph.json`,
`king_plan.json`, `subgoal_plan.json`, `worker_subgoal_*.json`,
`opencrab_context.json`, `ontology_execution_contract.json`, `ontology_ledger.json`,
`kinetic_workflow.json`, `kinetic_workflow_state.json`, `soldier_report.json`,
`oracle_result.md` and `goal_outcome.json`. The SQLite
event log remains at
`<workspace>/.crabagent/state.sqlite3`.

## Current boundaries

- Only the Codex App Server provider is wired. Claude, Kimi, GLM and Grok
  adapters are not implemented.
- The colony protocol's new orchestration layer can run distinct sessions in
  bounded parallelism, but role execution inside one mission remains bounded
  and mostly serial. Children sharing a folder are serialized; crash-resume
  leases and direct child mission links are not finished.
- A provider timeout or cancellation is not silently replayed. `/retry` is a
  deliberate user action and preserves the source mission ID.
- Token efficiency is measured, not guaranteed. Provider prompt-cache order can
  change billable input, so the existing A/B results are bounded observations.
- The Codex desktop setting `Luna Max` is not automatically inherited by this
  local runtime. KINGCRAB currently applies its own SOLTELU role policy and
  records the requested route; the next session should add an explicit provider
  policy surface if Max must be user-selectable.
- Mobile is implemented as a loopback/LAN HTTP gateway in `mobile_gateway.py`.
  It is not production internet-safe: no TLS, device revocation or hosted
  relay is included yet.
- Multi-orchestration persists fan-out state as JSON and uses session/project
  inspection for child completion. A crash-resumable lease table and direct
  orchestration-to-mission foreign key are still pending.
- Automatic revision is limited to one Queen handoff repair before a workspace
  write or final publication. It is opportunistic and budget-bound; it is not
  a general-purpose self-training loop. Failed or over-budget gates remain a
  durable stop/retry boundary.

## Next development slice

1. Let the user run the manual checks above and capture one real small mission.
2. Compare its kinetic trace, `goal_outcome.json`, ontology contract and
   subgoal receipts against the direct Codex result for the same objective.
3. Add answer-quality grading to the direct-vs-KINGCRAB comparison; token
   counts alone cannot prove a semantic advantage.
4. Add explicit provider-policy visibility and a safe effort override only
   after the current result contract is accepted.
5. Add crash-resume/true queued worker scheduling, direct child mission links,
   mobile event cursors and device revocation.

The ontology-ledger runtime rollout was completed on 2026-08-18 through the
canonical remote release path. Runtime release
`0.7.8-20260818-ontology-ledger-1` is active on `myserver-root` with source
commit `28392192c37cbb787e7702484ae4538cdb371e42` and wheel SHA-256
`d37993d95cc575517a44005bd31b3f768daacb163d94cae2243b252a18a97162`.
The deployment `ping`/`mission.list` canaries, gateway `/readyz`, public OAuth
metadata, unauthenticated 401 boundary and authenticated public OAuth
`tools/list` canary passed. The authenticated verifier observed 13 tools,
`query_status=ok`, the read-scope action boundary, and the execution contract,
update and receipt schemas. No OpenCrab ingest was performed.
