from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict

from .models import Role


COLONY_PROTOCOL_VERSION = "crab/1"
EVENT_SCHEMA_VERSION = "events/1"
TASK_CONTRACT_VERSION = "tasks/1"
ONTOLOGY_GRAMMAR_VERSION = "opencrab-9-space/1"

AI_LECTURE_BRAND = "CRABAGENT ACADEMY"
AI_LECTURE_BRAND_TAGLINE = "19 packs. One clear path to working AI."


def lecture_brand_context(package_count: int) -> str:
    """Return a grounded brand brief without inventing pack contents."""
    if package_count != 19:
        return ""
    return (
        "[CRABAGENT ACADEMY BRAND MODE]\n"
        "Build an AI lecture brand named CRABAGENT ACADEMY.\n"
        "Positioning: turn 19 selected knowledge packs into a clear path to working AI.\n"
        "Use only evidence returned by the connected MCP; do not infer individual pack contents."
    )


@dataclass(frozen=True)
class AgentHello:
    role: Role
    provider: str
    model: str
    colony_protocol: str = COLONY_PROTOCOL_VERSION
    event_schema: str = EVENT_SCHEMA_VERSION
    task_contract: str = TASK_CONTRACT_VERSION
    ontology_grammar: str = ONTOLOGY_GRAMMAR_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CompatibilityResult:
    accepted: bool
    reason: str


def check_compatibility(hello: AgentHello) -> CompatibilityResult:
    """Accept shared semantics, not merely a shared model or provider."""
    expected = {
        "colony_protocol": COLONY_PROTOCOL_VERSION,
        "event_schema": EVENT_SCHEMA_VERSION,
        "task_contract": TASK_CONTRACT_VERSION,
        "ontology_grammar": ONTOLOGY_GRAMMAR_VERSION,
    }
    observed = hello.to_dict()
    mismatches = [
        "%s=%s" % (key, observed[key])
        for key, value in expected.items()
        if observed[key] != value
    ]
    if mismatches:
        return CompatibilityResult(False, "incompatible colony semantics: %s" % ", ".join(mismatches))
    return CompatibilityResult(True, "shared signal and action contracts verified")
