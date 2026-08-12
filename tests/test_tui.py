import asyncio
import json
from pathlib import Path
from typing import Any, Dict

from rich.cells import cell_len
from textual.events import MouseMove

from crabagent.onboarding import record_opencrab_connected
from crabagent.tui import CrabAgentApp, FirstRunScreen, OpenCrabEndpointScreen


class FakeClient:
    def __init__(self) -> None:
        self.session = {
            "session_id": "session-test1234",
            "title": "Test",
            "status": "ready",
            "model_policy": "auto",
            "interaction_mode": "auto",
            "mcp_policy": "auto",
            "cli_policy": "auto",
            "max_workers": 3,
            "worker_policy": "auto",
            "active_mission_id": None,
        }
        self.messages = [
            {"message_id": 1, "role": "user", "content": "첫 질문", "metadata": {}},
            {"message_id": 2, "role": "assistant", "content": "첫 답변", "metadata": {"role": "CRAB", "interaction": "chat"}},
        ]

    def request(self, action: str, **payload: Any) -> Dict[str, Any]:
        if action == "session.ensure":
            return dict(self.session)
        if action == "assets":
            return {
                "codex": {"status": "available", "executable": "/bin/codex"},
                "mcp_servers": ["OpenCrab"],
                "skills": ["crab"],
                "clis": {"git": "/usr/bin/git"},
                "opencrab_signup_url": "https://opencrab.sh",
            }
        if action == "session.snapshot":
            after = int(payload.get("after_message_id") or 0)
            return {
                "session": dict(self.session),
                "messages": [message for message in self.messages if message["message_id"] > after],
                "pending_requests": [],
                "queue": [],
                "mission": None,
            }
        if action == "ui.snapshot":
            snapshot = self.request("session.snapshot", **payload)
            snapshot["colony"] = self.request("colony.overview")
            return snapshot
        if action == "conversation.export":
            return {"messages": list(self.messages), "total": len(self.messages), "truncated": False}
        if action == "colony.overview":
            return {
                "active_workers": 0,
                "soldier_activity": "idle",
                "ontology": {
                    "status": "ok",
                    "access_scope": "connected_account",
                    "available_pack_count": 4,
                    "has_more_packs": True,
                },
                "sessions": [
                    {
                        "session_id": self.session["session_id"],
                        "king_title": "Test king",
                        "status": "ready",
                        "project_name": "Unassigned",
                        "queen_ontology_count": 0,
                        "roles": {},
                    }
                ],
            }
        if action == "session.configure":
            self.session["model_policy"] = payload["model_policy"]
            self.session["interaction_mode"] = payload["interaction_mode"]
            self.session["mcp_policy"] = payload["mcp_policy"]
            self.session["cli_policy"] = payload.get("cli_policy", self.session.get("cli_policy", "auto"))
            self.session["max_workers"] = payload["max_workers"]
            self.session["worker_policy"] = payload.get("worker_policy", self.session.get("worker_policy", "auto"))
            return dict(self.session)
        if action == "session.fork":
            branch = dict(self.session)
            branch["session_id"] = "session-branch5678"
            branch["title"] = "Test branch"
            branch["status"] = "ready"
            branch["codex_thread_id"] = "thread-branch"
            branch["active_mission_id"] = None
            self.session = branch
            return {"session": dict(branch), "copied_messages": len(self.messages), "codex_context_forked": True}
        if action == "mcp.list":
            return {"servers": [{"name": "OpenCrab", "state": "enabled"}]}
        if action == "mcp.configure":
            self.session["mcp_policy"] = payload.get("mode") or "allow:OpenCrab"
            return dict(self.session)
        if action == "cli.list":
            return {"clis": {"git": "/usr/bin/git", "node": "/usr/bin/node"}, "codex": {"status": "available"}}
        if action == "ontology.catalog":
            return {"status": "unavailable", "payload": {}, "error": "fake client has no live catalog"}
        if action == "project.list":
            return self.request("colony.overview")
        if action == "project.assign":
            self.session["project_name"] = payload["project_name"]
            self.session["ontology_context_count"] = payload.get("ontology_context_count", 0)
            return dict(self.session)
        if action == "project.clear":
            self.session["project_name"] = ""
            self.session["ontology_context_count"] = 0
            return dict(self.session)
        raise AssertionError(action)


class LiveFakeClient(FakeClient):
    def request(self, action: str, **payload: Any) -> Dict[str, Any]:
        if action == "ontology.catalog":
            return {
                "status": "ok",
                "payload": {
                    "mode": "live_catalog",
                    "complete": True,
                    "display_scope": "connected_account",
                    "linked_workspace_count": 1,
                    "hydration": {"complete": True, "cache_hits": 0, "fetched": 1, "errors": []},
                    "account": {"tier": "enterprise"},
                    "projects": {"total": 1, "items": [{"project_id": "p-live", "name": "ALEXAI", "package_count": 1}]},
                    "packs": {"total": 1, "items": [{"package_id": "k-live", "title": "Fable5xGLM5.2", "project_id": "p-live", "project_name": "ALEXAI"}]},
                },
            }
        if action == "ontology.context":
            self.session["ontology_context_count"] = len(payload.get("package_ids") or [])
            context = {
                "project_ids": payload.get("project_ids") or [],
                "package_ids": payload.get("package_ids") or [],
                "project_labels": payload.get("project_labels") or {},
                "package_labels": payload.get("package_labels") or {},
                "count": len(payload.get("package_ids") or []),
            }
            self.session["ontology_context"] = context
            return {"session": dict(self.session), "context": context}
        return super().request(action, **payload)


class StartupTimeoutClient(FakeClient):
    def request(self, action: str, **payload: Any) -> Dict[str, Any]:
        if action == "session.ensure":
            raise TimeoutError("daemon request timed out")
        return super().request(action, **payload)


class ProjectDeleteClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.projects = [{"project_id": "project-1", "name": "Launch", "session_count": 1}]

    def request(self, action: str, **payload: Any) -> Dict[str, Any]:
        if action in {"colony.overview", "project.list"}:
            result = super().request("colony.overview", **payload)
            result["projects"] = list(self.projects)
            return result
        if action == "project.delete":
            self.projects = []
            return {"project_id": "project-1", "name": "Launch"}
        return super().request(action, **payload)


def test_tui_turns_internal_oracle_receipts_into_user_facing_text() -> None:
    raw_oracle = """ORACLE RESULT

목표: 그리스 여행 5박 6일 코스
근거 4개 · 모델 턴 0 · 로컬 게이트 4

SELECTED_PATH
subject -> resource -> evidence -> claim -> outcome (evidence_brief)
- 플라카 지구와 아크로폴리스 주변 숙소 [evidence_id: 99507117-2d5d-4d19-b5ed-88debc9286e2] [source: opencrab://evidence/99507117-2d5d-4d19-b5ed-88debc9286e2]
SOURCE_REFS
- [evidence_id: 99507117-2d5d-4d19-b5ed-88debc9286e2] [source: opencrab://evidence/99507117-2d5d-4d19-b5ed-88debc9286e2]
SLOT_COVERAGE
- goal-ef5af994d5376e98.evidence.subject: unresolved
SUPPORTED_CLAIMS
- Observed evidence is listed with its source and stable ID; no unsupported semantic claim was generated.
GAPS
- Model synthesis was not requested; the bounded action is handed to WORKER after the evidence gate.
NEXT_ACTION
STOP
"""
    display = CrabAgentApp._format_message(
        {"role": "assistant", "content": raw_oracle, "metadata": {"role": "ORACLE"}}
    )
    system = CrabAgentApp._format_message(
        {"role": "system", "content": "New durable session session-3da835cf6568", "metadata": {}}
    )
    context = CrabAgentApp._friendly_system_text(
        "QUEEN CONTEXT APPLIED\nProjects: 0\nPacks: 1\nSelected references will be carried into the next Chat/Colony turn."
    )

    assert "확인된 내용" in display
    assert "플라카 지구" in display
    assert "SELECTED_PATH" not in display
    assert "SLOT_COVERAGE" not in display
    assert "99507117" not in display
    assert "opencrab://" not in display
    assert "새 대화를 준비했습니다." in system
    assert "session-3da835cf6568" not in system
    assert "온톨로지 컨텍스트를 연결했습니다." in context


def test_tui_hides_internal_colony_handoffs_but_keeps_user_facing_oracle() -> None:
    internal = [
        {"role": "assistant", "content": "KING PLAN\ngoal-1234567890", "metadata": {"role": "KING"}},
        {"role": "assistant", "content": "QUEEN RETRIEVAL\nopencrab://evidence/abc", "metadata": {"role": "QUEEN"}},
        {"role": "assistant", "content": "worker implementation log", "metadata": {"role": "WORKER"}},
        {"role": "assistant", "content": "soldier patrol", "metadata": {"role": "SOLDIER"}},
        {"role": "assistant", "content": "final answer", "metadata": {"role": "ORACLE", "user_facing": True}},
    ]

    assert all(not CrabAgentApp._is_user_facing_message(message) for message in internal[:4])
    assert CrabAgentApp._is_user_facing_message(internal[-1])
    display = CrabAgentApp._format_message(internal[-1])
    assert "final answer" in display

    clean_oracle = CrabAgentApp._format_message(
        {
            "role": "assistant",
            "content": "ORACLE RESULT\n목표: 확인\n근거 2개 · 모델 턴 1 · 로컬 게이트 1\n\n결론입니다.",
            "metadata": {"role": "ORACLE", "user_facing": True},
        }
    )
    assert "결론입니다." in clean_oracle
    assert "목표: 확인" not in clean_oracle
    assert "근거 2개" not in clean_oracle


def test_first_run_exposes_direct_codex_and_opencrab_connection_buttons(
    tmp_path: Path, monkeypatch
) -> None:
    opened = []
    monkeypatch.setattr("crabagent.tui.webbrowser.open", lambda url: opened.append(url) or True)

    async def exercise() -> None:
        app = CrabAgentApp(tmp_path, client=FakeClient(), interactive_onboarding=False, clipboard_writer=lambda text: True)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            app.push_screen(
                FirstRunScreen(
                    {
                        "codex": {"status": "available", "login_status": "not_connected"},
                        "assets": {"mcp_count": 2, "cli_count": 3, "skill_count": 4},
                        "opencrab": {"status": "not_connected"},
                    }
                ),
                app._finish_first_run,
            )
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, FirstRunScreen)
            assert screen.query_one("#first-run-codex").disabled is False
            assert screen.query_one("#first-run-opencrab").disabled is False
            await pilot.click("#first-run-opencrab")
            await pilot.pause()
            assert opened == ["https://opencrab.sh"]
            assert isinstance(app.screen, OpenCrabEndpointScreen)

    asyncio.run(exercise())


def test_tui_mounts_real_command_deck_without_a_daemon(tmp_path: Path) -> None:
    async def exercise() -> None:
        copied = []
        app = CrabAgentApp(tmp_path, client=FakeClient(), clipboard_writer=lambda text: copied.append(text) or True)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            assert "st1234" in str(app.query_one("#connection").render())
            assert app.query_one("#connection").parent.id == "brand-stack"
            assert "ＫＩＮＧＣＲＡＢ" in str(app.query_one("#brand-king").render())
            assert app.query_one("#brand-king").styles.border.top[0] == ""
            rendered_crab = str(app.query_one("#crab-pet").render())
            assert any(0x2800 <= ord(char) <= 0x28FF for char in rendered_crab)
            assert rendered_crab.count("\n") <= 3
            assert app.query_one("#brand-king").region.width >= 20
            assert app.query_one("#crab-pet").region.width >= 24
            assert app.query_one("#workers").region.width >= 12
            assert app.query_one("#workers").value == "auto"
            assert "Ontology  4+" in str(app.query_one("#colony-summary").render())
            assert "COLONY READY / DIRECT CHAT" in str(app.query_one("#colony-summary").render())
            assert "Context   0 packs" in app.query_one("#queen-ontology").text
            assert "available" in str(app.query_one("#assets").render())
            assert "https://opencrab.sh" in str(app.query_one("#assets").render())
            assert app.query_one("#kings").root.children
            assert "READY" in str(app.query_one("#mission-title").render())
            assert app.query_one("#composer").region.height >= 3
            assert app.query_one("#composer").region.width >= 20
            assert app.query_one("#hint").parent.id == "center"
            conversation_tools = app.query_one("#conversation-tools").region
            conversation_actions = app.query_one("#conversation-actions").region
            center = app.query_one("#center").region
            assert app.query_one("#conversation-title").region.width >= 15
            assert conversation_actions.height == conversation_tools.height
            assert abs((conversation_actions.x + conversation_actions.width / 2) - (center.x + center.width / 2)) <= 1
            copy_button = app.query_one("#copy-conversation").region
            assert copy_button.y > conversation_tools.y
            assert copy_button.y + copy_button.height < conversation_tools.y + conversation_tools.height
            assert app.query_one("#model").value == "auto"
            assert app.query_one("#workers").value == "auto"
            assert "1 · SET" in str(app.query_one("#mcp-settings").render())
            assert "1 · SET" in str(app.query_one("#cli-settings").render())
            await pilot.click("#mcp-settings")
            await pilot.pause()
            assert any("OpenCrab" in str(option.prompt) for option in app.screen.query_one("#mcp-selection").options)
            await pilot.click("#mcp-settings-apply")
            assert app.session["mcp_policy"] == "all"
            await pilot.click("#cli-settings")
            await pilot.pause()
            assert "/usr/bin/git" in app.screen.query_one("#cli-inventory").render().plain
            await pilot.click("#cli-settings-apply")
            assert app.session["cli_policy"] == "auto"
            await pilot.click("#hide-left")
            assert app.query_one("#left").styles.display == "none"
            await pilot.click("#show-panels")
            assert app.query_one("#left").styles.display == "block"
            await pilot.click("#hide-right")
            await pilot.click("#show-panels")
            assert app.query_one("#right").styles.display == "block"
            app._apply_panel_layout(100)
            assert app.query_one("#left").styles.display == "none"
            assert app.query_one("#right").styles.display == "none"
            app._apply_panel_layout(140)
            app._slash("/mcp on OpenCrab")
            app._slash("/cli")
            app._slash("/project set ALEXAI")
            assert app.session["mcp_policy"] == "allow:OpenCrab"
            assert app.session["project_name"] == "ALEXAI"
            transcript = app.query_one("#transcript")
            transcript.focus()
            await pilot.press("ctrl+a")
            selected = transcript.selected_text
            assert "YOU\n첫 질문" in selected
            assert "CRAB\n첫 답변" in selected
            app.action_copy_conversation()
            assert copied == [selected]
            app.action_branch_conversation()
            assert app.session["session_id"] == "session-branch5678"
            assert app.session["codex_thread_id"] == "thread-branch"

    asyncio.run(exercise())


def test_tui_keeps_panel_alive_when_daemon_bootstrap_times_out(tmp_path: Path) -> None:
    async def exercise() -> None:
        app = CrabAgentApp(tmp_path, client=StartupTimeoutClient())
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            assert app.is_running
            assert "reconnecting" in str(app.query_one("#connection").render())
            assert "waiting for crabd" in str(app.query_one("#mission-title").render())

    asyncio.run(exercise())


def test_project_delete_keeps_tui_alive(tmp_path: Path) -> None:
    async def exercise() -> None:
        app = CrabAgentApp(tmp_path, client=ProjectDeleteClient())
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            await pilot.click("#project-delete-project-1")
            await pilot.pause()
            assert app.screen.__class__.__name__ == "ProjectDeletePrompt"
            await pilot.click("#project-delete-confirm")
            await pilot.pause()
            assert app.is_running
            assert app.screen.__class__.__name__ == "Screen"
            assert "Project deleted" in app.query_one("#transcript").text

    asyncio.run(exercise())


def test_tui_activity_uses_observed_running_state(tmp_path: Path) -> None:
    async def exercise() -> None:
        client = FakeClient()
        client.session["status"] = "running"
        app = CrabAgentApp(tmp_path, client=client, clipboard_writer=lambda text: True)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            app._tick_activity()
            assert app.query_one("#branch-conversation").disabled is True
            first = str(app.query_one("#activity").render())
            app._tick_activity()
            second = str(app.query_one("#activity").render())
            assert "WORKING." in first
            assert "WORKING.." in second
            assert "CODEX TURN" in second

    asyncio.run(exercise())


def test_tui_applies_visible_role_colors_to_conversation_blocks(tmp_path: Path) -> None:
    async def exercise() -> None:
        app = CrabAgentApp(tmp_path, client=FakeClient(), clipboard_writer=lambda text: True)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            transcript = app.query_one("#transcript")
            transcript.load_text("YOU\n첫 질문\n\nCRAB\n첫 답변\n\nSYSTEM\n시스템 안내")
            you_style = transcript.render_line(0)._segments[0].style
            crab_style = transcript.render_line(3)._segments[0].style
            system_style = transcript.render_line(6)._segments[0].style
            assert you_style.bgcolor is None
            assert tuple(you_style.color.triplet) == (98, 181, 255)
            assert crab_style.bgcolor is None
            assert tuple(crab_style.color.triplet) == (255, 121, 95)
            assert system_style.bgcolor is None
            assert tuple(system_style.color.triplet) == (244, 204, 99)

    asyncio.run(exercise())


def test_tui_role_colors_follow_soft_wrapped_visual_lines(tmp_path: Path) -> None:
    async def exercise() -> None:
        app = CrabAgentApp(tmp_path, client=FakeClient(), clipboard_writer=lambda text: True)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            transcript = app.query_one("#transcript")
            transcript.load_text(
                "YOU\n"
                + ("긴 질문 문장 " * 36)
                + "\n\nCRAB\n"
                + ("긴 답변 문장 " * 44)
                + "\n\nSYSTEM\n"
                + ("상태 안내 문장 " * 32)
            )
            await pilot.pause()

            expected = {
                "YOU": (98, 181, 255),
                "CRAB": (255, 121, 95),
                "SYSTEM": (244, 204, 99),
            }
            seen = set()
            wrapped_visual_lines = 0
            for visual_y in range(transcript.wrapped_document.height):
                logical_y = transcript._logical_line_for_visual_line(visual_y)
                role = transcript._role_for_visual_line(visual_y)
                if logical_y is not None and logical_y != visual_y:
                    wrapped_visual_lines += 1
                if role is None:
                    continue
                seen.add(role)
                style = transcript.render_line(visual_y)._segments[0].style
                assert tuple(style.color.triplet) == expected[role]

            assert wrapped_visual_lines > 0
            assert seen == set(expected)

    asyncio.run(exercise())


def test_tui_panel_dividers_resize_with_real_mouse_drag(tmp_path: Path) -> None:
    async def exercise() -> None:
        app = CrabAgentApp(tmp_path, client=FakeClient(), clipboard_writer=lambda text: True)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            left = app.query_one("#left")
            left_divider = app.query_one("#left-divider")
            left_before = left.region.width
            assert left_divider.region.height > 1
            left_offset_y = min(4, left_divider.region.height - 1)
            left_start_x = left_divider.region.x
            left_y = left_divider.region.y + left_offset_y
            await pilot.mouse_down(left_divider, offset=(0, left_offset_y))
            await pilot._post_mouse_events(
                [MouseMove],
                widget=None,
                offset=(left_start_x + 6, left_y),
                button=1,
            )
            await pilot.mouse_up(widget=None, offset=(left_start_x + 6, left_y))
            assert left.region.width == left_before + 6

            wide_left_before = left.region.width
            app._begin_panel_resize("left", left_divider.region.x)
            app._drag_panel_resize("left", left_divider.region.x + 60)
            app._end_panel_resize()
            await pilot.pause()
            assert left.region.width > 48
            divider_width = app.query_one("#left-divider").region.width + app.query_one("#right-divider").region.width
            assert left.region.width <= app.query_one("#body").region.width - app.query_one("#right").region.width - divider_width - 24
            left.styles.width = left_before
            await pilot.pause()

            right = app.query_one("#right")
            right_divider = app.query_one("#right-divider")
            right_before = right.region.width
            right_offset_y = min(4, right_divider.region.height - 1)
            right_start_x = right_divider.region.x
            right_y = right_divider.region.y + right_offset_y
            await pilot.mouse_down(right_divider, offset=(0, right_offset_y))
            await pilot._post_mouse_events(
                [MouseMove],
                widget=None,
                offset=(right_start_x - 6, right_y),
                button=1,
            )
            await pilot.mouse_up(widget=None, offset=(right_start_x - 6, right_y))
            assert right.region.width == right_before + 6

    asyncio.run(exercise())


def test_tui_retains_last_catalog_when_background_refresh_fails(tmp_path: Path) -> None:
    async def exercise() -> None:
        app = CrabAgentApp(tmp_path, client=LiveFakeClient(), clipboard_writer=lambda text: True)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            app._receive_live_catalog(
                {
                    "status": "ok",
                    "payload": {
                        "mode": "live_catalog_stale",
                        "complete": True,
                        "cache_status": "stale",
                        "account": {"tier": "enterprise"},
                        "projects": {"total": 1, "items": [{"project_id": "p-cached", "name": "ALEXAI"}]},
                        "packs": {"total": 1, "items": [{"package_id": "k-cached", "title": "Cached Fable5xGLM5.2"}]},
                        "refresh_deferred": True,
                    },
                }
            )
            app._receive_live_catalog({"status": "unavailable", "error": "empty remote response"})
            assert app._ontology_catalog["packs"]["total"] == 1
            assert app._ontology_catalog["refresh_deferred"] is False
            assert app._ontology_catalog["packs"]["items"][0]["title"] == "Cached Fable5xGLM5.2"
            assert "Refresh failed" in app.query_one("#queen-ontology").text

    asyncio.run(exercise())


def test_live_workspace_catalog_is_selectable_and_applied_to_session(tmp_path: Path) -> None:
    async def exercise() -> None:
        client = LiveFakeClient()
        record_opencrab_connected(tmp_path, tier="enterprise", scope="connected_account")
        app = CrabAgentApp(tmp_path, client=client, clipboard_writer=lambda text: True)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            assert "LIVE WORKSPACE" in app.query_one("#queen-ontology").text
            option_text = "\n".join(str(option.prompt) for option in app.query_one("#ontology-options").options)
            assert "ALEXAI" in option_text
            assert "Fable5xGLM5.2" in option_text
            assert "PROJECT" in option_text
            assert "PACK" in option_text
            assert "ALL" in option_text
            option_width = app._catalog_option_width()
            assert all(cell_len(str(option.prompt)) <= option_width for option in app.query_one("#ontology-options").options)
            long_option = app._catalog_option(
                "[ ] PACK ",
                "AI 사이언스 대형 온톨로지 연구팩",
                "",
                option_id="pack:long-label",
            )
            assert cell_len(str(long_option.prompt)) <= option_width
            assert app.query_one("#ontology-apply").region.x < app.query_one("#ontology-refresh").region.x
            await pilot.click("#ontology-refresh")
            assert "Selected  0 projects  ·  0 packs" in str(app.query_one("#ontology-selection").render())
            await pilot.pause()
            app._ontology_selected_projects.add("p-live")
            app._ontology_selected_packs.add("k-live")
            app._render_ontology_inventory()
            app._reset_ontology_selection()
            assert "Selected  0 projects  ·  0 packs" in str(app.query_one("#ontology-selection").render())
            app._ontology_selected_projects.add("p-live")
            app._ontology_selected_packs.add("k-live")
            app._apply_ontology_context()
            assert client.session["ontology_context_count"] == 1
            assert "Selected  1 projects  ·  1 packs" in str(app.query_one("#ontology-selection").render())

    asyncio.run(exercise())


def test_ontology_titles_reflow_when_right_panel_is_widened(tmp_path: Path) -> None:
    async def exercise() -> None:
        app = CrabAgentApp(tmp_path, client=LiveFakeClient(), clipboard_writer=lambda text: True)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            title = "Responsive Ontology Pack Title 2026"
            app._receive_live_catalog(
                {
                    "status": "ok",
                    "payload": {
                        "mode": "live_catalog",
                        "complete": True,
                        "display_scope": "connected_account",
                        "account": {"tier": "enterprise"},
                        "projects": {"total": 1, "items": [{"project_id": "p-long", "name": "ALEXAI"}]},
                        "packs": {
                            "total": 1,
                            "items": [{"package_id": "k-long", "title": title, "project_id": "p-long"}],
                        },
                    },
                }
            )
            await pilot.pause()
            options = app.query_one("#ontology-options").options
            before = str(next(option.prompt for option in options if option.id == "pack:k-long"))
            assert title not in before

            divider = app.query_one("#right-divider")
            app._begin_panel_resize("right", divider.region.x)
            app._drag_panel_resize("right", divider.region.x - 20)
            app._end_panel_resize()
            await pilot.pause()

            options = app.query_one("#ontology-options").options
            after = str(next(option.prompt for option in options if option.id == "pack:k-long"))
            assert title in after
            assert "\n" not in after

    asyncio.run(exercise())


def test_tui_lists_full_local_opencrab_inventory_in_queen_panel(tmp_path: Path) -> None:
    inventory_path = tmp_path / ".crabagent" / "opencrab" / "current.json"
    inventory_path.parent.mkdir(parents=True)
    inventory_path.write_text(
        json.dumps(
            {
                "account": {"tier": "expert"},
                "access_scope": "connected_account",
                "sync_status": "complete",
                "projects": {"total": 1, "items": [{"project_id": "p1", "name": "ALEXAI"}]},
                "packs": {
                    "total": 2,
                    "items": [
                        {"package_id": "pack-1", "title": "Fable5xGLM5.2"},
                        {"package_id": "pack-2", "title": "Ontology Starter"},
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    async def exercise() -> None:
        app = CrabAgentApp(tmp_path, client=FakeClient(), clipboard_writer=lambda text: True)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            panel = app.query_one("#queen-ontology").text
            assert "PROJECTS (1)" in panel
            assert "ALEXAI" in panel
            assert "PACKS (2)" in panel
            assert "Fable5xGLM5.2" in panel
            assert not app.query("#mission")

    asyncio.run(exercise())


def test_admin_inventory_only_shows_configured_owner_packs_and_projects(tmp_path: Path) -> None:
    inventory_path = tmp_path / ".crabagent" / "opencrab" / "current.json"
    inventory_path.parent.mkdir(parents=True)
    (inventory_path.parent / "scope.json").write_text(json.dumps({"owner_id_tail": "self1234"}), encoding="utf-8")
    inventory_path.write_text(
        json.dumps(
            {
                "account": {"tier": "enterprise"},
                "access_scope": "admin_all_customers",
                "sync_status": "complete",
                "projects": {
                    "total": 2,
                    "items": [
                        {"project_id": "mine-project", "name": "Mine", "workspace_id": "mine-workspace", "package_ids": ["mine-pack"]},
                        {"project_id": "other-project", "name": "Other", "workspace_id": "other-workspace", "package_ids": ["other-pack"]},
                    ],
                },
                "packs": {
                    "total": 3,
                    "items": [
                        {"package_id": "mine-pack", "title": "Created by me", "owner_id_tail": "self1234", "workspace_id": "mine-workspace"},
                        {"package_id": "installed-pack", "title": "Installed by me", "owner_id_tail": "self1234", "workspace_id": "mine-workspace", "origin": "marketplace_purchased", "license_scope": "purchased_public"},
                        {"package_id": "other-pack", "title": "Other customer's private pack", "owner_id_tail": "other999", "workspace_id": "other-workspace"},
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    async def exercise() -> None:
        app = CrabAgentApp(tmp_path, client=FakeClient(), clipboard_writer=lambda text: True)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            panel = app.query_one("#queen-ontology").text
            assert "PROJECTS (1)" in panel
            assert "Mine" in panel
            assert "Other" not in panel
            assert "PACKS (2)" in panel
            assert "Created by me" in panel
            assert "Installed by me" in panel
            assert "Other customer's private pack" not in panel

    asyncio.run(exercise())
