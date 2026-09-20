from types import SimpleNamespace
import pytest

from crabagent.daemon import RuntimeServer, RUNTIME_CAPABILITIES
from crabagent.models import MissionContract
from crabagent.store import ColonyStore

RUN = "kc-e2e-20260921-064055-3ed02d"


def create(store, mission_id, objective, session_id=None):
    contract = MissionContract(mission_id=mission_id, objective=objective, acceptance=[], workspace=str(store.workspace),
                               risk="low", max_attempts=1, max_workers=1, token_budget=None)
    store.create_mission(contract, session_id=session_id)


def test_exact_identifier_search_filters_before_recent_limit(tmp_path):
    store = ColonyStore(tmp_path)
    store.initialize()
    create(store, "mission-old-exact", "run_id=" + RUN + ". source")
    for index in range(25):
        create(store, "mission-new-" + str(index), "run_id=kc-other-2026-" + str(index) + " source")
    create(store, "mission-prefix", "run_id=" + RUN + "-other")
    rows = store.recent_missions(limit=2, objective_token=RUN)
    assert [r["mission_id"] for r in rows] == ["mission-old-exact"]
    server = RuntimeServer.__new__(RuntimeServer)
    server.service = SimpleNamespace(store=store)
    result = server.dispatch({"action": "mission.search", "payload": {"objective_token": RUN, "limit": 2}})
    assert result["search_complete"] is True
    assert [r["mission_id"] for r in result["missions"]] == ["mission-old-exact"]
    assert "mission.identifier_search" in RUNTIME_CAPABILITIES
    assert len(store.recent_missions(limit=100)) == 27


def test_exact_search_reports_ambiguity_and_never_broadens_empty_identifier(tmp_path):
    store = ColonyStore(tmp_path)
    store.initialize()
    for index in range(3):
        create(store, "mission-" + str(index), "run_id=" + RUN)
    server = RuntimeServer.__new__(RuntimeServer)
    server.service = SimpleNamespace(store=store)
    result = server.dispatch({"action": "mission.search", "payload": {"objective_token": RUN, "limit": 2}})
    assert len(result["missions"]) == 2
    assert result["search_complete"] is False
    with pytest.raises(ValueError, match="identifier"):
        server.dispatch({"action": "mission.search", "payload": {}})
    with pytest.raises(ValueError, match="invalid"):
        store.recent_missions(objective_token="%")
    assert store.recent_missions(objective_token="unrelated-run-2026") == []
