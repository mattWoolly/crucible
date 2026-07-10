"""Sandboxed shell tool (SPEC §7 shell tool, §7.3).

Layered guards, in order of importance:
  1. Run without an intervening shell: ``subprocess.run`` with a tokenized
     argv and ``shell=False``. Metacharacters become inert literals.
  2. Reject at the source: refuse commands with shell metacharacters that appear
     OUTSIDE quotes — i.e. real operator soup (``a && b``, ``x | y``, ``2>f``),
     defense in depth on top of 1. Metacharacters INSIDE quotes are literal data
     (``git commit -m "add (v1); done"``, ``grep "foo(bar)"``) and are allowed —
     blocking them punished legitimate quoted arguments and taught nothing (they
     are already inert under 1).
  3. Block path traversal: reject ``..`` components, ``~`` prefixes, absolutes.
  4. Allowlist the first token.
  5. Argument-level guards on exec-capable allowlisted commands
     (``find -exec``/``-execdir``/``-ok``, ``git -c``/``--exec-path``,
     bare ``xargs``/``env`` launching a non-allowlisted program).
  6. Deny-pattern list for specific known-bad commands.
  7. Timeout + output cap.

§7.3 — HONEST THREAT MODEL: this is a speed bump, not a security boundary. A
coder with write_file + an allowlisted ``python`` has full code execution
(``python wrote_this.py``). The job here is to stop accidental/low-effort
escapes and keep logs legible. The real boundary is the container (§7.3).
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass

from ..constants import SHELL_OUTPUT_CAP, SHELL_TIMEOUT_S

# (2) Shell metacharacters refused before tokenization — but only when they
# appear UNQUOTED (see _unquoted_metachars). Quoted occurrences are literal.
_METACHARS = set(";&|<>$(){}`")


def _unquoted_metachars(command: str) -> str:
    """Return the sorted metacharacters that appear OUTSIDE single/double quotes.

    A tiny quote-tracking scanner: characters inside matched quotes are literal
    data and ignored; a backslash escapes the next char except inside single
    quotes (shell semantics). Unbalanced quotes leave trailing content treated as
    quoted here — the later ``shlex.split`` rejects those with a tokenize error,
    so nothing slips through. Good enough for a §7.3 speed bump, not a parser.
    """
    found: set[str] = set()
    quote: str | None = None
    escaped = False
    for ch in command:
        if escaped:
            escaped = False
            continue
        if ch == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch in _METACHARS:
            found.add(ch)
    return "".join(sorted(found))

# (4) First-token allowlist (~30 commands).
# `uv`/`ruff` are here so coders can run the project's own checks
# (`uv run ruff check .`, `uv run pytest -q`) and self-verify their fixes
# instead of patching blind — the biggest convergence lever we found. This
# adds no capability beyond the already-allowlisted `python`/`pip` code-exec
# hatches (§7.3: speed bump, not a boundary).
ALLOWLIST: frozenset[str] = frozenset({
    "python", "python3", "pytest", "pip", "pip3", "uv", "ruff",
    "git", "ls", "cat", "echo", "pwd", "head", "tail", "wc",
    "grep", "find", "sort", "uniq", "diff", "sed", "awk",
    "mkdir", "touch", "mv", "cp", "rm", "node", "npm",
    "make", "true", "false", "which", "test", "env", "xargs",
})

# (5) Commands that are arbitrary-code vectors by design.
_FIND_BAD_FLAGS = {"-exec", "-execdir", "-ok", "-okdir", "-fprintf"}
_GIT_BAD_FLAGS = {"-c", "--exec-path"}

# (6) Deny patterns matched against the normalized command string / tokens.
_DENY_SUBSTRINGS = (
    "rm -rf /",
    "sudo",
    ":(){",          # fork bomb
    "/etc/passwd",
    "/etc/shadow",
    "chmod 777",
    "mkfs",
    "dd if=",
)
# Interpreter -c (inline code) is denied even though the interpreter is
# allowlisted: it is a direct code-exec hatch the argv guards must close.
_DENY_INTERP_INLINE = {"python", "python3", "node", "bash", "sh", "perl", "ruby"}


@dataclass
class ShellResult:
    ok: bool
    output: str

    def __str__(self) -> str:  # what the worker loop sees
        return self.output


def _reject(reason: str) -> ShellResult:
    return ShellResult(ok=False, output=f"ERROR: refused — {reason}")


def _has_traversal(arg: str) -> bool:
    if arg.startswith("~"):
        return True
    if arg.startswith("/"):
        return True
    parts = arg.replace("\\", "/").split("/")
    return ".." in parts


def _check_argument_guards(argv: list[str]) -> str | None:
    """Return a rejection reason for exec-via-allowlisted-command, else None."""
    cmd = argv[0]
    rest = argv[1:]

    if cmd == "find":
        for tok in rest:
            if tok in _FIND_BAD_FLAGS:
                return f"find {tok} runs arbitrary binaries"

    if cmd == "git":
        for tok in rest:
            if tok in _GIT_BAD_FLAGS or tok.startswith("--exec-path"):
                return f"git {tok} can execute arbitrary code"

    if cmd == "env":
        # First non VAR=VAL token is the program env will launch.
        for tok in rest:
            if "=" in tok and not tok.startswith("="):
                continue
            if tok.startswith("-"):
                continue
            if tok not in ALLOWLIST:
                return f"env launches non-allowlisted program {tok!r}"
            break

    if cmd == "xargs":
        for tok in rest:
            if tok.startswith("-"):
                continue
            if tok not in ALLOWLIST:
                return f"xargs launches non-allowlisted program {tok!r}"
            break

    # Interpreter inline-code hatch (python -c, node -e, bash -c, ...).
    if cmd in _DENY_INTERP_INLINE:
        for tok in rest:
            if tok in ("-c", "-e") or tok.startswith("-c") or tok.startswith("-e"):
                return f"{cmd} {tok} executes inline code"

    # Same hatch one level down: `uv run <interp> -c/-e ...`. Now that quoted
    # metacharacters are allowed, `uv run python -c "..."` would otherwise slip
    # past every guard (argv[0] is 'uv', not the interpreter). Keep inline code
    # consistently denied — write a script and run it instead.
    if cmd == "uv":
        interp: str | None = None
        for tok in rest:
            if interp is None:
                if tok in _DENY_INTERP_INLINE:
                    interp = tok
                continue
            if tok in ("-c", "-e") or tok.startswith("-c") or tok.startswith("-e"):
                return f"uv run {interp} {tok} executes inline code"

    return None


def run_shell(
    workspace: str,
    command: str,
    timeout_s: int = SHELL_TIMEOUT_S,
    output_cap: int = SHELL_OUTPUT_CAP,
) -> ShellResult:
    if not command or not command.strip():
        return _reject("empty command")

    # (6 partial) cheap substring deny on the raw string.
    lowered = command.lower()
    for bad in _DENY_SUBSTRINGS:
        if bad in lowered:
            return _reject(f"deny-pattern: {bad!r}")

    # (2) metacharacter rejection — unquoted operators only (quoted = literal).
    meta = _unquoted_metachars(command)
    if meta:
        return _reject(
            "shell metacharacters not allowed outside quotes "
            f"(run one command per call; quoting is fine): {meta}"
        )

    # tokenize.
    try:
        argv = shlex.split(command)
    except ValueError as e:
        return _reject(f"could not tokenize: {e}")
    if not argv:
        return _reject("empty command")

    # (3) path traversal / absolute / home expansion.
    for arg in argv[1:]:
        if _has_traversal(arg):
            return _reject(f"path traversal / absolute / home-expansion arg: {arg!r}")

    # (4) allowlist.
    if argv[0] not in ALLOWLIST:
        return _reject(f"command not in allowlist: {argv[0]!r}")

    # (5) argument-level guards.
    guard_reason = _check_argument_guards(argv)
    if guard_reason:
        return _reject(guard_reason)

    # (1)+(7) exec without a shell, with timeout + output cap.
    try:
        proc = subprocess.run(
            argv,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return ShellResult(ok=False, output=f"ERROR: timeout after {timeout_s}s")
    except FileNotFoundError:
        return _reject(f"executable not found: {argv[0]!r}")

    combined = (proc.stdout or "") + (proc.stderr or "")
    if len(combined) > output_cap:
        combined = combined[:output_cap] + f"\n[TRUNCATED: output exceeds {output_cap} bytes]"
    status = f"[exit {proc.returncode}]\n"
    return ShellResult(ok=proc.returncode == 0, output=status + combined)
