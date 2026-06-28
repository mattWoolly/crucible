"""Layered JSON extraction for small-model outputs (SPEC §8).

Small models frequently emit JSON that is *almost* valid: code fences,
trailing prose, an inline example object before the real answer, invalid
escapes. This extractor degrades gracefully and **never throws** — the final
layer returns a degraded result so the orchestrator keeps running (§8, §11).
"""

from __future__ import annotations

import json
import re

from .models import WorkerResult

_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*|\s*```\s*$", re.MULTILINE)
# A backslash NOT followed by a valid JSON escape char.
_BAD_ESCAPE_RE = re.compile(r'\\(?!["\\/bfnrtu])')


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text).strip()


def _balanced_candidates(text: str) -> list[str]:
    """Return every balanced ``{...}`` substring via brace scanning.

    Uses a decoder per candidate so quoted braces inside strings don't throw
    off the depth count.
    """
    candidates: list[str] = []
    decoder = json.JSONDecoder()
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "{":
            try:
                _, end = decoder.raw_decode(text, i)
            except json.JSONDecodeError:
                i += 1
                continue
            candidates.append(text[i:end])
            i = end
        else:
            i += 1
    return candidates


def _sanitize_escapes(text: str) -> str:
    return _BAD_ESCAPE_RE.sub(r"\\\\", text)


def extract_json(text: str) -> dict | None:
    """Best-effort parse to a dict, or ``None`` if every layer fails.

    Layer order (§8):
      1. strip markdown code fences
      2. direct ``json.loads``
      3. balanced-brace scan, prefer the **largest** object (tie -> last)
      4. invalid-escape sanitization, then retry 2 & 3
    """
    stripped = _strip_fences(text)

    # Layer 2: direct.
    for candidate_text in (stripped, text):
        try:
            obj = json.loads(candidate_text)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    # Layer 3: balanced-brace scan; prefer largest, tie -> last.
    def best_balanced(src: str) -> dict | None:
        best: dict | None = None
        best_len = -1
        for cand in _balanced_candidates(src):
            try:
                obj = json.loads(cand)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if len(cand) >= best_len:  # >= so a later, equal-length object wins the tie
                best, best_len = obj, len(cand)
        return best

    found = best_balanced(stripped)
    if found is not None:
        return found

    # Layer 4: sanitize invalid escapes, retry direct + balanced.
    sanitized = _sanitize_escapes(stripped)
    try:
        obj = json.loads(sanitized)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    return best_balanced(sanitized)


def extract_worker_result(text: str) -> WorkerResult:
    """Parse a worker's JSON answer into a ``WorkerResult``.

    Falls back to the canonical degraded result (§8) if parsing fails or the
    parsed object doesn't satisfy the schema.
    """
    obj = extract_json(text)
    if obj is None:
        return WorkerResult.degraded("no json object found", raw=text)
    try:
        return WorkerResult.model_validate(obj)
    except Exception as exc:  # pydantic ValidationError or unexpected shape
        return WorkerResult.degraded(f"schema mismatch: {exc}", raw=text)
