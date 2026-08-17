from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any, Dict, List, Optional

from .ontology_route import compile_ontology_route


ONTOLOGY_SIGNALS = (
    "ontology",
    "opencrab",
    "knowledge graph",
    "rag",
    "evidence",
    "schema",
    "mcp",
    "온톨로지",
    "오픈크랩",
    "지식 그래프",
    "근거",
    "스키마",
    "팩",
    "팩들",
)

WRITE_SIGNALS = (
    "build",
    "create",
    "fix",
    "implement",
    "install",
    "deploy",
    "edit",
    "add",
    "remove",
    "update",
    "refactor",
    "write file",
    "만들",
    "만들어",
    "고쳐",
    "수정",
    "구현",
    "설치",
    "배포",
    "추가",
    "삭제",
    "변경",
    "작성해",
    "개발",
)

NEGATED_WRITE_PHRASES = (
    "do not build",
    "do not create",
    "do not fix",
    "do not implement",
    "do not install",
    "do not deploy",
    "do not edit",
    "do not add",
    "do not remove",
    "do not update",
    "do not refactor",
    "do not write",
    "don't build",
    "don't create",
    "don't fix",
    "don't implement",
    "don't install",
    "don't deploy",
    "don't edit",
    "don't add",
    "don't remove",
    "don't update",
    "don't refactor",
    "don't write",
    "without changing",
    "without modifying",
    "without editing",
    "without writing",
    "without creating",
    "만들지",
    "고치지",
    "수정하지",
    "구현하지",
    "설치하지",
    "배포하지",
    "추가하지",
    "삭제하지",
    "변경하지",
    "작성하지",
    "개발하지",
)

RESEARCH_SIGNALS = (
    "research",
    "search",
    "crawl",
    "compare",
    "investigate",
    "analyze",
    "find",
    "조사",
    "검색",
    "크롤",
    "비교",
    "분석",
    "찾아",
    "확인해",
    "알아봐",
)

EXTERNAL_SIGNALS = (
    "web",
    "internet",
    "latest",
    "news",
    "external",
    "crawl",
    "browser",
    "웹",
    "인터넷",
    "최신",
    "뉴스",
    "외부",
    "크롤",
    "브라우저",
)

HIGH_RISK_SIGNALS = (
    "architecture",
    "production",
    "migration",
    "security",
    "distributed",
    "database",
    "legal",
    "medical",
    "financial",
    "아키텍처",
    "운영",
    "마이그레이션",
    "보안",
    "분산",
    "데이터베이스",
    "법률",
    "의료",
    "금융",
)

GRAPH_SIGNALS = (
    "graph",
    "node",
    "edge",
    "relation",
    "path",
    "topology",
    "connect",
    "graph-based",
    "그래프",
    "노드",
    "엣지",
    "관계",
    "연결",
    "경로",
    "위상",
)

DIRECT_LOOKUP_SIGNALS = (
    "list",
    "count",
    "show",
    "status",
    "names",
    "목록",
    "리스트",
    "몇 개",
    "개수",
    "갯수",
    "나열",
    "이름",
    "상태",
)

ACTION_SIGNALS = (
    "plan",
    "strategy",
    "recommend",
    "roadmap",
    "design",
    "build",
    "construct",
    "implement",
    "execute",
    "계획",
    "전략",
    "추천",
    "로드맵",
    "설계",
    "구축",
    "만들어",
    "실행해",
    "진행해",
    "어떻게 해야",
    "어떻게 할까",
    "어떤 방향으로",
    "방안",
)

KING_STRATEGY_SIGNALS = (
    "design",
    "roadmap",
    "architecture",
    "설계",
    "기획",
    "파이프라인",
)

# A faithful evidence brief should not spend a provider turn. These signals
# mark requests that need interpretation or a decision instead of a local
# projection of the observed MCP receipt.
SEMANTIC_SYNTHESIS_SIGNALS = (
    "why",
    "compare",
    "recommend",
    "insight",
    "interpret",
    "analyze",
    "strategy",
    "prioritize",
    "evaluate",
    "workflow",
    "structure",
    "relationship",
    "왜",
    "비교",
    "추천",
    "인사이트",
    "해석",
    "분석",
    "전략",
    "우선순위",
    "평가",
    "워크플로우",
    "구조",
    "관계",
    # Natural-language planning requests should produce a usable answer,
    # rather than stopping at a local evidence inventory.
    "itinerary",
    "trip plan",
    "travel plan",
    "schedule",
    "route plan",
    "코스",
    "일정",
    "동선",
    "여행",
    "여행지",
    "짜줘",
    "작성해줘",
    "골라줘",
)


def _contains(text: str, signals: tuple[str, ...]) -> bool:
    return any(signal in text for signal in signals)


def _contains_graph_signal(text: str) -> bool:
    for signal in GRAPH_SIGNALS:
        if signal.isascii():
            pattern = rf"(?<![A-Za-z0-9_]){re.escape(signal)}(?![A-Za-z0-9_])"
            if re.search(pattern, text):
                return True
        elif signal in text:
            return True
    return False

def _contains_write_intent(text: str) -> bool:
    scrubbed = text
    for phrase in NEGATED_WRITE_PHRASES:
        scrubbed = scrubbed.replace(phrase, "")
    return _contains(scrubbed, WRITE_SIGNALS)


@dataclass(frozen=True)
class GoalPlan:
    """A deterministic mission contract compiled before any model turn."""

    objective: str
    selected_pack_count: int
    selected_project_count: int
    kind: str
    complexity: str
    ontology_required: bool
    ontology_mode: str
    external_scouting: bool
    requires_write: bool
    requires_verification: bool
    requires_model_planning: bool
    requires_model_queen: bool
    outcome_type: str
    graph_required: bool
    requires_oracle_model: bool
    action_mode: str
    action_required: bool
    response_contract: List[str]
    stages: List[str] = field(default_factory=list)
    ontology_spaces: List[str] = field(default_factory=list)
    evidence_gate: str = "evidence_before_claim"
    estimated_model_turns: int = 0
    max_automatic_revision_cycles: int = 0
    token_budget: int = 0
    context_char_budget: int = 4000
    acceptance_checks: List[str] = field(default_factory=list)
    stop_conditions: List[str] = field(default_factory=list)
    retrieval_contract: Dict[str, Any] = field(default_factory=dict)
    rationale: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def classify_goal(
    objective: str,
    *,
    selected_pack_count: int = 0,
    selected_project_count: int = 0,
    knowledge_available: bool = False,
    forced: Optional[str] = None,
) -> GoalPlan:
    """Compile a user goal into a minimal executable colony graph.

    This is deliberately deterministic. It decides *whether* a role is needed;
    Codex is reserved for the role that needs interpretation or execution.
    """
    clean = " ".join(str(objective or "").split())
    text = clean.lower()
    selected_context = max(0, int(selected_pack_count)) > 0 or max(0, int(selected_project_count)) > 0
    # Selection is available context, not an instruction to invoke QUEEN. A
    # user may keep packs selected while having a casual conversation. But an
    # actionable, research, lookup, or planning request made while packs are
    # selected has an explicit knowledge dependency: silently falling back to
    # direct chat would show pack labels without actually consulting MCP.
    # Explicit ``chat`` mode still bypasses this compiler at the interaction
    # layer, so users retain a deliberate escape hatch.
    selected_context_ontology = selected_context and (
        _contains(text, ONTOLOGY_SIGNALS)
        or _contains(text, RESEARCH_SIGNALS)
        or _contains(text, DIRECT_LOOKUP_SIGNALS)
        or _contains(text, SEMANTIC_SYNTHESIS_SIGNALS)
        # Planning/design language can imply a knowledge dependency, but a
        # plain create/edit/build request must stay on the cheap code route.
        or (_contains(text, ACTION_SIGNALS) and not _contains_write_intent(text))
    )
    # When OpenCrab is connected, knowledge-shaped goals should use it even
    # without a manually selected pack. Pure code edits stay direct unless the
    # user selected an explicit knowledge scope. This is the main distinction
    # between a connected KINGCRAB workspace and a generic coding chat.
    ambient_knowledge = bool(knowledge_available) and (
        _contains(text, RESEARCH_SIGNALS)
        or _contains(text, DIRECT_LOOKUP_SIGNALS)
        or _contains(text, SEMANTIC_SYNTHESIS_SIGNALS)
    )
    contextual_ontology = selected_context_ontology or ambient_knowledge
    ontology = _contains(text, ONTOLOGY_SIGNALS) or contextual_ontology
    writes = _contains_write_intent(text)
    research = _contains(text, RESEARCH_SIGNALS)
    external = _contains(text, EXTERNAL_SIGNALS)
    high_risk = _contains(text, HIGH_RISK_SIGNALS) or len(clean) > 320
    strategic = high_risk or (
        ontology
        and (len(clean) > 180 or _contains(text, KING_STRATEGY_SIGNALS))
    )
    graph_required = ontology and _contains_graph_signal(text)

    if forced in {"chat", "colony"}:
        forced_colony = forced == "colony"
    else:
        forced_colony = False

    if ontology:
        kind = "ontology_execution" if writes else "ontology_research"
    elif writes:
        kind = "code_execution"
    elif research:
        kind = "research"
    else:
        kind = "bounded_execution" if forced_colony else "conversation"

    complexity = "strategic" if strategic else "bounded" if (writes or research or ontology) else "trivial"
    requires_verification = writes or ontology or research or high_risk
    requires_model_planning = strategic
    direct_lookup = ontology and not writes and not external and not strategic and _contains(text, DIRECT_LOOKUP_SIGNALS)
    semantic_synthesis = ontology and _contains(text, SEMANTIC_SYNTHESIS_SIGNALS)
    # A bounded evidence question can be answered by the local QUEEN from the
    # observed MCP receipt. It still creates the evidence ledger, Soldier
    # handoff, and Oracle gate; it simply does not ask a model to paraphrase
    # data that is already structured and source-backed.
    local_ontology_projection = (
        ontology
        and not writes
        and not external
        and not strategic
        and not graph_required
        and not semantic_synthesis
    )
    # A bounded write does not need a second model to paraphrase the observed
    # receipt. Local QUEEN still collects and structures the authoritative MCP
    # context; only WORKER spends a model turn on the actual change.
    bounded_ontology_write_projection = (
        ontology
        and writes
        and not external
        and not strategic
        and not graph_required
        and not semantic_synthesis
    )
    requires_model_queen = (
        ontology
        and not direct_lookup
        and not local_ontology_projection
        and not bounded_ontology_write_projection
    )
    # A bounded graph path can be verified locally. Spend an Oracle model turn
    # only when the conclusion is semantic/high-risk, external, or changes the
    # workspace; this keeps simple graph questions in the same model-turn class
    # as a direct model while adding stronger receipts.
    graph_requires_model_review = graph_required and (strategic or high_risk or writes)
    requires_oracle_model = requires_verification and (
        strategic or high_risk or external or graph_requires_model_review
    )

    if writes:
        outcome_type = "workspace_change"
    elif ontology:
        outcome_type = "ontology_brief"
    elif research:
        outcome_type = "research_brief"
    else:
        outcome_type = "answer"

    if direct_lookup:
        action_mode = "lookup"
    elif writes:
        action_mode = "execute"
    elif external or research:
        action_mode = "research"
    elif ontology and _contains(text, ACTION_SIGNALS):
        action_mode = "plan"
    elif ontology:
        action_mode = "explain"
    else:
        action_mode = "answer"
    # A request to explain, summarize, or research is already asking for a
    # usable result. Require an executable next action only when the user asks
    # for a plan or an actual workspace change; otherwise QUEEN must answer the
    # question instead of handing a coordination instruction back to KING.
    action_required = ontology and action_mode in {"plan", "execute"}
    response_contract = {
        "lookup": ["observed_items", "source_refs"],
        "explain": ["selected_path", "supported_claims", "gaps"],
        "plan": ["selected_path", "supported_claims", "ordered_actions", "gaps", "next_action"],
        "research": ["selected_path", "supported_claims", "gaps"],
        "execute": ["selected_path", "bounded_change", "verification", "next_action"],
        "answer": ["answer"],
    }.get(action_mode, ["answer"])

    stages: List[str] = ["KING"]
    if ontology:
        stages.append("QUEEN")
    if external or high_risk or ontology:
        stages.append("SOLDIER")
    if writes:
        stages.append("WORKER")
    if requires_verification:
        stages.append("ORACLE")

    # A forced colony with no semantic signal still gets one bounded worker.
    if forced_colony and "WORKER" not in stages and "ORACLE" not in stages:
        stages.append("WORKER")

    # KING and ORACLE can be deterministic gates for clear bounded work. A
    # model turn is reserved for ontology synthesis, strategic planning, real
    # external scouting, code execution, or final high-risk interpretation.
    model_turns = 0
    if requires_model_planning:
        model_turns += 1
    if requires_model_queen:
        model_turns += 1  # QUEEN interpretation
    if external:
        model_turns += 1  # SOLDIER scout
    if writes:
        model_turns += 1  # WORKER
    if requires_oracle_model:
        model_turns += 1  # ORACLE

    # A malformed Queen handoff is the one failure that can be repaired safely
    # before any workspace write. Reserve one optional turn, but do not spend
    # it unless the Soldier gate actually finds a handoff defect. This is a
    # bounded RSI loop, not an open-ended harness retry.
    max_automatic_revision_cycles = 1 if requires_model_queen else 0

    # The gate is measured in uncached provider input tokens. A persistent
    # Codex turn can spend several thousand uncached tokens before it reaches
    # the role's short answer, so a flat ontology budget can block the next
    # required role even when the compiled plan explicitly schedules it. Give
    # each planned model turn a bounded allowance instead of multiplying a
    # persistent-thread total by the number of roles.
    per_turn_input_budget = 5500
    # Optional repair is opportunistic: it may use only unused budget. Do not
    # enlarge the mission allowance merely because a repair is possible.
    planned_input_budget = max(1, model_turns) * per_turn_input_budget
    if complexity == "strategic":
        token_budget = max(6000, planned_input_budget)
        context_budget = 6000
    elif bounded_ontology_write_projection:
        token_budget = max(2600, planned_input_budget)
        context_budget = 2800
    elif ontology or research:
        token_budget = max(3200, planned_input_budget)
        context_budget = 4200
    elif writes:
        token_budget = 2200
        context_budget = 2800
    else:
        token_budget = 800
        context_budget = 1800

    spaces = ["subject", "resource", "evidence", "concept", "claim", "outcome", "lever", "policy"] if ontology else []
    retrieval_contract = (
        compile_ontology_route(clean, graph_required=graph_required, complexity=complexity)
        if ontology
        else {}
    )
    if ontology:
        retrieval_contract["scope_mode"] = "selected" if selected_context else "workspace_auto"
    rationale: List[str] = []
    if ontology:
        rationale.append("goal explicitly requires an evidence-to-claim ontology path")
        if selected_context_ontology and not _contains(text, ONTOLOGY_SIGNALS):
            rationale.append("selected OpenCrab pack/project context is an active dependency for this actionable goal")
        if ambient_knowledge and not _contains(text, ONTOLOGY_SIGNALS):
            rationale.append("connected OpenCrab workspace is the default knowledge source for this goal")
    elif selected_pack_count or selected_project_count:
        rationale.append("selected pack/project context is retained without forcing an ontology mission")
    if external:
        rationale.append("external retrieval/scouting is requested; SOLDIER owns scope and waste checks")
    if writes:
        rationale.append("a bounded WORKER is required to change the workspace")
    if not requires_model_planning:
        rationale.append("clear goal: KING is compiled locally and does not spend a planning turn")
    if direct_lookup:
        rationale.append("exact ontology lookup can be projected from the observed MCP receipt without a Queen model turn")
    elif local_ontology_projection:
        rationale.append("bounded evidence brief is projected locally; model synthesis is reserved for comparison, recommendation, and strategy")
    if action_required:
        rationale.append("this goal requires an executable next action; STOP is allowed only when the evidence gate is blocked")
    if not requires_verification:
        rationale.append("no claim or artifact is being published; ORACLE is omitted")

    acceptance_checks: List[str] = ["goal_contract_persisted", "every_claim_has_observed_receipt"]
    if ontology:
        acceptance_checks.extend(["opencrab_mcp_context_receipt_observed", "ontology_ledger_persisted"])
    if graph_required:
        acceptance_checks.append("graph_path_has_bounded_nodes_or_edges")
    if action_required:
        acceptance_checks.append("goal_specific_next_action_is_executable")
    if writes:
        acceptance_checks.extend(["workspace_change_receipt_observed", "workspace_change_is_bounded", "focused_verification_receipt_observed"])
    if requires_oracle_model:
        acceptance_checks.append("oracle_model_review_observed")
    else:
        acceptance_checks.append("oracle_local_gate_observed")
    stop_conditions = [
        "stop_on_missing_authoritative_context",
        "stop_before_model_when_token_budget_is_exhausted",
        "stop_on_repeated_no_progress_receipt",
    ]
    return GoalPlan(
        objective=clean,
        selected_pack_count=max(0, int(selected_pack_count)),
        selected_project_count=max(0, int(selected_project_count)),
        kind=kind,
        complexity=complexity,
        ontology_required=ontology,
        external_scouting=external,
        requires_write=writes,
        requires_verification=requires_verification,
        requires_model_planning=requires_model_planning,
        requires_model_queen=requires_model_queen,
        outcome_type=outcome_type,
        graph_required=graph_required,
        requires_oracle_model=requires_oracle_model,
        action_mode=action_mode,
        action_required=action_required,
        response_contract=response_contract,
        stages=stages,
        ontology_spaces=spaces,
        ontology_mode=(
            "graph_path"
            if graph_required
            else "receipt_projection"
            if direct_lookup
            else "evidence_brief"
            if local_ontology_projection
            else "evidence_query"
            if ontology
            else "none"
        ),
        estimated_model_turns=model_turns,
        max_automatic_revision_cycles=max_automatic_revision_cycles,
        token_budget=token_budget,
        context_char_budget=context_budget,
        acceptance_checks=acceptance_checks,
        stop_conditions=stop_conditions,
        retrieval_contract=retrieval_contract,
        rationale=rationale,
    )


def interaction_kind_for_goal(
    objective: str,
    mode: str = "auto",
    *,
    selected_pack_count: int = 0,
    selected_project_count: int = 0,
    knowledge_available: bool = False,
) -> str:
    if mode in {"chat", "colony"}:
        return mode
    plan = classify_goal(
        objective,
        selected_pack_count=selected_pack_count,
        selected_project_count=selected_project_count,
        knowledge_available=knowledge_available,
    )
    # Knowledge questions with explicit pack/OpenCrab semantics are colony
    # work: Queen retrieves the bounded graph context and Oracle closes it.
    if plan.kind.startswith("ontology_") or plan.requires_write:
        return "colony"
    return "chat"
