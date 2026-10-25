"""Tests for app/domain/otel_link_config.py (Domain Layer)."""
import dataclasses

import pytest

from app.domain.otel_link_config import (
    OTEL_LINK_CONFIG_FIELDS,
    OtelLinkConfig,
    OtelLinkConfigAuditEvent,
    OtelLinkConfigConflictError,
    OtelLinkConfigOverrides,
    OtelLinkConfigStoreError,
    OtelLinkConfigValidationError,
    OtelLinkConfigFieldState,
    ResolvedOtelLinkConfig,
    StoredOtelLinkConfig,
    build_otel_link_field_state,
    merge_overrides,
    validate_otel_link_config,
)

FIELDS = ("metric_base_url", "log_base_url", "trace_base_url", "profile_base_url")


def make_cfg(**kwargs) -> OtelLinkConfig:
    return OtelLinkConfig(**kwargs)


class TestFieldsOrder:
    def test_stable_field_order(self):
        assert OTEL_LINK_CONFIG_FIELDS == (
            "metric_base_url",
            "log_base_url",
            "trace_base_url",
            "profile_base_url",
        )

    def test_fields_match_dataclass(self):
        assert tuple(f.name for f in dataclasses.fields(OtelLinkConfig)) == OTEL_LINK_CONFIG_FIELDS
        assert tuple(f.name for f in dataclasses.fields(OtelLinkConfigOverrides)) == (
            OTEL_LINK_CONFIG_FIELDS
        )


class TestOtelLinkConfig:
    def test_frozen(self):
        cfg = OtelLinkConfig()
        assert dataclasses.is_dataclass(cfg)
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.metric_base_url = "http://x"  # type: ignore[misc]

    def test_defaults_are_empty_strings(self):
        cfg = OtelLinkConfig()
        for f in FIELDS:
            assert getattr(cfg, f) == ""
            assert isinstance(getattr(cfg, f), str)


class TestOverridesRoundTrip:
    def test_frozen(self):
        ov = OtelLinkConfigOverrides()
        with pytest.raises(dataclasses.FrozenInstanceError):
            ov.metric_base_url = "http://x"  # type: ignore[misc]

    def test_absent_or_null_means_follow_env(self):
        ov = OtelLinkConfigOverrides.from_dict({"metric_base_url": None})
        assert ov.metric_base_url is None
        assert ov.log_base_url is None
        assert ov.trace_base_url is None
        assert ov.profile_base_url is None
        assert ov.to_dict() == {}
        assert ov.overridden_fields() == ()

    def test_empty_string_means_disabled(self):
        ov = OtelLinkConfigOverrides.from_dict({"log_base_url": ""})
        assert ov.log_base_url == ""
        assert ov.to_dict() == {"log_base_url": ""}
        assert ov.overridden_fields() == ("log_base_url",)

    def test_non_empty_override(self):
        ov = OtelLinkConfigOverrides.from_dict({"trace_base_url": "http://jaeger:16686"})
        assert ov.trace_base_url == "http://jaeger:16686"
        assert ov.to_dict() == {"trace_base_url": "http://jaeger:16686"}
        assert ov.overridden_fields() == ("trace_base_url",)

    def test_round_trip_mixed(self):
        data = {"metric_base_url": "", "profile_base_url": "https://grafana.example.com/a"}
        ov = OtelLinkConfigOverrides.from_dict(data)
        assert ov.to_dict() == data
        assert ov.overridden_fields() == ("metric_base_url", "profile_base_url")


class TestFromDictStrict:
    @pytest.mark.parametrize("bad", [["metric_base_url"], "metric_base_url", 3, True])
    def test_non_dict_rejected(self, bad):
        with pytest.raises(OtelLinkConfigStoreError):
            OtelLinkConfigOverrides.from_dict(bad)

    def test_unknown_key_rejected(self):
        with pytest.raises(OtelLinkConfigStoreError):
            OtelLinkConfigOverrides.from_dict({"unknown_field": "http://x"})

    @pytest.mark.parametrize("bad_value", [True, False, 1, 3.14, ["http://x"], {"a": 1}])
    def test_non_str_value_rejected(self, bad_value):
        with pytest.raises(OtelLinkConfigStoreError):
            OtelLinkConfigOverrides.from_dict({"metric_base_url": bad_value})


class TestValidate:
    def test_all_empty_passes(self):
        validate_otel_link_config(OtelLinkConfig())

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:3000",
            "https://grafana.example.com",
            "https://grafana.example.com/d/abc?var-service=n-agent&from=now-1h",
            "http://[::1]:3000/path",
            "http://127.0.0.1:9090/api/v1/query?query=up",
        ],
    )
    def test_valid_urls_pass(self, url):
        validate_otel_link_config(make_cfg(metric_base_url=url))

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.com",
            "grafana.example.com",
            "//example.com/path",
            "file:///etc/passwd",
        ],
    )
    def test_non_http_scheme_rejected(self, url):
        with pytest.raises(OtelLinkConfigValidationError):
            validate_otel_link_config(make_cfg(metric_base_url=url))

    def test_missing_hostname_rejected(self):
        with pytest.raises(OtelLinkConfigValidationError):
            validate_otel_link_config(make_cfg(metric_base_url="http://"))
        with pytest.raises(OtelLinkConfigValidationError):
            validate_otel_link_config(make_cfg(metric_base_url="http:///path"))

    @pytest.mark.parametrize(
        "url",
        [
            "http://user:pass@grafana.example.com",
            "http://user@grafana.example.com",
            "http://:pass@grafana.example.com",
            "http://@grafana.example.com",  # empty userinfo still rejected
        ],
    )
    def test_userinfo_rejected(self, url):
        with pytest.raises(OtelLinkConfigValidationError):
            validate_otel_link_config(make_cfg(metric_base_url=url))

    def test_invalid_port_rejected(self):
        with pytest.raises(OtelLinkConfigValidationError):
            validate_otel_link_config(make_cfg(metric_base_url="http://example.com:notaport"))
        with pytest.raises(OtelLinkConfigValidationError):
            validate_otel_link_config(make_cfg(metric_base_url="http://example.com:99999"))

    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com/a b",          # ASCII space
            "http://example.com/a\tb",         # tab
            "http://example.com/a\nb",         # newline
            "http://example.com/a\rb",         # carriage return
            "http://example.com/a\x7fb",       # DEL
            "http://example.com/a\x85b",       # C1 control
            "http://example.com/a\xa0b",  # Unicode no-break space (U+00A0)
            "http://example.com/a\\b",         # backslash
        ],
    )
    def test_control_whitespace_backslash_rejected(self, url):
        with pytest.raises(OtelLinkConfigValidationError):
            validate_otel_link_config(make_cfg(metric_base_url=url))

    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com/#frag",
            "http://example.com#frag",
            "http://example.com/path?a=b#c",
        ],
    )
    def test_fragment_rejected(self, url):
        with pytest.raises(OtelLinkConfigValidationError):
            validate_otel_link_config(make_cfg(metric_base_url=url))

    def test_length_limit(self):
        base = "http://example.com/"
        ok = base + "a" * (2048 - len(base))
        validate_otel_link_config(make_cfg(metric_base_url=ok))  # exactly 2048 passes
        too_long = base + "a" * (2049 - len(base))
        with pytest.raises(OtelLinkConfigValidationError):
            validate_otel_link_config(make_cfg(metric_base_url=too_long))

    @pytest.mark.parametrize("bad", [True, False, 1, None, ["x"]])
    def test_non_str_field_rejected(self, bad):
        cfg = OtelLinkConfig(metric_base_url=bad)  # type: ignore[arg-type]
        with pytest.raises(OtelLinkConfigValidationError):
            validate_otel_link_config(cfg)

    def test_fields_subset_validated_only(self):
        cfg = OtelLinkConfig(metric_base_url="not a url", log_base_url="http://ok.example.com")
        validate_otel_link_config(cfg, fields=("log_base_url",))
        with pytest.raises(OtelLinkConfigValidationError):
            validate_otel_link_config(cfg, fields=("metric_base_url",))

    def test_default_fields_cover_all(self):
        cfg = OtelLinkConfig(profile_base_url="gopher://x")
        with pytest.raises(OtelLinkConfigValidationError):
            validate_otel_link_config(cfg)


class TestMergeOverrides:
    def test_none_falls_back_to_env_base(self):
        base = OtelLinkConfig(metric_base_url="http://env-metrics", log_base_url="http://env-logs")
        merged = merge_overrides(base, OtelLinkConfigOverrides())
        assert merged == base

    def test_empty_string_override_disables(self):
        base = OtelLinkConfig(log_base_url="http://env-logs")
        merged = merge_overrides(base, OtelLinkConfigOverrides(log_base_url=""))
        assert merged.log_base_url == ""

    def test_non_empty_override_wins(self):
        base = OtelLinkConfig(trace_base_url="http://env-jaeger")
        merged = merge_overrides(base, OtelLinkConfigOverrides(trace_base_url="http://db-jaeger"))
        assert merged.trace_base_url == "http://db-jaeger"
        # untouched fields keep env values
        assert merged.metric_base_url == ""
        assert merged.log_base_url == ""
        assert merged.profile_base_url == ""


class TestStoredAndResolved:
    def test_stored_fields(self):
        ov = OtelLinkConfigOverrides(metric_base_url="http://x")
        stored = StoredOtelLinkConfig(
            overrides=ov, version=3, updated_at="2026-10-26T00:00:00Z", updated_by="dashboard-local"
        )
        assert stored.overrides is ov
        assert stored.version == 3

    def test_resolved_memory_sentinel(self):
        resolved = ResolvedOtelLinkConfig(config=OtelLinkConfig(), version=0)
        assert resolved.version == 0
        assert resolved.overridden_fields == ()
        assert resolved.updated_at is None
        assert resolved.updated_by is None


class TestFieldStateRedaction:
    def test_empty_resolved_gives_empty_url(self):
        state = build_otel_link_field_state("", "")
        assert state == OtelLinkConfigFieldState(override="disabled", url="")

    def test_override_states(self):
        assert build_otel_link_field_state(None, "http://env").override == "env"
        assert build_otel_link_field_state("", "").override == "disabled"
        assert build_otel_link_field_state("http://x", "http://x").override == "custom"

    def test_path_query_fragment_dropped(self):
        url = "https://grafana.example.com:8443/d/abc?token=SECRET&var-service=n-agent"
        state = build_otel_link_field_state(url, url)
        assert state.url == "https://grafana.example.com:8443"
        assert "SECRET" not in state.url
        assert "token" not in state.url

    def test_ipv6_brackets_preserved(self):
        state = build_otel_link_field_state("http://[::1]:3000/a", "http://[::1]:3000/a")
        assert state.url == "http://[::1]:3000"

    def test_default_port_dropped_when_absent(self):
        state = build_otel_link_field_state("https://example.com/p", "https://example.com/p")
        assert state.url == "https://example.com"

    def test_invalid_url_gives_invalid_marker(self):
        state = build_otel_link_field_state("http://example.com:notaport", "http://example.com:notaport")
        assert state.url == "<invalid>"


class TestAuditEvent:
    def test_audit_event_with_field_states(self):
        before = build_otel_link_field_state(None, "http://env-metrics/explore?token=SECRET")
        after = build_otel_link_field_state("", "")
        event = OtelLinkConfigAuditEvent(
            actor="dashboard-local",
            updated_at="2026-10-26T00:00:00Z",
            old_version=1,
            new_version=2,
            changed_fields={"metric_base_url": (before, after)},
        )
        assert event.changed_fields["metric_base_url"][0].override == "env"
        assert event.changed_fields["metric_base_url"][0].url == "http://env-metrics"
        assert "SECRET" not in event.changed_fields["metric_base_url"][0].url
        assert event.changed_fields["metric_base_url"][1] == OtelLinkConfigFieldState(
            override="disabled", url=""
        )

