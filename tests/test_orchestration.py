import threading
import time
from pathlib import Path

from crabagent.daemon import RuntimeServer
from crabagent.mobile_gateway import MobileGateway, ensure_pairing
from crabagent.orchestration import OrchestrationStore


def test_orchestration_plan_runs_children_and_survives_fresh_store(tmp_path: Path) -> None:
    socket_path = Path("/tmp") / ("crabagent-orch-test-%s.sock" % time.time_ns())
    server = RuntimeServer(tmp_path, socket_path)
    try:
        first = server.service.store.create_session(title="First")
        second = server.service.store.create_session(title="Second")
        planned = server.dispatch(
            {
                "action": "orchestration.plan",
                "payload": {
                    "children": [
                        {"session_id": first["session_id"], "objective": "first bounded task"},
                        {"session_id": second["session_id"], "objective": "second bounded task"},
                    ],
                    "max_parallel": 2,
                },
            }
        )
        run_id = planned["state"]["orchestration_id"]
        assert planned["state"]["status"] == "planned"

        # A deterministic local executor stands in for two already-tested
        # CrabAgent child launches; the coordinator still persists fan-out,
        # terminal child state, and Oracle-level aggregation.
        server._dispatch = lambda _session, objective, _explicit="": {"status": "starting", "objective": objective}  # type: ignore[assignment]
        started = server.dispatch({"action": "orchestration.run", "payload": {"orchestration_id": run_id}})
        assert started["status"] == "started"
        deadline = time.monotonic() + 3
        state = None
        while time.monotonic() < deadline:
            state = server.dispatch({"action": "orchestration.status", "payload": {"orchestration_id": run_id}})["state"]
            if state["status"] == "completed":
                break
            time.sleep(0.02)
        assert state is not None
        assert state["status"] == "completed"
        assert [child["status"] for child in state["children"]] == ["completed", "completed"]
        replay = server.service.store.events_after(0, limit=1000)
        assert any(row["event_type"] == "orchestration_planned" for row in replay)
        assert any(row["event_type"] == "orchestration_finished" for row in replay)

        fresh = OrchestrationStore(tmp_path).load(run_id)
        assert fresh is not None
        assert fresh["status"] == "completed"
    finally:
        server.server_close()
        if socket_path.exists():
            socket_path.unlink()


def test_orchestration_restart_marks_running_state_interrupted(tmp_path: Path) -> None:
    store = OrchestrationStore(tmp_path)
    state = {
        "orchestration_id": "orch-restart-test",
        "status": "running",
        "max_parallel": 1,
        "children": [{"child_id": "child-1", "session_id": "session-1", "objective": "work", "status": "running"}],
    }
    store.save(state)
    assert store.reconcile_after_restart() == ["orch-restart-test"]
    recovered = store.load("orch-restart-test")
    assert recovered is not None
    assert recovered["status"] == "interrupted"
    assert recovered["children"][0]["status"] == "pending"


class _FakeMobileClient:
    def __init__(self) -> None:
        self.session_id = "session-mobile"
        self.calls = []

    def request(self, action: str, **payload):
        self.calls.append((action, payload))
        if action == "session.ensure":
            return {"session_id": self.session_id, "status": "ready"}
        if action == "ping":
            return {"status": "online"}
        if action == "ui.snapshot":
            return {
                "session": {"session_id": self.session_id, "status": "ready"},
                "messages": [
                    {"message_id": 1, "role": "user", "content": "hello", "metadata": {}},
                    {"message_id": 2, "role": "assistant", "content": "internal handoff", "metadata": {"role": "WORKER"}},
                    {"message_id": 3, "role": "assistant", "content": "final answer", "metadata": {"role": "ORACLE", "user_facing": True}},
                ],
                "pending_requests": [],
                "queue": [],
                "mission": None,
                "outcome": {},
                "quality": {},
            }
        if action == "prompt.submit":
            return {"status": "starting", "session_id": payload["session_id"]}
        if action == "session.interrupt":
            return {"interrupted": True}
        if action == "orchestration.status":
            return {"orchestrations": [], "summaries": []}
        raise AssertionError("unexpected fake action: %s" % action)


def test_mobile_gateway_requires_pairing_and_projects_user_facing_messages(tmp_path: Path) -> None:
    pair = ensure_pairing(tmp_path)
    fake = _FakeMobileClient()
    gateway = MobileGateway(tmp_path, client=fake, token=pair["token"])
    server = gateway.make_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    import urllib.error
    import urllib.request

    base = "http://127.0.0.1:%d" % server.server_address[1]
    try:
        with urllib.request.urlopen(base + "/health", timeout=2) as response:
            assert response.status == 200
        try:
            urllib.request.urlopen(base + "/v1/snapshot", timeout=2)
        except urllib.error.HTTPError as error:
            assert error.code == 401
        request = urllib.request.Request(base + "/v1/snapshot", headers={"Authorization": "Bearer " + pair["token"]})
        with urllib.request.urlopen(request, timeout=2) as response:
            import json
            payload = json.loads(response.read().decode("utf-8"))
        assert [message["label"] for message in payload["messages"]] == ["YOU", "ORACLE"]
        assert payload["messages"][1]["content"] == "final answer"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
