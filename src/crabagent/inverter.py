from __future__ import annotations

from typing import Dict

from .models import ModelRoute, Role


class SolteluInverter:
    """Codex-only route planner. A plan is not an invocation receipt."""

    _BASE: Dict[Role, ModelRoute] = {
        Role.KING: ModelRoute(
            role=Role.KING,
            provider="codex",
            profile="sol",
            model="gpt-5.6-sol",
            effort="high",
            reason="Goal, workflow, and pipeline design require the strongest planning route.",
        ),
        Role.QUEEN: ModelRoute(
            role=Role.QUEEN,
            provider="codex",
            profile="terra",
            model="gpt-5.6-terra",
            effort="high",
            reason="Ontology planning and context provisioning require grounded synthesis.",
        ),
        Role.WORKER: ModelRoute(
            role=Role.WORKER,
            provider="codex",
            profile="luna",
            model="gpt-5.6-luna",
            effort="low",
            reason="Bounded task contracts should run on the least expensive sufficient route.",
        ),
        Role.SOLDIER: ModelRoute(
            role=Role.SOLDIER,
            provider="codex",
            profile="luna",
            model="gpt-5.6-luna",
            effort="low",
            reason="Monitoring and scouting begin with cheap observable checks.",
        ),
        Role.ORACLE: ModelRoute(
            role=Role.ORACLE,
            provider="codex",
            profile="sol",
            model="gpt-5.6-sol",
            effort="high",
            reason="Only final evidence-backed synthesis is allowed to publish a result.",
        ),
    }

    def plan(self, role: Role, task_kind: str = "default") -> ModelRoute:
        route = self._BASE[role]
        if role is Role.QUEEN and task_kind == "bounded_ontology":
            return ModelRoute(
                role=role,
                provider="codex",
                profile="terra",
                model="gpt-5.6-terra",
                effort="medium",
                invocation_status="planned",
                reason="Bounded ontology interpretation uses the smallest sufficient grounded route.",
            )
        if role is Role.SOLDIER and task_kind in {"ontology_judgment", "security_review"}:
            return ModelRoute(
                role=role,
                provider="codex",
                profile="terra",
                model="gpt-5.6-terra",
                effort="medium",
                invocation_status="planned",
                reason="Irreversible quality or security judgment is escalated above patrol mode.",
            )
        return route

    def local(self, role: Role, reason: str) -> ModelRoute:
        """Return a persisted deterministic gate without invoking a provider."""
        return ModelRoute(
            role=role,
            provider="local",
            profile="rule",
            model="none",
            effort="deterministic",
            invocation_status="planned",
            reason=reason,
        )

    def matrix(self) -> Dict[str, Dict[str, str]]:
        return {role.value: self.plan(role).to_dict() for role in Role}
