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
    "no external changes",
    "without external changes",
    "external changes prohibited",
    "external mutation prohibited",
    "do not change external state",
    "do not mutate external state",
    "만들지",
    "고치지",
    "수정하지",
    "구현하지",
    "설치하지",
    "배포하지",
    "추가하지",
    "삭제하지",
    "변경하지",
    "변경 금지",
    "변경을 금지",
    "변경해서는 안",
    "변경하면 안",
    "작성하지",
    "개발하지",
)

NEGATED_WRITE_CONTEXTS = (
    r"\b(?:do not|don't|must not|never)\b[^.!?;\n]{0,48}\b(?:build|create|fix|implement|install|deploy|edit|add|remove|update|refactor|write|save|modify)\w*\b",
    r"\bwithout\b[^.!?;\n]{0,36}\b(?:changing|modifying|editing|writing|creating|saving|updating)\b",
    r"(?:만들|고치|수정|구현|설치|배포|추가|삭제|변경|작성|개발)(?:(?:하거나|하고|하며|·|및|,)\s*(?:[가-힣A-Za-z_]+)){0,3}(?:은|는|을|를|도|만)?\s*하지\s*(?:마|말|않)",
    r"(?:만들기|고치기|수정|구현|설치|배포|추가|삭제|변경|작성|개발)\s*(?:은|는|을|를)?\s*금지",
)

EXPLICIT_BOOLEAN_CONTRACT_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?P<key>graph_required|requires_write|read_only|requires_worker_output)\s*[:=]\s*(?P<value>true|false|1|0|yes|no)(?![A-Za-z0-9_])",
    flags=re.IGNORECASE,
)

NEGATED_GRAPH_CONTEXTS = (
    r"(?:graph|graph\s+path|graph\s+traversal|relationship\s+graph)[^.!?;\n]{0,60}(?:not\s+required|optional|do\s+not|don't|without|skip|avoid|없어도)",
    r"(?:그래프|그래프\s*경로|관계\s*그래프)[^.!?;\n]{0,60}(?:필요\s*없|없어도|하지\s*마|하지\s*말|하지\s*않|금지|제외|배제)",
)

WORKER_OUTPUT_PATTERNS = (
    r"(?<![A-Za-z0-9_])worker(?![A-Za-z0-9_])[^.!?;\n]{0,220}(?:후보표|근거표|결과표|산출물|보고서|worker_result|deliverable|output|table|report)[^.!?;\n]{0,120}(?:작성|정리|생성|produce|write|draft|return)",
    r"(?<![A-Za-z0-9_])worker(?![A-Za-z0-9_])[^.!?;\n]{0,220}(?:작성|정리|생성|produce|write|draft|return)[^.!?;\n]{0,120}(?:후보표|근거표|결과표|산출물|보고서|worker_result|deliverable|output|table|report)",
)

# These words describe an observed change, not an instruction to make one.
# Remove only the bounded read/list forms so imperative requests such as
# "update the config" and "파일을 수정해" remain write intent.
NEGATED_SCOPE_CONTEXTS = (
    # English negative-scope clauses must not activate knowledge/external routing.
    r"\b(?:do not|don't|must not)\b[^.!?;\n]*\b(?:opencrab|ontology|knowledge graph|rag|evidence|mcp|web|internet|external|browser|crawl|research|search)\b[^.!?;\n]*",
    r"\bwithout\b[^.!?;\n]*\b(?:opencrab|ontology|knowledge graph|rag|evidence|mcp|web|internet|external|browser|crawl|research|search)\b[^.!?;\n]*",
    r"\b(?:no|without|never|avoid)\s+external\s+(?:changes?|mutations?|writes?)\b",
    # Korean equivalents, including phrases such as '오픈크랩을 사용하지 말고'.
    r"외부\s*(?:변경|상태|데이터)\s*(?:금지|하지\s*마|하지\s*말|하지\s*않)",
    r"(?:opencrab|오픈크랩|온톨로지|지식\s*그래프|근거|팩|웹|인터넷|외부\s*(?:근거|서비스)?|브라우저|검색|조사)[^.!?;\n]{0,60}(?:사용하지|접근하지|조회하지|검색하지|조사하지|쓰지|말고|않고|없이|금지|불필요|필요\s*(?:없|하지\s*않))[^.!?;\n]*",
)

READ_ONLY_CHANGE_CONTEXTS = (
    r"\b(?:change|update|modification|revision|edit(?:ed)?)\s+(?:history|logs?|records?|list)\b",
    r"\b(?:added|deleted|modified|updated|changed)\s+(?:files?|items?|records?|entries?|documents?)\b",
    r"\b(?:show|list|view|read|check|inspect|review|display)\b(?:\s+(?:the|all|recent|current))?\s+(?:changes?|updates?|modifications?|revisions?|edits?|additions?|deletions?)(?:\s+(?:history|logs?|records?|list))?\b",
    r"(?:변경|수정|추가|삭제|업데이트)(?:된|한)?\s*(?:내역|이력|기록|목록|사항|내용|파일|항목|문서)",
    r"(?:파일|항목|문서|프로젝트|팩)\s*(?:변경|수정|추가|삭제|업데이트)\s*(?:내역|이력|기록|목록|사항)",
    r"(?:수정|변경|업데이트)(?:본|안|사항|내용|범위|대상|결과|이력|내역)",
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
    "합성",
    "synthesize",
    "synthesis",
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

def _explicit_boolean_contract(text: str, key: str) -> Optional[bool]:
    requested_key = str(key or "").strip().casefold()
    for match in EXPLICIT_BOOLEAN_CONTRACT_RE.finditer(str(text or "")):
        if str(match.group("key") or "").casefold() != requested_key:
            continue
        return str(match.group("value") or "").casefold() in {"true", "1", "yes"}
    return None


def _contains_negated_graph_context(text: str) -> bool:
    return any(re.search(pattern, str(text or ""), flags=re.IGNORECASE) for pattern in NEGATED_GRAPH_CONTEXTS)


def _requires_worker_output(text: str) -> bool:
    explicit = _explicit_boolean_contract(text, "requires_worker_output")
    if explicit is not None:
        return explicit
    return any(re.search(pattern, str(text or ""), flags=re.IGNORECASE) for pattern in WORKER_OUTPUT_PATTERNS)


def _scope_signal_text(text: str) -> str:
    scrubbed = text
    for pattern in NEGATED_SCOPE_CONTEXTS:
        scrubbed = re.sub(pattern, " ", scrubbed, flags=re.IGNORECASE)
    return scrubbed


def _contains_write_intent(text: str) -> bool:
    scrubbed = text
    for phrase in NEGATED_WRITE_PHRASES:
        scrubbed = scrubbed.replace(phrase, "")
    for pattern in NEGATED_WRITE_CONTEXTS:
        scrubbed = re.sub(pattern, " ", scrubbed, flags=re.IGNORECASE)
    for pattern in READ_ONLY_CHANGE_CONTEXTS:
        scrubbed = re.sub(pattern, " ", scrubbed, flags=re.IGNORECASE)
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
    requires_worker_output: bool
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
    signal_text = _scope_signal_text(text)
    selected_context = max(0, int(selected_pack_count)) > 0 or max(0, int(selected_project_count)) > 0
    # Selection is available context, not an instruction to invoke QUEEN. A
    # user may keep packs selected while having a casual conversation. But an
    # actionable, research, lookup, or planning request made while packs are
    # selected has an explicit knowledge dependency: silently falling back to
    # direct chat would show pack labels without actually consulting MCP.
    # Explicit ``chat`` mode still bypasses this compiler at the interaction
    # layer, so users retain a deliberate escape hatch.
    selected_context_ontology = selected_context and (
        _contains(signal_text, ONTOLOGY_SIGNALS)
        or _contains(signal_text, RESEARCH_SIGNALS)
        or _contains(signal_text, DIRECT_LOOKUP_SIGNALS)
        or _contains(signal_text, SEMANTIC_SYNTHESIS_SIGNALS)
        # Planning/design language can imply a knowledge dependency, but a
        # plain create/edit/build request must stay on the cheap code route.
        or (_contains(signal_text, ACTION_SIGNALS) and not _contains_write_intent(text))
    )
    # When OpenCrab is connected, knowledge-shaped goals should use it even
    # without a manually selected pack. Pure code edits stay direct unless the
    # user selected an explicit knowledge scope. This is the main distinction
    # between a connected KINGCRAB workspace and a generic coding chat.
    ambient_knowledge = bool(knowledge_available) and (
        _contains(signal_text, RESEARCH_SIGNALS)
        or _contains(signal_text, DIRECT_LOOKUP_SIGNALS)
        or _contains(signal_text, SEMANTIC_SYNTHESIS_SIGNALS)
    )
    contextual_ontology = selected_context_ontology or ambient_knowledge
    ontology = _contains(signal_text, ONTOLOGY_SIGNALS) or contextual_ontology
    explicit_write = _explicit_boolean_contract(text, "requires_write")
    explicit_read_only = _explicit_boolean_contract(text, "read_only")
    writes = _contains_write_intent(text)
    if explicit_write is not None:
        writes = explicit_write
    if explicit_read_only is True:
        writes = False
    requires_worker_output = _requires_worker_output(text)
    research = _contains(signal_text, RESEARCH_SIGNALS)
    external = _contains(signal_text, EXTERNAL_SIGNALS)
    high_risk = _contains(signal_text, HIGH_RISK_SIGNALS) or len(clean) > 320
    strategic = high_risk or (
        ontology
        and (len(clean) > 180 or _contains(signal_text, KING_STRATEGY_SIGNALS))
    )
    explicit_graph_required = _explicit_boolean_contract(text, "graph_required")
    if explicit_graph_required is not None:
        graph_required = ontology and explicit_graph_required
    elif _contains_negated_graph_context(text):
        graph_required = False
    else:
        graph_required = ontology and _contains_graph_signal(signal_text)

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

    complexity = "strategic" if strategic else "bounded" if (writes or requires_worker_output or research or ontology) else "trivial"
    requires_verification = writes or requires_worker_output or ontology or research or high_risk
    requires_model_planning = strategic
    direct_lookup = ontology and not writes and not external and not strategic and _contains(signal_text, DIRECT_LOOKUP_SIGNALS)
    semantic_synthesis = ontology and _contains(signal_text, SEMANTIC_SYNTHESIS_SIGNALS)
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
    elif ontology and _contains(signal_text, ACTION_SIGNALS):
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
    if writes or requires_worker_output:
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
    if writes or requires_worker_output:
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
        if selected_context_ontology and not _contains(signal_text, ONTOLOGY_SIGNALS):
            rationale.append("selected OpenCrab pack/project context is an active dependency for this actionable goal")
        if ambient_knowledge and not _contains(signal_text, ONTOLOGY_SIGNALS):
            rationale.append("connected OpenCrab workspace is the default knowledge source for this goal")
    elif selected_pack_count or selected_project_count:
        rationale.append("selected pack/project context is retained without forcing an ontology mission")
    if external:
        rationale.append("external retrieval/scouting is requested; SOLDIER owns scope and waste checks")
    if writes:
        rationale.append("a bounded WORKER is required to change the workspace")
    elif requires_worker_output:
        rationale.append("a non-mutating WORKER deliverable is explicitly required and must remain evidence-bound")
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
    elif requires_worker_output:
        acceptance_checks.append("worker_result_observed")
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
        requires_worker_output=requires_worker_output,
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
