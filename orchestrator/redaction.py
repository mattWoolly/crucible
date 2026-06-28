"""Secret redaction for trace previews (SPEC §9).

llm_call/tool_call previews carry file contents and tool args straight into a
third-party trace UI. Before any preview leaves the process we scrub secret
patterns. A file-reading agent must not exfiltrate the repo into the backend.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),                       # OpenAI-style keys
    re.compile(r"AKIA[0-9A-Z]{12,}"),                            # AWS access key id
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{10,}"),            # bearer tokens
    re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password)\b\s*[=:]\s*\S+"),  # KEY=...
    re.compile(r"[A-Za-z0-9._%+\-]*(?:live|prod)_[A-Za-z0-9]{12,}"),  # xxx_live_...
    re.compile(r"eyJ[A-Za-z0-9_\-]{3,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]+"),  # JWT
]


def _scrub_str(s: str) -> str:
    for pat in _SECRET_PATTERNS:
        s = pat.sub(REDACTED, s)
    return s


def redact(obj, *, field_allowlist: set[str] | None = None):
    """Deep-walk dict/list/str, scrubbing secret patterns.

    If ``field_allowlist`` is given, dict values whose key is NOT allowlisted
    are dropped entirely (replaced with ``[REDACTED]``); allowlisted values are
    still pattern-scrubbed.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if field_allowlist is not None and k not in field_allowlist:
                out[k] = REDACTED
            else:
                out[k] = redact(v, field_allowlist=field_allowlist)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v, field_allowlist=field_allowlist) for v in obj]
    if isinstance(obj, str):
        return _scrub_str(obj)
    return obj


def redact_preview(text: str, max_len: int = 1000) -> str:
    """Truncate then scrub a preview string."""
    if text is None:
        return ""
    truncated = text[:max_len]
    scrubbed = _scrub_str(truncated)
    if len(text) > max_len:
        scrubbed += " …[truncated]"
    return scrubbed
