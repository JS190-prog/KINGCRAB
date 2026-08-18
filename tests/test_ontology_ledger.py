from __future__ import annotations

from pathlib import Path

from crabagent.ontology_contract import promote_execution_contract_to_ledger
from crabagent.runtime import RuntimeService
from crabagent.store import ColonyStore


def test_ontology_ledger_persist_readback_is_idempotent(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    planned = service.plan_mission("오픈크랩 팩의 근거를 요약해줘", adaptive=True)
    mission_id = planned["mission"]["mission_id"]
    queen_task_id = next(
        row["task_id"] for row in planned["tasks"] if row["role"] == "QUEEN"
    )
    ledger = promote_execution_contract_to_ledger(
        planned["ontology_execution_contract"],
        mission_id=mission_id,
        source_artifact_id="artifact-contract",
    )

    first = service.store.persist_ontology_ledger(
        mission_id,
        ledger,
        task_id=queen_task_id,
    )
    second = service.store.persist_ontology_ledger(
        mission_id,
        queen_task_id,
        ledger,
    )

    assert first["artifact_id"] == second["artifact_id"]
    assert second["idempotent"] is True
    assert second["readback"]["readback"]["validated"] is True
    latest = service.store.get_latest_ontology_ledger(
        mission_id,
        goal_graph_id=ledger["goal_graph_id"],
        revision=ledger["revision"],
    )
    assert latest is not None
    assert latest["_artifact"]["kind"] == "ontology_ledger"
    assert latest["mission_id"] == mission_id
    reopened = ColonyStore(tmp_path).get_latest_ontology_ledger(
        mission_id,
        goal_graph_id=ledger["goal_graph_id"],
        revision=ledger["revision"],
    )
    assert reopened is not None
    assert reopened["readback"]["validated"] is True

    rows = [
        row
        for row in service.store.inspect(mission_id)["artifacts"]
        if row["kind"] == "ontology_ledger"
    ]
    assert len(rows) == 1


def test_ontology_ledger_readback_rejects_tampered_bytes(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    planned = service.plan_mission("오픈크랩 팩의 근거를 요약해줘", adaptive=True)
    mission_id = planned["mission"]["mission_id"]
    ledger = promote_execution_contract_to_ledger(
        planned["ontology_execution_contract"],
        mission_id=mission_id,
    )
    persisted = service.store.persist_ontology_ledger(mission_id, ledger)
    path = Path(persisted["path"])
    path.write_bytes(path.read_bytes() + b"tampered")

    assert service.store.get_latest_ontology_ledger(mission_id) is None
