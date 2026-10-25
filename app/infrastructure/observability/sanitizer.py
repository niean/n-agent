"""Pure-function log content sanitizer (Infrastructure layer).

Spec-261027: logs (stderr JSON, OTel projection, span events) carry readable
message/exception/stack + live scene, but sensitive content is redacted via a
fixed pattern set plus a known-secret catalog injected at startup. All matches
emit the single ``[REDACTED]`` placeholder -- irreversible, no key management,
no decryption.

Rules implemented here:
- Exact known-secret values (longest-at-layout-position, single pass;
  replacement output does not re-enter secret matching).
- Fixed patterns: Authorization headers (Bearer/Basic), URL userinfo,
  credential field values (api_key/token/password/secret) in JSON/URL
  query/``:``/``=`` forms, complete JWT tokens, PEM private-key blocks.
- Explicitly NOT treated as secrets: arbitrary UUID / long hex values outside a
  credential field or auth syntax (they are usually session/call/trace ids and
  blanket replacement would break correlation).

Each rule is a ``(pattern, replacer)`` pair where ``replacer`` receives the
``re.Match`` and returns the replacement string. Pure stdlib dep (``re``),
no Application/Domain imports. One atomic configuration snapshot is captured
per ``sanitize`` call.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Callable, Pattern

REDACTED = "[REDACTED]"

Matcher = Callable[[re.Match], str]


@dataclass(frozen=True)
class Rule:
    pattern: Pattern[str]
    replacer: Matcher

    @property
    def is_default(self) -> bool:
        return self._in_default

    _in_default: bool = False


def _redact_group(group_index: int) -> Matcher:
    """Build a replacer that redacts exactly one captured group, keeping all
    other matched text (e.g. field name/separator/scheme/@).

    ``re`` passes the replacer the whole Match and swaps the returned string
    for the *matched span*, so offsets are computed relative to ``span(0)``."""

    def replacer(match: re.Match) -> str:
        whole_start = match.start(0)
        start, end = match.span(group_index)
        token = match.group(0)
        rel_start = start - whole_start
        rel_end = end - whole_start
        return token[:rel_start] + REDACTED + token[rel_end:]

    return replacer


def _redact_last_group(match: re.Match) -> str:
    """Redact the last (by position) captured group; a group-less match
    redacts the whole token. Used by custom ``register_scrub_rule`` patterns
    where the value is expected in the final group."""
    whole_start = match.start(0)
    token = match.group(0)
    for group_index in range(len(match.groups()), 0, -1):
        start, end = match.span(group_index)
        if start >= 0:
            return token[: start - whole_start] + REDACTED + token[end - whole_start :]
    return REDACTED


# ---------------------------------------------------------------------------
# Default (built-in) rules
# ---------------------------------------------------------------------------

# Authorization header: keep "Bearer "/"Basic ", redact the credential token.
_PAT_AUTHORIZATION = re.compile(
    r"(?i)(\bBearer\s+|\bBasic\s+)([A-Za-z0-9\-._~+/]+=*)"
)
# URL userinfo: scheme://user:pass@host -> scheme://[REDACTED]@host
# scheme (group1), userinfo (group2), "@" (group3); redact group2, keep the @.
_PAT_URL_USERINFO = re.compile(
    r"(?i)([a-z][a-z0-9+.-]*://)([^/\r\n\s@]+)(@)"
)
# Credential fields: keep name + separator, redact value. Forms:
#   name=value / name: value / name : "value" / "name": "value" / ?name=value
# Note: "authorization" is deliberately absent from the credential field-name
# list below; Authorization headers are handled exclusively by
# ``_PAT_AUTHORIZATION`` (keeps the "Bearer "/"Basic " scheme) so the two rules
# never double-hits the same segment.
_PAT_CREDENTIAL_QUOTED = re.compile(
    r'(?i)("[\w.-]*(?:api[_-]?key|apikey|token|password|passwd|pwd|secret)[\w.-]*"'
    r')(\s*:\s*")([^"]*)(?:"|$)'
)
_PAT_CREDENTIAL_RAW = re.compile(
    r"(?i)([\w.-]*(?:api[_-]?key|apikey|token|password|passwd|pwd|secret)"
    r"[\w.-]*)(\s*[:=]\s*|\s*:\s*)([^\s,\";&]+)"
)
# JWT: three base64url segments (no capture group -> whole match redacted).
_PAT_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\."
    r"[A-Za-z0-9_-]{4,}(?![A-Za-z0-9])"
)
# PEM private key block (multi-line), no group -> whole match redacted.
_PAT_PEM = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)

_DEFAULT_RULES = (
    Rule(_PAT_AUTHORIZATION, _redact_group(2)),
    Rule(_PAT_URL_USERINFO, _redact_group(2)),
    Rule(_PAT_CREDENTIAL_RAW, _redact_group(3)),
    Rule(_PAT_CREDENTIAL_QUOTED, _redact_group(3)),
    Rule(_PAT_JWT, _redact_last_group),
    Rule(_PAT_PEM, _redact_last_group),
)


class LogSanitizer:
    """Immutable-version, thread-safe content sanitizer.

    Each ``sanitize`` builds a frozen snapshot (rules tuple + known secrets
    frozenset) under a lock, so a concurrent ``register_scrub_rule`` /
    ``configure_secret_catalog`` never produces a half-versioned result within
    one call. ``register_scrub_rule`` is a trusted-startup extension point
    that never accepts untrusted input.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rules: list[Rule] = [
            Rule(r.pattern, r.replacer, _in_default=True) for r in _DEFAULT_RULES
        ]
        self._known_secrets: frozenset[str] = frozenset()

    # -- configuration ------------------------------------------------------

    def register_scrub_rule(self, pattern: str | Pattern[str]) -> None:
        """Append a fixed sensitive pattern (trusted startup). Compiles and
        validates first; on error the pattern is rejected and no partial
        state is published. Custom rules redact their whole matches."""
        if isinstance(pattern, Pattern):
            compiled = pattern
        elif isinstance(pattern, str):
            compiled = re.compile(pattern)
        else:
            raise TypeError("scrub rule must be a str or compiled re.Pattern")
        with self._lock:
            self._rules.append(Rule(compiled, _redact_last_group))

    def configure_secret_catalog(self, known_secrets: object) -> None:
        """Atomically publish an immutable set of known secret values.

        Empty strings are never secrets. An empty catalog is valid and still
        runs the fixed-pattern rules."""
        secrets = frozenset(
            v for v in (known_secrets or ()) if isinstance(v, str) and v
        )
        with self._lock:
            self._known_secrets = secrets

    def sanitize_partial(self, text: str) -> str:
        """Also conceal a credential prefix at an interrupted byte boundary."""
        with self._lock:
            secrets = self._known_secrets
        for secret in secrets:
            minimum = min(8, max(1, len(secret) // 2))
            for size in range(min(len(secret) - 1, len(text)), minimum - 1, -1):
                if text.endswith(secret[:size]):
                    text = text[:-size] + REDACTED
                    break
        return self.sanitize(text)

    # -- applies ------------------------------------------------------------

    def sanitize(self, text: str) -> str:
        """Redact known secrets and fixed sensitive patterns in ``text``.

        ``text`` must be str; non-str returns unchanged (callers validate
        types before invoking). Replacement output does not re-enter pattern
        matching, so a short known secret cannot corrupt an emitted
        ``[REDACTED]`` placeholder."""
        if not isinstance(text, str):
            return text
        with self._lock:
            rules = tuple(self._rules)
            secrets = self._known_secrets
        out = self._redact_secrets(text, secrets)
        for rule in rules:
            out = rule.pattern.sub(rule.replacer, out)
        return out

    @staticmethod
    def _redact_secrets(text: str, secrets: frozenset[str]) -> str:
        if not secrets:
            return text
        # Single left-to-right pass, longest value wins at a given start;
        # replacement output is not rescanned for secrets.
        result = []
        i = 0
        n = len(text)
        while i < n:
            best_end = -1
            best_len = -1
            for secret in secrets:
                ln = len(secret)
                if ln <= best_len:
                    continue
                if text.startswith(secret, i):
                    best_end = i + ln
                    best_len = ln
            if best_end >= 0:
                result.append(REDACTED)
                i = best_end
            else:
                result.append(text[i])
                i += 1
        return "".join(result)
