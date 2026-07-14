"""CrucibleApp smoke tests via Textual's headless pilot.

These exercise the UI shell (layout, slash commands, engine-event rendering)
without any LLM: engine events are injected as messages, exactly as
TUIObserver would post them.
"""

from textual.widgets import Static, Tree

from orchestrator.providers import PROVIDERS, ResolvedProvider
from orchestrator.tui.app import CrucibleApp, EngineEvent, PromptInput, Transcript

TEST_BACKEND = ResolvedProvider(
    provider=None, api_key="test-key", base_url="https://test/v1", model="test-model"
)

ALL_KEY_ENVS = [env for p in PROVIDERS.values() for env in p.api_key_envs] + [
    "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
]


def make_app(tmp_path) -> CrucibleApp:
    return CrucibleApp(workspace=str(tmp_path), splash=False, backend=TEST_BACKEND)


def transcript_text(app: CrucibleApp) -> str:
    return "\n".join(app.query_one("#transcript", Transcript).plain_lines())


async def submit(pilot, text: str) -> None:
    prompt = pilot.app.query_one("#prompt", PromptInput)
    prompt.value = text
    await pilot.press("enter")
    await pilot.pause()


async def test_boots_full_screen_with_banner_and_widgets(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test():
        assert app.query_one("#transcript", Transcript) is not None
        assert app.query_one("#plan-tree", Tree) is not None
        assert app.query_one("#prompt", PromptInput).has_focus
        text = transcript_text(app)
        assert "C R U C I B L E" in text
        assert str(tmp_path) in text


async def test_splash_screen_shows_and_dismisses_on_key(tmp_path):
    app = CrucibleApp(workspace=str(tmp_path), splash=True)
    async with app.run_test() as pilot:
        assert app.screen.__class__.__name__ == "SplashScreen"
        await pilot.press("space")  # dismiss works even mid-glitch
        await pilot.pause()
        assert app.screen.__class__.__name__ != "SplashScreen"


async def test_splash_glitch_settles_into_aligned_logo(tmp_path):
    app = CrucibleApp(workspace=str(tmp_path), splash=True)
    async with app.run_test(size=(110, 32)) as pilot:
        splash = app.screen
        assert splash.__class__.__name__ == "SplashScreen"
        # drive the animation past its glitch window deterministically
        for _ in range(int(splash.GLITCH_SECONDS * splash.GLITCH_FPS) + 2):
            splash._glitch_tick()
        await pilot.pause()
        assert splash._settled is True
        # settle frame: plain-text logo lines are present and uniformly wide
        rendered = str(splash.query_one("#splash-logo", Static).render())
        lines = [ln for ln in rendered.splitlines() if ln.strip()]
        assert lines and len({len(ln) for ln in lines}) == 1


async def test_help_lists_commands(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await submit(pilot, "/help")
        text = transcript_text(app)
        for name in ("/permissions", "/theme", "/model", "/tokens"):
            assert name in text


async def test_unknown_command_shows_suggestion(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await submit(pilot, "/thme nord")
        assert "did you mean /theme" in transcript_text(app)


async def test_theme_command_switches_theme(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await submit(pilot, "/theme nord")
        assert app.theme == "nord"
        await submit(pilot, "/theme not-a-theme")
        assert app.theme == "nord"
        assert "unknown theme" in transcript_text(app)


async def test_model_command_sets_model_on_current_provider(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await submit(pilot, "/model my-model-v2")
        assert app.backend.model == "my-model-v2"
        assert app.backend.api_key == "test-key"  # provider unchanged
        assert "my-model-v2" in transcript_text(app)


async def test_model_command_switches_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "mm-key")
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await submit(pilot, "/model minimax")
        assert app.backend.label == "minimax"
        assert app.backend.model == "MiniMax-M3"
        assert app.backend.api_key == "mm-key"
        await submit(pilot, "/model glm glm-5.2-air")
        assert app.backend.label == "glm" and app.backend.model == "glm-5.2-air"


async def test_model_command_lists_providers(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await submit(pilot, "/model")
        text = transcript_text(app)
        for name in ("glm", "minimax", "anthropic", "openai"):
            assert name in text
        assert "test-model" in text  # current backend shown


async def test_permissions_command_sets_mode_and_allowlist(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        assert app.gate.mode == "ask"  # safe default
        await submit(pilot, "/permissions auto")
        assert app.gate.mode == "auto"
        await submit(pilot, "/permissions allow run_shell")
        assert "run_shell" in app.gate.session_allowed
        await submit(pilot, "/permissions reset")
        assert app.gate.session_allowed == set()
        await submit(pilot, "/permissions readonly")
        assert app.gate.mode == "readonly"


async def test_plain_input_routes_to_operator(tmp_path):
    app = make_app(tmp_path)

    class StubOperator:
        def __init__(self):
            self.heard: list[str] = []

        async def chat(self, text):
            self.heard.append(text)
            return "hello from your rep"

        def reset(self):
            self.heard.clear()

    stub = StubOperator()
    app._operator = stub
    app.rep_backend = TEST_BACKEND  # skip lazy resolution
    async with app.run_test() as pilot:
        await submit(pilot, "what should we build?")
        for _ in range(20):  # let the chat task finish
            await pilot.pause()
            if not app._run_active():
                break
        text = transcript_text(app)
        assert stub.heard == ["what should we build?"]
        assert "you" in text and "what should we build?" in text
        assert "hello from your rep" in text


async def test_reset_command_resets_operator_session(tmp_path):
    app = make_app(tmp_path)

    class StubOperator:
        def __init__(self):
            self.resets = 0

        def reset(self):
            self.resets += 1

    stub = StubOperator()
    app._operator = stub
    async with app.run_test() as pilot:
        await submit(pilot, "/reset")
        assert stub.resets == 1
        assert "operator session reset" in transcript_text(app)


async def test_rep_command_shows_and_switches(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await submit(pilot, "/rep")
        assert "operator" in transcript_text(app)
        await submit(pilot, "/rep anthropic")
        assert app.rep_backend.label == "anthropic"
        assert app.rep_backend.model == "claude-opus-4-8"
        assert app._operator is None  # fresh session on brain change
        await submit(pilot, "/rep claude-haiku-4-5")
        assert app.rep_backend.model == "claude-haiku-4-5"


async def test_swarm_command_dispatches_directly(tmp_path, monkeypatch):
    for env in ALL_KEY_ENVS:
        monkeypatch.delenv(env, raising=False)
    app = CrucibleApp(workspace=str(tmp_path), splash=False)  # no backend
    async with app.run_test() as pilot:
        await submit(pilot, "/swarm build me a thing")
        # direct dispatch path hit its backend guard (no provider)
        assert "no provider configured" in transcript_text(app)
        await submit(pilot, "/swarm")
        assert "/swarm needs a task" in transcript_text(app)


async def test_task_without_any_provider_is_refused_gracefully(tmp_path, monkeypatch):
    for env in ALL_KEY_ENVS:
        monkeypatch.delenv(env, raising=False)
    app = CrucibleApp(workspace=str(tmp_path), splash=False)  # resolves to None
    async with app.run_test() as pilot:
        assert app.backend is None
        await submit(pilot, "do something big")
        assert "no provider configured" in transcript_text(app)
        assert app._run_task is None  # nothing launched


async def test_engine_events_render_and_update_plan_tree(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        app.post_message(EngineEvent("run_started", {"task": "build musa", "workspace": "w"}))
        app.post_message(EngineEvent("plan_ready", {
            "reasoning": "two steps", "subtask_count": 2,
            "layers": [["research_api"], ["code_impl"]],
        }))
        app.post_message(EngineEvent("subtask_started", {
            "subtask_id": "research_api", "role": "researcher", "task": "t", "dependency_ids": [],
        }))
        app.post_message(EngineEvent("llm_call", {"role": "researcher", "usage": 1234}))
        app.post_message(EngineEvent("tool_call", {"name": "read_file", "args": {"path": "a.py"}}))
        app.post_message(EngineEvent("subtask_finished", {
            "subtask_id": "research_api", "role": "researcher",
            "summary": "found it", "confidence": 0.92,
        }))
        app.post_message(EngineEvent("verify", {"attempt": 0, "passed": False,
                                                "output_preview": "1 failed"}))
        app.post_message(EngineEvent("run_finished", {
            "summary": "s", "confidence": 0.9, "iterations": 1, "tokens_total": 1234,
        }))
        await pilot.pause()

        text = transcript_text(app)
        assert "▶ run" in text and "build musa" in text
        assert "2 subtasks in 2 layers" in text
        assert "research_api" in text and "found it" in text
        assert "read_file" in text
        assert "FAIL" in text and "1 failed" in text
        assert "run finished" in text

        assert app.stats.total_tokens == 1234
        assert app.stats.tool_calls == 1
        assert set(app._plan_nodes) == {"research_api", "code_impl"}
        assert "●" in str(app._plan_nodes["research_api"].label)

        status = str(app.query_one("#statusbar", Static).render())
        assert "1.2k" in status


async def test_tokens_command_reports_utilization(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        app.post_message(EngineEvent("llm_call", {"role": "coder", "usage": 5000}))
        await pilot.pause()
        await submit(pilot, "/tokens")
        text = transcript_text(app)
        assert "token utilization" in text
        assert "5,000" in text
        assert "coder" in text


async def test_clear_command_empties_transcript(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await submit(pilot, "/clear")
        assert transcript_text(app) == ""


async def test_prompt_history_recall(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await submit(pilot, "/tokens")
        await submit(pilot, "/help")
        prompt = app.query_one("#prompt", PromptInput)
        await pilot.press("up")
        assert prompt.value == "/help"
        await pilot.press("up")
        assert prompt.value == "/tokens"
        await pilot.press("down")
        assert prompt.value == "/help"


async def test_multiline_paste_flattens_into_prompt(tmp_path):
    from textual import events

    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        prompt = app.query_one("#prompt", PromptInput)
        prompt.focus()
        prompt.post_message(events.Paste("build a parser\nwith tests\n\nand docs"))
        await pilot.pause()
        assert prompt.value == "build a parser with tests and docs"


async def test_copy_command_reply_report_all(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await submit(pilot, "/copy")
        assert "nothing to copy" in transcript_text(app)

        app._last_reply = "the rep said this"
        app._last_report = "verified green summary"
        await submit(pilot, "/copy")
        assert app.clipboard == "the rep said this"
        await submit(pilot, "/copy report")
        assert app.clipboard == "verified green summary"
        await submit(pilot, "/copy all")
        assert "the rep said this" not in app.clipboard  # /copy output is dim meta
        assert "copied transcript" in transcript_text(app)
        assert "C R U C I B L E" in app.clipboard  # banner is part of the transcript
        await submit(pilot, "/copy nonsense")
        assert "usage: /copy" in transcript_text(app)


async def test_transcript_click_drag_selection_resolves_text(tmp_path):
    """The reason Transcript exists: RichLog drags never resolved to text."""
    app = make_app(tmp_path)
    async with app.run_test(size=(110, 32)) as pilot:
        app.transcript.clear()
        app.transcript.write("grab this exact line of text")
        await pilot.pause()
        target = app.query_one("#transcript").children[0]
        assert target.allow_select is True
        await pilot.mouse_down(target, offset=(0, 0))
        await pilot.hover(target, offset=(9, 0))
        await pilot.mouse_up(target, offset=(9, 0))
        await pilot.pause()
        selected = app.screen.get_selected_text()
        assert selected and "grab this" in selected


async def test_transcript_handles_renderables_and_bad_markup(tmp_path):
    from rich.panel import Panel

    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        app.transcript.clear()
        app.transcript.write(Panel("panel body", title="report"))
        app.transcript.write("[unclosed markup [red")  # must not crash
        await pilot.pause()
        text = transcript_text(app)
        assert "panel body" in text
        assert "unclosed markup" in text


async def test_transcript_caps_entries(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        app.transcript.clear()
        app.transcript.MAX_ENTRIES = 10
        for i in range(25):
            app.transcript.write(f"line {i}")
        await pilot.pause()
        lines = app.transcript.plain_lines()
        assert len(lines) == 10
        assert lines[0] == "line 15" and lines[-1] == "line 24"


async def test_roots_command_manages_read_roots(tmp_path):
    app = make_app(tmp_path)
    sibling = tmp_path.parent / f"{tmp_path.name}-sibling"
    sibling.mkdir()
    async with app.run_test() as pilot:
        await submit(pilot, "/roots")
        assert "no read roots" in transcript_text(app)
        await submit(pilot, f"/roots add {sibling}")
        assert app.read_roots == [str(sibling)]
        await submit(pilot, f"/roots add {sibling}")  # dedup
        assert app.read_roots == [str(sibling)]
        await submit(pilot, "/roots add /nope/missing")
        assert "not a directory" in transcript_text(app)
        await submit(pilot, f"/roots rm {sibling}")
        assert app.read_roots == []
        await submit(pilot, f"/roots add {sibling}")
        await submit(pilot, "/roots clear")
        assert app.read_roots == []


async def test_workspace_command_validates_directory(tmp_path):
    app = make_app(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    async with app.run_test() as pilot:
        await submit(pilot, f"/workspace {other}")
        assert app.workspace == str(other)
        await submit(pilot, "/workspace /nope/definitely/missing")
        assert "not a directory" in transcript_text(app)
        assert app.workspace == str(other)
