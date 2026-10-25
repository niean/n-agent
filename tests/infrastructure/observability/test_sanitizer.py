"""LogSanitizer fixed-pattern + known-secret redaction tests.

Spec-261027. 纯函数脱敏单元：固定规则集（认证头/凭据字段/JWT/PEM/URL
userinfo），叠加注入的已知密钥精确值；全部输出统一 ``[REDACTED]``。
不把无凭据语法的 UUID/hex 一律当秘密（它们常是 session/call/trace 标识）。
"""
from __future__ import annotations

import re

import pytest

from app.infrastructure.observability.sanitizer import LogSanitizer


def _s(*rules):
    sanitizer = LogSanitizer()
    for rule in rules:
        sanitizer.register_scrub_rule(rule)
    return sanitizer


# ---------------------------------------------------------------------------
# Bearer / Basic / Authorization headers
# ---------------------------------------------------------------------------


def test_redacts_authorization_bearer_and_keeps_neighbors():
    s = _s()
    out = s.sanitize("k=alice Authorization: Bearer abc123 done")
    assert out == "k=alice Authorization: Bearer [REDACTED] done"


def test_authorization_matching_is_case_insensitive_and_basic():
    s = _s()
    assert s.sanitize("authorization: basic dXNlcjpwYXNz") == \
        "authorization: basic [REDACTED]"


# ---------------------------------------------------------------------------
# URL userinfo
# ---------------------------------------------------------------------------


def test_redacts_url_userinfo_whole_segment():
    s = _s()
    out = s.sanitize("GET http://alice:hunter2@host/p?q=1")
    assert out == "GET http://[REDACTED]@host/p?q=1"
    assert "alice" not in out and "hunter2" not in out


# ---------------------------------------------------------------------------
# Credential fields (JSON quote / colon / equals / URL query)
# ---------------------------------------------------------------------------


def test_redacts_credential_field_query_value():
    s = _s()
    assert s.sanitize("token=sk-abc123 rest") == "token=[REDACTED] rest"


def test_redacts_credential_field_json_quoted():
    s = _s()
    assert s.sanitize('{"api_key":"sk-secret","name":"alice"}') == \
        '{"api_key":"[REDACTED]","name":"alice"}'


def test_redacts_credential_field_colon_value():
    s = _s()
    assert s.sanitize("password: hunter2!") == "password: [REDACTED]"


# ---------------------------------------------------------------------------
# JWT three segments
# ---------------------------------------------------------------------------


def test_redacts_full_jwt_token():
    s = _s()
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    out = s.sanitize(f"tok={jwt} end")
    assert out == "tok=[REDACTED] end"
    assert jwt[:10] not in out


# ---------------------------------------------------------------------------
# PEM private key block (multi-line)
# ---------------------------------------------------------------------------


def test_redacts_pem_private_key_block():
    s = _s()
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAsecretline==\n"
        "-----END RSA PRIVATE KEY-----\n"
    )
    out = s.sanitize(f"conf\n{pem}rest")
    assert "BEGIN" not in out and "END" not in out and "secretline" not in out


# ---------------------------------------------------------------------------
# Do NOT treat unmarked UUID/hex as secrets
# ---------------------------------------------------------------------------


def test_keeps_uuid_and_session_trace_identifiers():
    s = _s()
    text = "call=call_9f2a32f1 session=ab12cd trace=9f2a32f1bef0daab"
    assert s.sanitize(text) == text


def test_empty_catalog_is_noop_for_bare_text():
    s = _s()
    assert s.sanitize("plain text 1234-5678-abcdef") == "plain text 1234-5678-abcdef"


# ---------------------------------------------------------------------------
# Known-secret exact replacement
# ---------------------------------------------------------------------------


def test_known_secret_exact_replaced():
    s = _s()
    secret = "sk-live-9f2a32f1"
    s.configure_secret_catalog({secret})
    assert s.sanitize(f"key={secret} ok") == "key=[REDACTED] ok"


def test_overlapping_known_secrets_longest_first():
    s = _s()
    short = "ab"
    long = "abcdef"
    s.configure_secret_catalog({short, long})
    assert s.sanitize("xxabcdefyy") == "xx[REDACTED]yy"


def test_empty_string_not_a_secret():
    s = _s()
    s.configure_secret_catalog({"", "real-secret"})
    assert s.sanitize("plain and real-secret") == "plain and [REDACTED]"


def test_unknown_secret_not_replaced():
    s = _s()
    s.configure_secret_catalog({"known-a"})
    assert s.sanitize("secret=known-b") == "secret=[REDACTED]"  # rule covers value shape


# ---------------------------------------------------------------------------
# register_scrub_rule validation
# ---------------------------------------------------------------------------


def test_register_scrub_rule_bad_pattern_rejected_and_no_partial_publish():
    s = LogSanitizer()
    with pytest.raises(re.error):
        s.register_scrub_rule("unclosed[(")
    # nothing was published: a previously registered rule still works
    s.register_scrub_rule(r"tok=(\w+)")
    assert s.sanitize("tok=abc") == "tok=[REDACTED]"


def test_register_scrub_rule_accepts_str_or_pattern():
    s = LogSanitizer()
    s.register_scrub_rule(r"pw=(\w+)")
    s.register_scrub_rule(re.compile(r"ssn=(\d{3}-\d{2}-\d{4})"))
    assert s.sanitize("pw=xyz ssn=123-45-6789") == \
        "pw=[REDACTED] ssn=[REDACTED]"


def test_default_rules_present_and_cannot_be_removed():
    s = LogSanitizer()
    # default rules include Authorization/Bearer handling
    assert "[REDACTED]" in s.sanitize("Authorization: Bearer tok123")