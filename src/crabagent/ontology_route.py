from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, List


_STOP_WORDS = {
    "and", "the", "for", "from", "with", "that", "this", "into", "about",
    "what", "which", "where", "when", "how", "why", "please", "find", "help",
    "해줘", "해", "찾아", "찾아줘", "확인", "확인해", "대해", "위한", "있는", "있어", "것을", "좀",
    "팩", "조회", "도움", "도움이", "정리", "알려", "알려줘", "보여줘",
}

_KOREAN_PARTICLE_SUFFIXES = (
    "해주세요", "해줘", "해서", "하며", "으로", "에서", "에게", "을", "를",
    "은", "는", "이", "가", "의", "와", "과", "에", "로", "도", "만",
)

_SPACE_SIGNALS = {
    "subject": ("subject", "주제", "대상", "누구", "무엇"),
    "resource": ("resource", "source", "자료", "출처", "문서", "데이터"),
    "evidence": ("evidence", "근거", "증거", "검증", "사실"),
    "concept": ("concept", "개념", "용어", "정의"),
    "claim": ("claim", "주장", "판단", "가설"),
    "outcome": ("outcome", "result", "결과", "성과", "추천", "결론"),
    "lever": ("lever", "레버", "전략", "요인", "변수", "영향"),
    "policy": ("policy", "정책", "조건", "제약", "규칙", "원칙"),
}

_RELATION_SIGNALS = {
    "supports": ("support", "근거", "지지", "뒷받침"),
    "causes": ("cause", "원인", "영향", "만들", "발생"),
    "contradicts": ("conflict", "contradict", "충돌", "반대", "모순"),
    "depends_on": ("depend", "의존", "조건", "필요"),
    "leads_to": ("lead", "결과", "이어", "연결", "경로"),
    "mentions": ("mention", "mentions", "언급"),
}


def _clean_terms(text: str) -> List[str]:
    raw = re.findall(r"[A-Za-z0-9_가-힣]{2,}", text.lower())
    terms: List[str] = []
    for value in raw:
        for suffix in _KOREAN_PARTICLE_SUFFIXES:
            if value.endswith(suffix) and len(value) - len(suffix) >= 2:
                value = value[: -len(suffix)]
                break
        if value in _STOP_WORDS or value in terms:
            continue
        terms.append(value)
    return terms[:12]


def _matches(text: str, signals: tuple[str, ...]) -> bool:
    return any(signal in text for signal in signals)


@dataclass(frozen=True)
class OntologyRoutePlan:
    """A small, deterministic search contract for the Queen's MCP work."""

    schema: str
    mode: str
    primary_query: str
    query_terms: List[str]
    spaces: List[str]
    relation_bias: List[str]
    evidence_top_k: int
    node_limit: int
    node_scan_limit: int
    context_node_limit: int
    edge_limit: int
    package_limit: int
    max_mcp_calls: int
    claim_requirements: List[str]
    explicit_spaces: List[str]
    explicit_relations: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def compile_ontology_route(
    objective: str,
    *,
    graph_required: bool = False,
    complexity: str = "bounded",
) -> Dict[str, Any]:
    """Compile an ontology-first retrieval route without using a model.

    The route is intentionally a contract, not a generated answer. It gives
    the MCP collector and Queen the same bounded search shape, which prevents
    every role from rediscovering the user's entire catalog independently.
    """
    clean = " ".join(str(objective or "").split())
    text = clean.lower()
    terms = _clean_terms(clean)
    spaces = [name for name, signals in _SPACE_SIGNALS.items() if _matches(text, signals)]
    explicit_spaces = list(spaces)
    for required in ("subject", "resource", "evidence", "claim", "outcome"):
        if required not in spaces:
            spaces.append(required)
    if graph_required:
        spaces.extend(value for value in ("concept", "lever") if value not in spaces)
    relation_bias = [name for name, signals in _RELATION_SIGNALS.items() if _matches(text, signals)]
    explicit_relations = list(relation_bias)
    if graph_required and not relation_bias:
        relation_bias = ["supports", "leads_to", "depends_on"]

    evidence_top_k = 6 if complexity == "trivial" else 8
    node_limit = 6 if graph_required else 0
    node_scan_limit = 1000 if graph_required else 0
    # A bounded graph question needs a path, not a neighborhood dump. Keep
    # one context expansion for ordinary work and reserve three for strategic
    # analysis where the extra semantic neighborhood is worth the latency.
    context_node_limit = 0 if not graph_required else 1 if complexity == "bounded" else 3
    # The first 16 edges in the live pack can be generic mentions. Keep the
    # bounded packet small, but ask for enough rows to reach evidence-backed
    # preferred relations such as supports.
    edge_limit = 24 if graph_required else 0
    # Large explicit selections can make the SaaS query planner scan too much
    # before it reaches the evidence top-k. Keep the retrieval contract honest
    # and record any omitted selection in the receipt rather than timing out.
    package_limit = 8 if graph_required else 12
    # query + node search + up to three node contexts, with one bounded
    # fallback node/edge listing when the search endpoint is unavailable.
    max_mcp_calls = 1 + (3 + context_node_limit if graph_required else 0)
    if graph_required:
        max_mcp_calls += 1
    claim_requirements = ["evidence_id", "source_uri", "captured_text"]
    if graph_required:
        claim_requirements.extend(["node_id", "typed_relation", "bounded_path"])
    return OntologyRoutePlan(
        schema="crab.ontology-route/v1",
        mode="graph_path" if graph_required else "evidence_first",
        primary_query=clean,
        query_terms=terms,
        spaces=spaces,
        relation_bias=relation_bias,
        evidence_top_k=evidence_top_k,
        node_limit=node_limit,
        node_scan_limit=node_scan_limit,
        context_node_limit=context_node_limit,
        edge_limit=edge_limit,
        package_limit=package_limit,
        max_mcp_calls=max_mcp_calls,
        claim_requirements=claim_requirements,
        explicit_spaces=explicit_spaces,
        explicit_relations=explicit_relations,
    ).to_dict()
