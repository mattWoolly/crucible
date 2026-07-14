"""The Crucible full-screen TUI (Textual).

Layout (fills the terminal, Claude Code-style):

    ┌──────────────────────────────────────────────────────┐
    │ header: workspace · model · permission mode          │
    │ ┌────────────┬─────────────────────────────────────┐ │
    │ │ plan tree  │ transcript (engine events, reports) │ │
    │ └────────────┴─────────────────────────────────────┘ │
    │ ❯ input (tasks and /commands)                        │
    │ status: tokens · llm calls · tools · elapsed         │
    │ footer: key bindings                                 │
    └──────────────────────────────────────────────────────┘

The app talks to the engine ONLY through public seams: ``Observer`` events in
(via ``TUIObserver``) and ``Config.tool_gate`` out (via ``PermissionGate``).
The orchestrator runs as an asyncio task on the app's own event loop.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
from typing import Any

from rich.markup import escape
from rich.panel import Panel
from rich.style import Style
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Input, Static, Tree

from ..config import Config
from ..constants import DEFAULT_MAX_LLM_CALLS
from ..models import FinalReport
from ..observers import MultiObserver
from ..operator import Operator
from ..orchestrator import Orchestrator
from ..otel import create_otel_observer
from ..providers import (
    PROVIDER_ORDER,
    PROVIDERS,
    ResolvedProvider,
    api_key_for,
    make_client,
    resolve_from_env,
    resolve_provider,
)
from .commands import COMMANDS, CommandError, SlashCommand, parse_input
from .logo import LOGO_GRADIENT, LOGO_SMALL, TAGLINE, logo_markup
from .observer import TUIObserver
from .permissions import ALLOW_ALWAYS, ALLOW_ONCE, DENY, MODES, PermissionGate
from .stats import RunStats, fmt_tokens

_STATUS_GLYPH = {"pending": "○", "running": "◐", "done": "●", "failed": "✖"}


def _validated_markup(content: str) -> Text:
    """Markup -> Text, degrading to literal text on ANY defect. Rich ignores
    unknown span styles at print time but Textual's renderer raises — so every
    span style is validated eagerly with Style.parse (e.g. `[a|b|c]` parses as
    a tag yet fails as a style and would otherwise crash the app mid-render)."""
    try:
        text = Text.from_markup(content)
        for span in text.spans:
            if isinstance(span.style, str):
                Style.parse(span.style)
        return text
    except Exception:  # noqa: BLE001 - malformed markup is data, not a bug
        return Text(content)


class Transcript(VerticalScroll):
    """The output log, as a scrollable stack of Static lines.

    Exists because RichLog cannot do text selection: Textual's selection
    machinery extracts text only from widgets that render Text/Content
    (``Widget.get_selection``), and RichLog renders pre-baked strips — drags
    register but never resolve. One Static per write() gives real
    click-drag + ctrl+c selection with the same write/clear surface.
    """

    MAX_ENTRIES = 1500  # oldest lines drop past this — same spirit as RichLog max_lines

    DEFAULT_CSS = """
    Transcript > Static { width: 1fr; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._plain: list[str] = []  # parallel plain-text log for /copy all + tests

    @staticmethod
    def _prepare(content: Any) -> tuple[Any, str]:
        """(renderable, plain_text). Bad markup degrades to literal text
        rather than crashing the app at render time — including tags that
        PARSE but fail style resolution when rendered (e.g. `[a|b|c]` in
        usage strings), hence the probe render."""
        if isinstance(content, str):
            text = _validated_markup(content)
            return text, text.plain
        if isinstance(content, Text):
            return content, content.plain
        # Arbitrary Rich renderable (Panel, ...): render to plain via a capture.
        from io import StringIO

        from rich.console import Console

        console = Console(file=StringIO(), width=100)
        console.print(content)
        return content, console.file.getvalue().rstrip("\n")

    def write(self, content: Any = "") -> None:
        renderable, plain = self._prepare(content)
        at_end = self.scroll_offset.y >= self.max_scroll_y
        self._plain.append(plain)
        self.mount(Static(renderable))
        if len(self._plain) > self.MAX_ENTRIES:
            overflow = len(self._plain) - self.MAX_ENTRIES
            del self._plain[:overflow]
            for child in list(self.children)[:overflow]:
                child.remove()
        if at_end:  # stick to the bottom unless the user scrolled up
            self.call_after_refresh(self.scroll_end, animate=False)

    def clear(self) -> None:
        self._plain.clear()
        self.remove_children()

    def plain_lines(self) -> list[str]:
        return list(self._plain)


class EngineEvent(Message):
    """One engine Observer event, re-posted onto the Textual message bus."""

    def __init__(self, event: str, fields: dict[str, Any]) -> None:
        super().__init__()
        self.event = event
        self.fields = fields


class PromptInput(Input):
    """Input with shell-style up/down history and newline-safe paste.

    Textual's Input keeps only the first line of a bracketed paste; a chat
    prompt should take the whole thing — newlines flatten to spaces."""

    async def _on_paste(self, event: events.Paste) -> None:
        flat = " ".join(part.strip() for part in event.text.splitlines() if part.strip())
        if flat:
            self.insert_text_at_cursor(flat)
        event.stop()
        event.prevent_default()

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.history: list[str] = []
        self._cursor: int | None = None

    def remember(self, line: str) -> None:
        if line and (not self.history or self.history[-1] != line):
            self.history.append(line)
        self._cursor = None

    def _recall(self, delta: int) -> None:
        if not self.history:
            return
        if self._cursor is None:
            self._cursor = len(self.history)
        self._cursor = max(0, min(len(self.history), self._cursor + delta))
        self.value = "" if self._cursor == len(self.history) else self.history[self._cursor]
        self.cursor_position = len(self.value)

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "up":
            self._recall(-1)
            event.stop()
            return
        if event.key == "down":
            self._recall(+1)
            event.stop()
            return
        await super()._on_key(event)


class SplashScreen(Screen[None]):
    """Full-screen splash: the logo glitches in — lines jitter horizontally
    with decaying amplitude and the odd color-flicker frame, then snap into
    alignment. Dismissed by any key (or a short timer)."""

    GLITCH_FPS = 20
    GLITCH_SECONDS = 1.2
    GLITCH_AMPLITUDE = 4  # max horizontal shift (chars) at t=0
    FLICKER_COLORS = ("#7fdbff", "#ffffff", "#ffe08a")

    DEFAULT_CSS = """
    SplashScreen { align: center middle; }
    SplashScreen > Static { width: auto; text-align: center; }
    #splash-tagline { color: $text-muted; margin-top: 1; }
    #splash-hint { color: $text-disabled; margin-top: 2; }
    """

    def compose(self) -> ComposeResult:
        yield Static(logo_markup(self.app.size.width), id="splash-logo")
        yield Static(TAGLINE, id="splash-tagline")
        yield Static("press any key", id="splash-hint")

    def on_mount(self) -> None:
        self._elapsed = 0.0
        self._settled = False
        self._glitch_timer = self.set_interval(1 / self.GLITCH_FPS, self._glitch_tick)
        self.set_timer(3.0, self._leave)

    def _glitch_tick(self) -> None:
        logo = self.query_one("#splash-logo", Static)
        self._elapsed += 1 / self.GLITCH_FPS
        t = self._elapsed / self.GLITCH_SECONDS
        if t >= 1.0:  # settle: perfectly aligned, full gradient, done
            logo.update(logo_markup(self.app.size.width))
            self._settled = True
            self._glitch_timer.stop()
            return
        amp = max(1, round(self.GLITCH_AMPLITUDE * (1.0 - t)))
        offsets = [random.randint(-amp, amp) for _ in range(len(LOGO_GRADIENT))]
        colors: dict[int, str] = {}
        if random.random() < 0.35:  # occasional corrupted-scanline frame
            colors[random.randrange(len(LOGO_GRADIENT))] = random.choice(self.FLICKER_COLORS)
        logo.update(logo_markup(self.app.size.width, offsets=offsets, colors=colors))

    def on_key(self, event: events.Key) -> None:
        event.stop()
        self._leave()

    def _leave(self) -> None:
        if self.is_current:
            self.dismiss(None)


class PermissionModal(ModalScreen[str]):
    """Ask the user to approve one mutating tool call."""

    BINDINGS = [
        Binding("y", "answer('allow')", "allow once"),
        Binding("a", "answer('always')", "always allow"),
        Binding("n,escape", "answer('deny')", "deny"),
    ]

    DEFAULT_CSS = """
    PermissionModal { align: center middle; }
    #permission-box {
        width: 70%; max-width: 90; height: auto; max-height: 80%;
        border: heavy $warning; background: $surface; padding: 1 2;
    }
    #permission-title { text-style: bold; color: $warning; }
    #permission-detail { margin: 1 0; overflow-y: auto; }
    #permission-buttons { height: auto; align-horizontal: center; }
    #permission-buttons Button { margin: 0 1; }
    """

    def __init__(self, tool: str, args: dict) -> None:
        super().__init__()
        self.tool = tool
        self.args = args

    def compose(self) -> ComposeResult:
        detail = "\n".join(
            f"{k}: {str(v)[:400]}" for k, v in self.args.items()
        ) or "(no arguments)"
        with Vertical(id="permission-box"):
            yield Static(f"⚠ permission: {self.tool}", id="permission-title")
            yield Static(detail, id="permission-detail")
            with Horizontal(id="permission-buttons"):
                yield Button("Allow once (y)", variant="success", id="allow")
                yield Button(f"Always {self.tool} (a)", variant="warning", id="always")
                yield Button("Deny (n)", variant="error", id="deny")

    def action_answer(self, answer: str) -> None:
        self.dismiss(answer)

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(str(event.button.id))


class CrucibleApp(App[None]):
    TITLE = "crucible"

    BINDINGS = [
        Binding("ctrl+q", "quit", "quit"),
        Binding("escape", "cancel_run", "cancel run", show=True),
        Binding("ctrl+l", "clear_transcript", "clear"),
    ]

    CSS = """
    #headerbar { height: 1; background: $panel; color: $text-muted; padding: 0 1; }
    #body { height: 1fr; }
    #plan-tree { width: 32; min-width: 24; border-right: solid $panel; }
    #transcript { width: 1fr; padding: 0 1; }
    #prompt { border: round $primary; }
    #statusbar { height: 1; background: $panel; color: $text-muted; padding: 0 1; }
    """

    def __init__(
        self,
        workspace: str = ".",
        backend: ResolvedProvider | None = None,
        theme: str | None = None,
        splash: bool = True,
    ) -> None:
        super().__init__()
        self.workspace = os.path.abspath(workspace)
        self.backend = backend or resolve_from_env()
        self.rep_backend: ResolvedProvider | None = None  # resolved lazily
        self._operator: Operator | None = None
        # Read-only dirs agents may access outside the workspace (/roots).
        # ONE list shared with the operator so /roots applies mid-session.
        self.read_roots: list[str] = []
        # /copy sources: the last operator reply and the last final report.
        self._last_reply: str = ""
        self._last_report: str = ""
        # OTel telemetry (WI-34): one observer per process, chained behind the
        # UI observer; None unless CRUCIBLE_OTEL=1.
        self._otel = create_otel_observer(
            provider_label=self.backend.label if self.backend else "unknown"
        )
        self.gate = PermissionGate(asker=self._ask_permission, mode="ask")
        self.stats = RunStats()
        self.approval_threshold: float | None = None
        self._run_task: asyncio.Task | None = None
        self._plan_nodes: dict[str, Any] = {}
        self._show_splash = splash
        self._start_theme = theme

    # -- layout --------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static(id="headerbar")
        with Horizontal(id="body"):
            yield Tree("plan", id="plan-tree")
            yield Transcript(id="transcript")
        yield PromptInput(placeholder="chat with your operator — /swarm <task> · /help", id="prompt")
        yield Static(id="statusbar")
        yield Footer()

    def on_unmount(self) -> None:
        if self._otel is not None:
            self._otel.close()  # flush the last export interval on exit

    def on_mount(self) -> None:
        if self._start_theme:
            try:
                self.theme = self._start_theme
            except Exception:
                self.transcript.write(f"[red]unknown theme {self._start_theme!r}[/]")
        self.query_one("#plan-tree", Tree).root.expand()
        self._refresh_header()
        self._refresh_status()
        self.set_interval(1.0, self._refresh_status)
        self._banner()
        self.query_one("#prompt", PromptInput).focus()
        if self._show_splash:
            self.push_screen(SplashScreen())

    # -- small helpers ---------------------------------------------------------
    @property
    def transcript(self) -> Transcript:
        return self.query_one("#transcript", Transcript)

    def _banner(self) -> None:
        t = self.transcript
        t.write(f"[bold #ff8c2e]{LOGO_SMALL}[/]  [dim]{TAGLINE}[/]")
        t.write(f"[dim]workspace[/] {self.workspace}")
        if self.backend is not None:
            t.write(
                f"[dim]provider[/] {self.backend.label} "
                f"[dim]model[/] {self.backend.model} "
                f"[dim]endpoint[/] {self.backend.base_url or '(native)'}"
            )
        else:
            t.write(
                "[yellow]no provider configured — export one of "
                "GLM_API_KEY / MINIMAX_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY "
                "(or LLM_API_KEY + LLM_BASE_URL), then /model lists what's found.[/]"
            )
        if self._otel is not None:
            t.write("[dim]otel[/] [green]on[/] [dim]→ metrics + traces to the local collector[/]")
        t.write("[dim]type to chat with your operator (it dispatches the swarm) · "
                "/swarm <task> dispatches directly · /help for commands[/]")
        t.write("")

    def _refresh_header(self) -> None:
        mode_color = {"auto": "green", "ask": "yellow", "readonly": "red"}[self.gate.mode]
        backend = (
            f"{self.backend.label}:{self.backend.model}" if self.backend else "no provider"
        )
        self.query_one("#headerbar", Static).update(
            f"[bold #ff8c2e]◆ crucible[/]  "
            f"[dim]{self.workspace}[/]  ·  {backend}"
            f"  ·  [{mode_color}]{self.gate.mode}[/]"
            + (f"  ·  threshold {self.approval_threshold:g}" if self.approval_threshold else "")
        )

    def _refresh_status(self) -> None:
        running = self._run_task is not None and not self._run_task.done()
        state = "[green]⚡ running[/]" if running else "[dim]idle[/]"
        line = self.stats.status_line(DEFAULT_MAX_LLM_CALLS, None)
        self.query_one("#statusbar", Static).update(f"{state}  ·  {line}  ·  [dim]{self.theme}[/]")

    # -- permission gate UI ----------------------------------------------------
    def _ask_permission(self, tool: str, args: dict) -> "asyncio.Future[str]":
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        def settle(answer: str | None) -> None:
            if not fut.done():
                fut.set_result(
                    {"allow": ALLOW_ONCE, "always": ALLOW_ALWAYS}.get(answer or "", DENY)
                )

        self.push_screen(PermissionModal(tool, args), settle)
        return fut

    # -- input ------------------------------------------------------------------
    @on(Input.Submitted, "#prompt")
    def _submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        prompt = self.query_one("#prompt", PromptInput)
        prompt.value = ""
        if not text:
            return
        prompt.remember(text)
        try:
            cmd = parse_input(text)
        except CommandError as e:
            self.transcript.write(f"[red]{e}[/]")
            return
        if cmd is None:
            self._chat(text)  # plain input goes to the operator (your rep)
        else:
            self._dispatch(cmd)

    # -- slash commands -----------------------------------------------------------
    def _dispatch(self, cmd: SlashCommand) -> None:
        t = self.transcript
        if cmd.name == "help":
            t.write("[bold]commands[/]")
            width = max(len(usage) for usage, _ in COMMANDS.values())
            for usage, desc in COMMANDS.values():
                pad = " " * (width - len(usage))
                t.write(f"  [cyan]{escape(usage)}[/]{pad}  [dim]{escape(desc)}[/]")
        elif cmd.name == "quit":
            self.exit()
        elif cmd.name == "clear":
            self.action_clear_transcript()
        elif cmd.name == "theme":
            if not cmd.args:
                current = self.theme
                names = ", ".join(sorted(self.available_themes))
                t.write(f"theme: [bold]{current}[/]\navailable: {names}")
            else:
                try:
                    self.theme = cmd.args[0]
                    t.write(f"theme → [bold]{cmd.args[0]}[/]")
                except Exception:
                    t.write(f"[red]unknown theme {cmd.args[0]!r} — /theme lists them[/]")
        elif cmd.name == "model":
            self._cmd_model(cmd.args)
        elif cmd.name == "rep":
            self._cmd_rep(cmd.args)
        elif cmd.name == "swarm":
            if not cmd.args:
                t.write("[red]/swarm needs a task, e.g. /swarm migrate tokens v1 -> v2[/]")
            else:
                self._launch(" ".join(cmd.args))
        elif cmd.name == "copy":
            self._cmd_copy(cmd.args)
        elif cmd.name == "roots":
            self._cmd_roots(cmd.args)
        elif cmd.name == "reset":
            if self._operator is not None:
                self._operator.reset()
            t.write("[dim]operator session reset — fresh conversation[/]")
        elif cmd.name == "permissions":
            self._cmd_permissions(cmd.args)
        elif cmd.name == "tokens":
            t.write("[bold]token utilization[/]")
            for line in self.stats.detail_lines(DEFAULT_MAX_LLM_CALLS, None):
                t.write(f"  {line}")
        elif cmd.name == "plan":
            if not self._plan_nodes:
                t.write("[dim]no plan yet — run a task first[/]")
            else:
                for sid, node in self._plan_nodes.items():
                    t.write(f"  {node.label}")
        elif cmd.name == "workspace":
            if not cmd.args:
                t.write(f"workspace: [bold]{self.workspace}[/]")
            elif self._run_active():
                t.write("[red]cannot change workspace while a run is active[/]")
            else:
                path = os.path.abspath(cmd.args[0])
                if not os.path.isdir(path):
                    t.write(f"[red]not a directory: {path}[/]")
                else:
                    self.workspace = path
                    self._operator = None  # workspace is baked into the session
                    t.write(f"workspace → [bold]{path}[/] [dim](operator session reset)[/]")
        elif cmd.name == "threshold":
            if not cmd.args:
                cur = self.approval_threshold
                t.write(f"approval threshold: [bold]{cur if cur is not None else 'default (8.0)'}[/]")
            else:
                try:
                    val = float(cmd.args[0])
                    if not 0 <= val <= 10:
                        raise ValueError
                except ValueError:
                    t.write("[red]threshold must be a number 0-10[/]")
                    return
                self.approval_threshold = val
                t.write(f"approval threshold → [bold]{val:g}[/]")
        self._refresh_header()

    def _cmd_model(self, args: list[str]) -> None:
        t = self.transcript
        if not args:
            if self.backend is not None:
                t.write(
                    f"provider: [bold]{self.backend.label}[/]  "
                    f"model: [bold]{self.backend.model}[/]  "
                    f"endpoint: {self.backend.base_url or '(native)'}"
                )
            else:
                t.write("[yellow]no provider configured[/]")
            t.write("[bold]providers[/] [dim](✓ = API key found)[/]")
            for name in PROVIDER_ORDER:
                p = PROVIDERS[name]
                mark = "[green]✓[/]" if api_key_for(p) else "[red]✗[/]"
                t.write(
                    f"  {mark} [cyan]{name:<9}[/] {p.default_model or '(model required)':<18} "
                    f"[dim]{p.api_key_envs[0]} — {p.notes}[/]"
                )
            t.write(f"[dim]{escape('/model <provider> [model]')} switches provider · "
                    "/model <name> sets the model on the current provider[/]")
            return
        head = args[0].lower()
        if head in PROVIDERS:
            try:
                self.backend = resolve_provider(head, args[1] if len(args) > 1 else None)
            except ValueError as e:
                t.write(f"[red]{e}[/]")
                return
            key_note = "" if self.backend.api_key else (
                " [yellow](no API key found — anthropic may still resolve an "
                "`ant auth login` profile)[/]" if head == "anthropic"
                else f" [red](set {PROVIDERS[head].api_key_envs[0]} first)[/]"
            )
            t.write(f"provider → [bold]{head}[/] model [bold]{self.backend.model}[/]{key_note}")
        elif self.backend is not None:
            self.backend = ResolvedProvider(
                provider=self.backend.provider,
                api_key=self.backend.api_key,
                base_url=self.backend.base_url,
                model=args[0],
            )
            t.write(f"model → [bold]{args[0]}[/] (provider {self.backend.label}, next run)")
        else:
            t.write("[red]no provider configured — /model <provider> first[/]")

    def _transcript_text(self) -> str:
        return "\n".join(self.transcript.plain_lines())

    def _cmd_copy(self, args: list[str]) -> None:
        t = self.transcript
        what = (args[0].lower() if args else "reply")
        sources = {
            "reply": (self._last_reply, "last rep reply"),
            "report": (self._last_report, "last final report"),
            "all": (self._transcript_text(), "transcript"),
        }
        if what not in sources:
            t.write(f"[red]usage: {escape('/copy [reply|report|all]')}[/]")
            return
        text, label = sources[what]
        if not text:
            t.write(f"[yellow]nothing to copy — no {label} yet[/]")
            return
        self.copy_to_clipboard(text)
        t.write(f"[dim]⧉ copied {label} ({len(text)} chars) — "
                "clipboard via OSC 52; drag-select + ctrl+c also works[/]")

    def _cmd_roots(self, args: list[str]) -> None:
        t = self.transcript
        if not args:
            if self.read_roots:
                t.write("[bold]read roots[/] [dim](read-only, outside the workspace)[/]")
                for r in self.read_roots:
                    t.write(f"  {r}")
            else:
                t.write("[dim]no read roots — agents are confined to the workspace[/]")
            t.write("[dim]/roots add <dir> · /roots rm <dir> · /roots clear — "
                    "writes/shell always stay inside the workspace[/]")
            return
        head = args[0].lower()
        if head == "add" and len(args) > 1:
            path = os.path.abspath(os.path.join(self.workspace, args[1]))
            if not os.path.isdir(path):
                t.write(f"[red]not a directory: {path}[/]")
            elif path in self.read_roots:
                t.write(f"[dim]already a read root: {path}[/]")
            else:
                self.read_roots.append(path)
                t.write(f"read root + [bold]{path}[/] [dim](read-only)[/]")
        elif head == "rm" and len(args) > 1:
            path = os.path.abspath(os.path.join(self.workspace, args[1]))
            if path in self.read_roots:
                self.read_roots.remove(path)
                t.write(f"read root − [bold]{path}[/]")
            else:
                t.write(f"[red]not a read root: {path}[/]")
        elif head == "clear":
            self.read_roots.clear()
            t.write("read roots cleared — workspace-only again")
        else:
            t.write(f"[red]usage: {escape('/roots [add <dir> | rm <dir> | clear]')}[/]")

    def _cmd_permissions(self, args: list[str]) -> None:
        t = self.transcript
        if not args:
            allowed = ", ".join(sorted(self.gate.session_allowed)) or "(none)"
            t.write(
                f"permission mode: [bold]{self.gate.mode}[/]\n"
                f"  auto      run every tool call\n"
                f"  ask       prompt for write_file / run_shell\n"
                f"  readonly  deny mutating tools outright\n"
                f"session always-allowed: {allowed}\n"
                "[dim]/permissions <mode> · /permissions allow <tool> · /permissions reset[/]"
            )
            return
        head = args[0].lower()
        if head in MODES:
            self.gate.mode = head
            t.write(f"permission mode → [bold]{head}[/]")
        elif head == "allow" and len(args) > 1:
            self.gate.allow_always(args[1])
            t.write(f"[bold]{args[1]}[/] always allowed for this session")
        elif head == "reset":
            self.gate.reset()
            t.write("session allowlist cleared")
        else:
            t.write(f"[red]unknown permissions argument {args[0]!r}[/]")

    # -- running the engine -----------------------------------------------------
    def _run_active(self) -> bool:
        return self._run_task is not None and not self._run_task.done()

    def _backend_error(self, backend: ResolvedProvider | None) -> str | None:
        """None when the backend is usable; else the transcript error line."""
        if backend is None:
            return (
                "no provider configured. export GLM_API_KEY / MINIMAX_API_KEY / "
                "ANTHROPIC_API_KEY / OPENAI_API_KEY (or LLM_API_KEY) and restart — "
                "/model lists what's detected."
            )
        # anthropic runs without an env key: the SDK can resolve an OAuth profile.
        if backend.api_key is None and (
            backend.provider is None or backend.provider.kind != "anthropic"
        ):
            env = backend.provider.api_key_envs[0] if backend.provider else "LLM_API_KEY"
            return f"{backend.label}: API key missing — export {env}"
        return None

    def _observer(self) -> Any:
        ui = TUIObserver(lambda ev, f: self.post_message(EngineEvent(ev, f)))
        if self._otel is None:
            return ui
        return MultiObserver([ui, self._otel])

    def _launch(self, task: str) -> None:
        if self._run_active():
            self.transcript.write("[yellow]a run is already active — esc cancels it[/]")
            return
        err = self._backend_error(self.backend)
        if err:
            self.transcript.write(f"[red]{err}[/]")
            return
        self._reset_plan_tree()
        self._run_task = asyncio.create_task(self._execute(task))
        self._refresh_status()

    def _swarm_config(self) -> Config:
        overrides: dict[str, Any] = {
            "observer": self._observer(),
            "tool_gate": self.gate.check,
            "read_roots": tuple(self.read_roots),
        }
        if self.approval_threshold is not None:
            overrides["approval_threshold"] = self.approval_threshold
        return Config.from_env(workspace=self.workspace, **overrides)

    async def _execute(self, task: str) -> None:
        llm = make_client(self.backend)
        config = self._swarm_config()
        try:
            report = await Orchestrator(llm, config).run(task)
        except asyncio.CancelledError:
            self.transcript.write("[yellow]✋ run cancelled[/]")
            return
        except Exception as e:  # provider/config errors surface here
            self.transcript.write(f"[red]run failed: {e}[/]")
            return
        finally:
            self._refresh_status()
        verified = {True: "[green]verified ✔[/]", False: "[red]verify FAILED[/]", None: ""}[report.verified]
        self._last_report = report.summary.strip()
        body = Text(report.summary.strip() or "(no summary)")
        self.transcript.write(
            Panel(
                body,
                title=f"final report · confidence {report.confidence:.2f} · "
                      f"{fmt_tokens(report.tokens_total)} tokens {verified}",
                border_style="green" if report.verified is not False else "red",
            )
        )

    # -- the operator (chat) ------------------------------------------------------
    def _default_rep_backend(self) -> ResolvedProvider | None:
        """The rep gets the big brain when one is available: anthropic if a
        key/profile is plausible, else whatever the swarm uses."""
        if self.backend is not None and self.backend.provider is not None \
                and self.backend.provider.kind == "anthropic":
            return self.backend
        if api_key_for(PROVIDERS["anthropic"]):
            return resolve_provider("anthropic")
        return self.backend

    async def _run_swarm(self, task: str) -> FinalReport:
        """The operator's run_swarm tool: same engine path as /swarm."""
        err = self._backend_error(self.backend)
        if err:
            raise RuntimeError(err)
        self._reset_plan_tree()
        llm = make_client(self.backend)
        report = await Orchestrator(llm, self._swarm_config()).run(task)
        self._last_report = report.summary.strip()
        return report

    def _ensure_operator(self) -> Operator | None:
        if self.rep_backend is None:
            self.rep_backend = self._default_rep_backend()
        err = self._backend_error(self.rep_backend)
        if err:
            self.transcript.write(f"[red]{err}[/]")
            return None
        if self._operator is None:
            self._operator = Operator(
                llm=make_client(self.rep_backend),
                workspace=self.workspace,
                swarm_runner=self._run_swarm,
                tool_gate=self.gate.check,
                observer=self._observer(),
                read_roots=self.read_roots,  # shared list — /roots edits apply live
                on_dispatch=lambda brief: self.transcript.write(
                    f"[bold #ff8c2e]⇶ dispatching swarm[/] [dim]{escape(brief[:200])}…[/]"
                    if len(brief) > 200 else
                    f"[bold #ff8c2e]⇶ dispatching swarm[/] [dim]{escape(brief)}[/]"
                ),
            )
        return self._operator

    def _chat(self, text: str) -> None:
        if self._run_active():
            self.transcript.write("[yellow]busy — esc cancels the current turn[/]")
            return
        operator = self._ensure_operator()
        if operator is None:
            return
        self.transcript.write(f"[bold cyan]you[/]  {escape(text)}")
        self._run_task = asyncio.create_task(self._chat_turn(operator, text))
        self._refresh_status()

    async def _chat_turn(self, operator: Operator, text: str) -> None:
        try:
            reply = await operator.chat(text)
        except asyncio.CancelledError:
            self.transcript.write("[yellow]✋ turn cancelled[/]")
            return
        except Exception as e:  # noqa: BLE001 - surfaced to the transcript
            self.transcript.write(f"[red]operator failed: {e}[/]")
            return
        finally:
            self._refresh_status()
        self._last_reply = reply
        self.transcript.write(f"[bold #ff8c2e]◆ rep[/]  {escape(reply)}")
        self.transcript.write("")

    def _cmd_rep(self, args: list[str]) -> None:
        t = self.transcript
        if not args:
            if self.rep_backend is None:
                self.rep_backend = self._default_rep_backend()
            if self.rep_backend is not None:
                t.write(
                    f"operator: [bold]{self.rep_backend.label}[/] "
                    f"model [bold]{self.rep_backend.model}[/]"
                )
            else:
                t.write("[yellow]no provider available for the operator[/]")
            t.write("[dim]plain input chats with the operator; it composes briefs and "
                    f"dispatches the swarm (/model) via run_swarm — "
                    f"{escape('/rep <provider> [model]')} changes its brain (fresh session)[/]")
            return
        head = args[0].lower()
        if head in PROVIDERS:
            try:
                self.rep_backend = resolve_provider(head, args[1] if len(args) > 1 else None)
            except ValueError as e:
                t.write(f"[red]{e}[/]")
                return
            self._operator = None  # new brain, fresh session
            t.write(f"operator → [bold]{head}[/] model [bold]{self.rep_backend.model}[/] "
                    "[dim](fresh session)[/]")
        elif self.rep_backend is not None:
            self.rep_backend = ResolvedProvider(
                provider=self.rep_backend.provider,
                api_key=self.rep_backend.api_key,
                base_url=self.rep_backend.base_url,
                model=args[0],
            )
            self._operator = None
            t.write(f"operator model → [bold]{args[0]}[/] [dim](fresh session)[/]")
        else:
            t.write("[red]no operator provider — /rep <provider> first[/]")

    def action_cancel_run(self) -> None:
        if self._run_active():
            self._run_task.cancel()

    def action_clear_transcript(self) -> None:
        self.transcript.clear()

    # -- engine events ------------------------------------------------------------
    def _reset_plan_tree(self) -> None:
        tree = self.query_one("#plan-tree", Tree)
        tree.root.remove_children()
        self._plan_nodes.clear()

    def on_engine_event(self, msg: EngineEvent) -> None:
        self.stats.note_event(msg.event, msg.fields)
        handler = getattr(self, f"_ev_{msg.event}", None)
        if handler is not None:
            handler(msg.fields)
        self._refresh_status()

    def _ev_run_started(self, f: dict) -> None:
        self.transcript.write(f"[bold]▶ run[/] {f.get('task', '')}")

    def _ev_plan_ready(self, f: dict) -> None:
        tree = self.query_one("#plan-tree", Tree)
        layers = f.get("layers") or []
        for i, layer in enumerate(layers, 1):
            branch = tree.root.add(f"layer {i}", expand=True)
            for sid in layer:
                self._plan_nodes[sid] = branch.add_leaf(f"○ {sid}")
        self.transcript.write(
            f"[cyan]◇ plan[/] {f.get('subtask_count', '?')} subtasks in {len(layers)} layers"
            + (f" — [dim]{f.get('reasoning', '')}[/]" if f.get("reasoning") else "")
        )

    def _set_node(self, sid: str, glyph: str, suffix: str = "") -> None:
        node = self._plan_nodes.get(sid)
        if node is not None:
            node.set_label(f"{glyph} {sid}{suffix}")

    def _ev_subtask_started(self, f: dict) -> None:
        sid = f.get("subtask_id", "?")
        if sid not in self._plan_nodes:  # repair subtasks appear mid-run
            tree = self.query_one("#plan-tree", Tree)
            self._plan_nodes[sid] = tree.root.add_leaf(f"○ {sid}")
        self._set_node(sid, _STATUS_GLYPH["running"])
        self.transcript.write(f"[blue]● {f.get('role', '?')}[/] [bold]{sid}[/] started")

    def _ev_llm_call(self, f: dict) -> None:
        pass  # counted in stats; per-call lines would flood the transcript

    def _ev_tool_call(self, f: dict) -> None:
        name, args = f.get("name", "?"), f.get("args") or {}
        arg = str(args.get("command") or args.get("path") or "")[:80]
        err = f.get("error")
        if err:
            self.transcript.write(f"  [red]⚒ {name}[/] {arg} [red]{str(err)[:120]}[/]")
        else:
            self.transcript.write(f"  [dim]⚒ {name} {arg}[/]")

    def _ev_critic_score(self, f: dict) -> None:
        sid = f.get("subtask_id", "?")
        score = f.get("score", "?")
        verdict = f.get("verdict", "")
        self.transcript.write(f"  [magenta]⚖ critic[/] {sid} → {score} {verdict}")

    def _ev_subtask_finished(self, f: dict) -> None:
        sid = f.get("subtask_id", "?")
        conf = f.get("confidence")
        glyph = _STATUS_GLYPH["failed" if (conf or 0) <= 0.1 else "done"]
        self._set_node(sid, glyph, f" [{conf:.2f}]" if isinstance(conf, float) else "")
        summary = str(f.get("summary", ""))[:160]
        self.transcript.write(f"[green]✔[/] [bold]{sid}[/] {summary}")

    def _ev_verify(self, f: dict) -> None:
        passed = f.get("passed")
        mark = "[green]PASS[/]" if passed else "[red]FAIL[/]"
        self.transcript.write(f"[bold]⛭ verify[/] attempt {f.get('attempt', '?')}: {mark}")
        if not passed and f.get("output_preview"):
            tail = str(f["output_preview"])[-400:]
            self.transcript.write(f"[dim]{tail}[/]")

    def _ev_run_finished(self, f: dict) -> None:
        self.transcript.write(
            f"[bold]■ run finished[/] · confidence {f.get('confidence', '?')} · "
            f"{fmt_tokens(int(f.get('tokens_total') or 0))} tokens · "
            f"{f.get('iterations', 0)} critic iterations"
        )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="crucible", description="Crucible — full-screen TUI")
    p.add_argument("--workspace", "-w", default=".",
                   help="project directory the agents operate in (default: cwd)")
    p.add_argument("--theme", default=None, help="start with this Textual theme")
    p.add_argument("--no-splash", action="store_true", help="skip the splash screen")
    args = p.parse_args(argv)
    app = CrucibleApp(workspace=args.workspace, theme=args.theme, splash=not args.no_splash)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
