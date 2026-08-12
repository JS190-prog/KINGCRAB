# Goal-centric harness

KINGCRAB must earn its extra structure. The comparison target is a direct
Codex/Claude turn with OpenCrab available, not a fictional zero-cost baseline.

## What is measured

Every mission records:

- `goal_plan.json`: goal kind, required stages, ontology spaces and estimated
  model turns.
- role assignments: whether each role is `local` or a real Codex route.
- `mcp_context_receipt`: the observed read-only OpenCrab MCP response, including
  bounded evidence rows, retrieval metadata and pack scope.
- `retrieval_contract`: a deterministic `evidence_first` or `graph_path` route
  with ontology spaces, relation bias, MCP call limits and claim requirements.
- context quality: usable evidence coverage, typed edge count, bounded path
  count and graph coverage.
- tool receipts and artifacts: what actually ran and what was produced.
- `budgets.tokens_observed`: provider-reported usage, or `unknown` when the
  provider did not return usage.
- `cachedInputTokens` and the derived `billable_input_tokens_observed`, when
  Codex reports them. This separates total-token reduction from provider
  prompt-cache effects.
- Oracle result: whether the final artifact passed the evidence gate.

## Baseline and adaptive paths

| Goal | Direct turn baseline | KINGCRAB adaptive path | Intended difference |
| --- | ---: | --- | --- |
| Exact small file change | one model turn | local KING -> WORKER -> local ORACLE | same model-turn class, with durable receipts and verification |
| OpenCrab evidence question | one model turn plus optional MCP query | KING -> QUEEN -> local SOLDIER -> local ORACLE | bounded briefs use zero-model local Queen projection; workflow/comparison synthesis uses one grounded Queen turn |
| Graph or high-risk ontology work | one model turn plus optional MCP query | KING -> QUEEN -> SOLDIER -> ORACLE | extra model turn is justified only by graph/high-risk verification |

The adaptive path is not declared superior from estimates alone. A run is a
win only when its persisted evidence and Oracle decision improve the result per
observed token. Missing usage is reported as unknown rather than converted to
zero.

Exact list/count/status questions and bounded evidence briefs are a special
zero-model path: KING, QUEEN,
SOLDIER and ORACLE still leave durable receipts, but the local Queen projects
the observed MCP evidence instead of spending a synthesis turn. Semantic
questions retain the model Queen. Comparison, recommendation and strategy are
explicit synthesis signals; graph questions always retain the model Queen.
Bounded ontology-backed writes close Oracle
locally after worker artifact and evidence checks; strategic, graph, external
or high-risk work is the only path that spends an Oracle model turn.

The practical difference from a direct model turn is the kinetic handoff:

```text
goal -> retrieval_contract -> MCP evidence/graph receipt
     -> local quality gate -> Queen path interpretation
     -> one bounded next action -> Oracle decision
```

The Queen does not receive the full Workspace catalog. The collector first
applies the route contract, deduplicates nodes and edges, constructs only
bounded node-to-node paths, and records a graph gate. If a graph endpoint is
unavailable, it tries only the bounded list endpoint; it never silently treats
an empty result as a graph. The model prompt has a real character budget and
the turn receipt records `prompt_chars` and `context_chars` so token efficiency
can be measured rather than assumed.

Pack selection is also a routing signal. With selected packs or projects, a
knowledge-shaped lookup, research, recommendation, strategy, or ontology
prompt must pass through the ontology route and produce an observed MCP
receipt. A generic code edit stays on the direct code route even when a pack
is selected. Only casual prompts, or an explicit chat-mode override, stay
outside the colony. This makes the selection UI operational rather than
decorative without taxing unrelated coding work.

If OpenCrab is configured and the selection is empty, knowledge-shaped goals
still use a bounded `workspace_auto` route. Plain code edits stay on the code
route, so automatic ontology use does not become an unconditional catalog
scan. Configuration is only a local routing hint; the observed MCP receipt is
still required before any claim is accepted.

The implicit-context regression uses the recommendation prompt
`내 사업 전략을 추천해줘`, with no explicit OpenCrab or ontology wording. With
one selected pack, the adaptive path observed `opencrab_query`, completed the
Queen handoff and local gates, and measured 24,870 total / 1,159 billable
input tokens versus 29,695 / 4,764 for the direct baseline. Structural quality
was 100 versus 63 and both result gates passed. The saved snapshot is
`/tmp/kingcrab-suite-20260804-implicit-v2`.

The mission budget gate uses the provider-reported uncached input portion
(`inputTokens - cachedInputTokens`) when available. Codex's persistent-thread
`totalTokens` remains visible for diagnosis, but is not a per-role allowance:
otherwise inherited cached context can block the next required role before it
has a chance to work. A provider without input dimensions falls back to total
tokens and records that basis in the durable budget event. For ontology-backed
workspace changes, QUEEN is read-only; it must return the selected path and a
bounded WORKER action, while WORKER is the only write authority.

Successful evidence contexts are cached locally for a short TTL and reused by
the same route/package scope. The cache keeps a bounded set of recent goal and
pack-scope entries, so moving between two active projects does not evict the
first context immediately. Cache hits are marked separately from live MCP
observations. Empty, failed, or graph-incomplete responses are never cached.

Provider fallback is a separate gate from retry. A rejected model/effort route
may fall back once to the authenticated Codex session default; timeout, cancel,
connection loss and server-close results are recorded as failures without
replaying the turn. This keeps the observed token count honest and prevents a
second Worker turn from repeating a partial file edit.

## Harness loop

1. Compile the same objective into a `GoalPlan`.
2. Run the direct baseline in an isolated branch or session. For a write goal,
   the harness creates two sibling copies before either route starts.
3. Run the adaptive KINGCRAB mission from the matching copy.
4. Compare artifact correctness, evidence reachability, changed-file scope,
   retries, total tokens, cached input, billable input and wall time.
5. Keep the adaptive path only when it improves quality per token or provides a
   required audit/permission boundary at an acceptable cost.

`benchmark-run --order direct-first|adaptive-first` lets the same pair run in
both orders. If total tokens fall but billable input rises because the direct
turn received a larger prompt-cache hit, the result is reported as
`adaptive_total_token_advantage_cache_sensitive`, not as a cost advantage.

The planned comparison is available without spending provider tokens:

```bash
crab benchmark "오픈크랩 팩을 조회해서 브랜드 전략에 도움이 될 근거를 찾아줘"
```

This command compares the honest direct baseline (one model turn) with the
goal-compiled route. It does not call Codex, OpenCrab, or claim that KINGCRAB is
better. The fixed five-role plan is retained only as a reference, not as the
baseline.

After two identical-goal runs have been persisted as JSON snapshots:

```bash
crab inspect --latest --json > adaptive.json
crab benchmark-observed adaptive.json direct.json
```

The observed comparator reports Oracle acceptance, evidence refs, model turns,
MCP receipts, ontology ledgers and provider-reported tokens. For an ontology
comparison, a direct snapshot without its own MCP/evidence receipt is marked
unknown rather than treated as a weaker answer. Unknown usage or missing
grounding prevents an efficiency/quality verdict.

For write goals, a model response is not sufficient evidence of success. The
Worker records a SHA-256-backed source-tree delta while excluding `.crabagent`,
VCS internals and common dependency/build directories. The local Oracle checks
that receipt before accepting the mission. This makes the direct baseline and
KINGCRAB path comparable even when the two runs happen in reverse order.

## Ontology authority boundary

For an ontology mission, the runtime calls the configured OpenCrab MCP
`opencrab_query` once before the Queen branch. The response is persisted as
`opencrab_context.json` with kind `mcp_context_receipt`; its evidence IDs,
bounded text, retrieval metadata and pack scope become the factual input to the
Queen. Exact lookup goals project that receipt locally; semantic Queen output
may interpret, connect and qualify it, but neither output is promoted to
evidence.

If the MCP response is `no_evidence`, or the read fails, the mission stops
before spending a Queen turn. This is deliberately stricter than accepting a
model-created JSON handoff, because a generated handoff cannot prove that the
underlying pack or chunk was actually retrieved.

When a graph response contains only unresolved endpoints, the mission also
stops before the Queen turn but preserves the observed partial paths, graph
quality and a concrete endpoint-label repair action in `oracle_blocked.json`.

For a graph goal, evidence alone is insufficient: at least one bounded typed
node-to-node path must also pass the graph gate. This prevents a direct model
from producing a plausible relationship narrative from disconnected terms.
The collector requests a bounded 24-edge slice, prioritizes evidence-backed
relations named by the route, and spends endpoint lookups on those relations
before generic topology. This keeps graph work kinetic and semantically
useful without sending the full graph to the Queen.

The deterministic harness proves the routing and MCP-receipt contract without
spending provider tokens. Live Fable5xGLM5.2 A/B runs are preserved outside the
repository under `/tmp/kingcrab-ab-20260804-v9/results` and
`/tmp/kingcrab-ab-20260804-v10/results`. Both completed with accepted results
and the same bounded OpenCrab package context. The measured total-token
advantage is real in these two observations, but billable-input accounting is
cache-sensitive, so a broad cost claim remains unproven.
