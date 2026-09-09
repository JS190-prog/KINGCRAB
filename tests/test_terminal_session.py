from pathlib import Path

import pytest

from crabagent.models import MissionStatus, Role
from crabagent.runtime import RuntimeService
from crabagent.store import ColonyStore


def bound_mission(tmp_path: Path):
    service = RuntimeService(tmp_path)
    session_id = service.store.create_session()['session_id']
    mission_id = service.plan_mission('bounded local work', session_id=session_id)['mission']['mission_id']
    service.store.update_session(session_id, status='running', active_mission_id=mission_id)
    service.store.transition_mission(mission_id, MissionStatus.RUNNING, Role.KING)
    return service.store, session_id, mission_id


@pytest.mark.parametrize('target', [MissionStatus.COMPLETED, MissionStatus.FAILED, MissionStatus.CANCELLED])
def test_terminal_commit_releases_its_session_without_caller_cleanup(tmp_path, target):
    store, session_id, mission_id = bound_mission(tmp_path)
    if target is MissionStatus.COMPLETED:
        store.transition_mission(mission_id, MissionStatus.VERIFYING, Role.ORACLE)
    store.transition_mission(mission_id, target, Role.ORACLE)
    # A fresh observer must never see a committed terminal mission still owning
    # its session, even if the executor stops before any subsequent cleanup.
    observer = ColonyStore(tmp_path)
    assert observer.inspect(mission_id)['mission']['status'] == target.value
    session = observer.session(session_id)
    assert session['active_mission_id'] is None
    assert session['status'] == 'ready'


def test_terminal_commit_does_not_clear_another_active_mission(tmp_path):
    store, session_id, mission_id = bound_mission(tmp_path)
    store.update_session(session_id, active_mission_id='newer-mission', codex_thread_id='new-thread')
    store.transition_mission(mission_id, MissionStatus.FAILED, Role.ORACLE, codex_thread_id='old-thread')
    session = store.session(session_id)
    assert session['active_mission_id'] == 'newer-mission'
    assert session['status'] == 'running'
    assert session['codex_thread_id'] == 'new-thread'


def test_terminal_commit_persists_thread_and_rolls_back_as_one_unit(tmp_path, monkeypatch):
    store, session_id, mission_id = bound_mission(tmp_path)
    original_append = store.append_event

    def reject_event(*args, **kwargs):
        raise RuntimeError('event commit rejected')

    monkeypatch.setattr(store, 'append_event', reject_event)
    with pytest.raises(RuntimeError, match='event commit rejected'):
        store.transition_mission(mission_id, MissionStatus.FAILED, Role.ORACLE, codex_thread_id='final-thread')
    assert store.inspect(mission_id)['mission']['status'] == 'running'
    assert store.session(session_id)['active_mission_id'] == mission_id
    monkeypatch.setattr(store, 'append_event', original_append)
    store.transition_mission(mission_id, MissionStatus.FAILED, Role.ORACLE, codex_thread_id='final-thread')
    session = store.session(session_id)
    assert session['active_mission_id'] is None and session['codex_thread_id'] == 'final-thread'


@pytest.mark.parametrize('newer_owner', [False, True])
def test_user_cancellation_releases_only_its_own_session(tmp_path, newer_owner):
    store, session_id, mission_id = bound_mission(tmp_path)
    if newer_owner:
        store.update_session(session_id, active_mission_id='newer-mission')
    store.cancel_active_mission(mission_id, 'user_interrupt')
    assert store.inspect(mission_id)['mission']['status'] == 'cancelled'
    session = store.session(session_id)
    assert session['active_mission_id'] == ('newer-mission' if newer_owner else None)
    assert session['status'] == ('running' if newer_owner else 'ready')


def test_executor_return_does_not_clear_a_new_owner_after_terminal_commit(tmp_path, monkeypatch):
    service = RuntimeService(tmp_path)
    session_id = service.store.create_session()['session_id']
    transition = service.store.transition_mission

    def handoff_after_commit(mission_id, target, actor, **kwargs):
        transition(mission_id, target, actor, **kwargs)
        if target is MissionStatus.COMPLETED:
            service.store.update_session(session_id, status='running', active_mission_id='newer-mission')

    monkeypatch.setattr(service.store, 'transition_mission', handoff_after_commit)
    service.run_demo('Produce a bounded demonstration', session_id=session_id)
    session = service.store.session(session_id)
    assert session['active_mission_id'] == 'newer-mission'
    assert session['status'] == 'running'
