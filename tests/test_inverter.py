from crabagent.inverter import SolteluInverter
from crabagent.identity import AgentHello, check_compatibility
from crabagent.models import Role


def test_role_routes_are_codex_only_and_planned() -> None:
    router = SolteluInverter()
    expected = {
        Role.KING: ("sol", "high"),
        Role.QUEEN: ("terra", "high"),
        Role.WORKER: ("luna", "low"),
        Role.SOLDIER: ("luna", "low"),
        Role.ORACLE: ("sol", "high"),
    }
    for role, (profile, effort) in expected.items():
        route = router.plan(role)
        assert route.provider == "codex"
        assert route.profile == profile
        assert route.effort == effort
        assert route.invocation_status == "planned"


def test_soldier_escalates_judgment_without_changing_provider() -> None:
    route = SolteluInverter().plan(Role.SOLDIER, "ontology_judgment")
    assert route.provider == "codex"
    assert route.profile == "terra"
    assert route.effort == "medium"


def test_colony_identity_depends_on_shared_contracts_not_model_name() -> None:
    compatible_other_model = AgentHello(
        role=Role.WORKER,
        provider="future-adapter",
        model="future-model",
    )
    incompatible_codex = AgentHello(
        role=Role.WORKER,
        provider="codex",
        model="gpt-5.6-luna",
        event_schema="events/999",
    )
    assert check_compatibility(compatible_other_model).accepted is True
    assert check_compatibility(incompatible_codex).accepted is False
