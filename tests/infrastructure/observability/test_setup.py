"""Tests for OTel SDK assembly (T3 of plan-261024-otel-observability).

Covers:
- disabled state: handle status=disabled, no provider/thread initialization
- enabled+endpoint: OTLP exporter assembly params (endpoint, insecure,
  resource attrs service.name/version/instance.id, sampler, queue sizes)
- idempotent configure: second call reuses the handle
- partial failure rollback: cleans up registered resources, returns
  degraded, does not replace an existing global Provider
- local-only (endpoint empty): no network exporter, bounded metric reader
- snapshot(): pure JSON contract (status/mode/instance_id/sample ratio,
  instrument list, span buffer <=100 with fields capped at 256 chars,
  per-signal delivery disabled/unknown/succeeded/failed, semconv version)
- shutdown(): total budget, producers stopped before flush, drop after
  deadline, repeat-safe
- ring buffer only accepts ended, sampled spans
- ParentBased sampler composition (no parent -> ratio; remote sampled /
  unsampled parent -> inherited)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    TraceFlags,
    set_span_in_context,
)
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.metrics.export import MetricExportResult
from opentelemetry.trace.status import Status, StatusCode

from app.application import telemetry_semantics as ts
from app.config import Settings
from app.infrastructure.observability import setup


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeResource:
    def __init__(self, attributes=None):
        self.attributes = dict(attributes or {})


class FakeExporter:
    """Fake OTLP exporter recording assembly kwargs and export results."""

    instances: list["FakeExporter"] = []

    def __init__(self, endpoint=None, insecure=None, timeout=None, **kwargs):
        self.kwargs = {
            "endpoint": endpoint,
            "insecure": insecure,
            "timeout": timeout,
        }
        self.extra_kwargs = kwargs
        self.exports: list[object] = []
        self.fail_exports = False
        self.shutdown_calls = 0
        self.force_flush_calls = 0
        type(self).instances.append(self)

    def export(self, batch, timeout_millis=None, **kwargs):
        self.exports.append(batch)
        if self.fail_exports:
            return (
                MetricExportResult.FAILURE
                if isinstance(self, FakeMetricExporter)
                else SpanExportResult.FAILURE
            )
        return (
            MetricExportResult.SUCCESS
            if isinstance(self, FakeMetricExporter)
            else SpanExportResult.SUCCESS
        )

    def force_flush(self, timeout_millis=None):
        self.force_flush_calls += 1
        return True

    def shutdown(self, timeout_millis=None, **kwargs):
        self.shutdown_calls += 1
        return True


class FakeSpanExporter(FakeExporter):
    pass


class FakeMetricExporter(FakeExporter):
    pass


class FakeLogExporter(FakeExporter):
    pass


class FailingMetricExporter(FakeMetricExporter):
    def __init__(self, *args, **kwargs):
        raise RuntimeError("boom: metric exporter construction failed")


class FakeBatchProcessor:
    def __init__(self, exporter, max_queue_size=None, max_export_batch_size=None,
                 export_timeout_millis=None, max_export_batch_size_log=None, **kwargs):
        self.exporter = exporter
        self.kwargs = {
            "max_queue_size": max_queue_size,
            "max_export_batch_size": max_export_batch_size,
            "export_timeout_millis": export_timeout_millis,
        }

    def shutdown(self, timeout_millis=None):
        return True

    def force_flush(self, timeout_millis=None):
        return True


class FakeMetricReader:
    def __init__(self, exporter, export_interval_millis=None,
                 export_timeout_millis=None, **kwargs):
        self.exporter = exporter
        self.kwargs = {
            "export_interval_millis": export_interval_millis,
            "export_timeout_millis": export_timeout_millis,
        }

    def shutdown(self, timeout_millis=None, **kwargs):
        return True

    def force_flush(self, timeout_millis=None):
        return True


class FakeProvider:
    """Fake Tracer/Meter/Logger provider with event recording."""

    instances: list["FakeProvider"] = []

    def __init__(self, resource=None, sampler=None, metric_readers=None, **kwargs):
        type(self).instances.append(self)
        self.resource = resource
        self.sampler = sampler
        self.metric_readers = list(metric_readers or [])
        self.kwargs = dict(
            resource=resource, sampler=sampler, metric_readers=self.metric_readers
        )
        self.processors: list[object] = []
        self.events: list[tuple] = []
        self.event_lock = __import__("threading").Lock()
        self.block_seconds = 0.0

    def _record(self, event):
        with self.event_lock:
            self.events.append(event)

    def add_span_processor(self, processor):
        self.processors.append(processor)

    add_log_record_processor = add_span_processor

    def force_flush(self, timeout_millis=None):
        self._record(("force_flush", timeout_millis))
        if self.block_seconds:
            time.sleep(self.block_seconds)
        return True

    def shutdown(self):
        self._record(("shutdown",))
        if self.block_seconds:
            time.sleep(self.block_seconds)
        return True


class _ProxyTracerProvider:
    pass


class _ProxyMeterProvider:
    pass


class _ProxyLoggerProvider:
    pass


_PROXY_PROVIDER_NAMES = {
    "ProxyTracerProvider",
    "_ProxyMeterProvider",
    "ProxyLoggerProvider",
}


class FakeLoader:
    """SDK loader stand-in: all global setter/getter calls are recorded and
    never touch the real OTel global state. Unclaimed globals are modelled
    the same way the real loader detects them: by proxy provider type
    names."""

    def __init__(self, *, claimed_tracer=False, claimed_meter=False,
                 claimed_logger=False, metric_exporter_cls=FakeMetricExporter):
        self.Resource = FakeResource
        self.OTLPSpanExporter = FakeSpanExporter
        self.OTLPMetricExporter = metric_exporter_cls
        self.OTLPLogExporter = FakeLogExporter
        self.BatchSpanProcessor = FakeBatchProcessor
        self.BatchLogRecordProcessor = FakeBatchProcessor
        self.PeriodicExportingMetricReader = FakeMetricReader
        self.TracerProvider = FakeProvider
        self.MeterProvider = FakeProvider
        self.LoggerProvider = FakeProvider
        self.set_tracer_calls: list[object] = []
        self.set_meter_calls: list[object] = []
        self.set_logger_calls: list[object] = []
        self._claimed_tracer = claimed_tracer
        self._claimed_meter = claimed_meter
        self._claimed_logger = claimed_logger
        # unclaimed -> proxy-named provider (what the real OTel API returns
        # before anyone sets a provider); claimed -> external owner
        self._tracer_provider = (
            SimpleNamespace(external="tracer") if claimed_tracer
            else _ProxyTracerProvider()
        )
        self._meter_provider = (
            SimpleNamespace(external="meter") if claimed_meter
            else _ProxyMeterProvider()
        )
        self._logger_provider = (
            SimpleNamespace(external="logger") if claimed_logger
            else _ProxyLoggerProvider()
        )

    def set_tracer_provider(self, provider):
        self.set_tracer_calls.append(provider)

    def set_meter_provider(self, provider):
        self.set_meter_calls.append(provider)

    def set_logger_provider(self, provider):
        self.set_logger_calls.append(provider)

    def get_tracer_provider(self):
        return None if not self._claimed_tracer else self._tracer_provider

    def get_meter_provider(self):
        return None if not self._claimed_meter else self._meter_provider

    def get_logger_provider(self):
        return None if not self._claimed_logger else self._logger_provider

    def is_unclaimed(self, provider):
        # Same detection strategy as the real loader: proxy type names
        # (or nothing set at all) mean the global is still up for grabs.
        return provider is None or type(provider).__name__ in _PROXY_PROVIDER_NAMES


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


@pytest.fixture(autouse=True)
def _reset_setup_state():
    setup._reset_for_tests()
    FakeExporter.instances = []
    FakeProvider.instances = []
    yield
    setup._reset_for_tests()
    FakeExporter.instances = []
    FakeProvider.instances = []


def make_handle(loader=None, **overrides):
    kwargs = {"otel_enabled": True}
    kwargs.update(overrides)
    return setup.configure_observability(_settings(**kwargs), sdk_loader=loader)


def make_enabled_loader() -> FakeLoader:
    FakeExporter.instances = []
    return FakeLoader()


def ring_of(handle) -> setup._RingBufferSpanProcessor:
    return handle._ring_buffer


def make_ended_span(
    name="op",
    sampled=True,
    trace_id=0x11111111111111111111111111111111,
    span_id=0x2222222222222222,
    parent_span_id=None,
    attributes=None,
):
    ctx = SpanContext(
        trace_id=trace_id,
        span_id=span_id,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED if sampled else TraceFlags.DEFAULT),
    )
    parent = None
    if parent_span_id is not None:
        parent = SpanContext(
            trace_id=trace_id,
            span_id=parent_span_id,
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.DEFAULT),
        )
    return SimpleNamespace(
        name=name,
        context=ctx,
        parent=parent,
        start_time=1_000_000_000,
        end_time=1_500_000_000,
        status=Status(StatusCode.OK),
        attributes=dict(attributes or {}),
    )


# ---------------------------------------------------------------------------
# Disabled state
# ---------------------------------------------------------------------------


class TestDisabled:
    def test_returns_disabled_handle_without_sdk(self):
        class ExplodingLoader:
            def __getattr__(self, name):
                raise AssertionError(
                    f"disabled configure must not touch the SDK loader (got {name})"
                )

        handle = setup.configure_observability(
            _settings(otel_enabled=False), sdk_loader=ExplodingLoader()
        )
        assert handle.status == "disabled"
        snap = handle.snapshot()
        assert snap["status"] == "disabled"
        assert snap["mode"] == "disabled"
        assert snap["spans"] == []
        for signal in snap["signals"].values():
            assert signal["status"] == "disabled"
            assert signal["delivery"] == "disabled"

    def test_disabled_does_not_import_sdk_modules(self, monkeypatch):
        import sys

        for mod in list(sys.modules):
            if mod.startswith("opentelemetry.sdk") or mod.startswith(
                "opentelemetry.exporter"
            ):
                monkeypatch.delitem(sys.modules, mod)
        setup._reset_for_tests()
        setup.configure_observability(_settings(otel_enabled=False))
        assert not any(
            m.startswith("opentelemetry.sdk") or m.startswith("opentelemetry.exporter")
            for m in sys.modules
        )


# ---------------------------------------------------------------------------
# Assembly (enabled + endpoint)
# ---------------------------------------------------------------------------


class TestAssembly:
    def test_otlp_assembly_params_http(self):
        loader = make_enabled_loader()
        handle = make_handle(
            loader,
            otel_exporter_otlp_endpoint="http://otel-collector:4317",
            otel_traces_sample_ratio=0.5,
        )
        assert handle.status == "healthy"
        snap = handle.snapshot()
        assert snap["mode"] == "otlp"
        assert snap["sample_ratio"] == 0.5

        span_exporters = [e for e in FakeExporter.instances if isinstance(e, FakeSpanExporter)]
        assert len(span_exporters) == 1
        assert span_exporters[0].kwargs["endpoint"] == "http://otel-collector:4317"
        assert span_exporters[0].kwargs["insecure"] is True
        assert span_exporters[0].kwargs["timeout"] == 3.0
        for exporter in FakeExporter.instances:
            assert exporter.kwargs["endpoint"] == "http://otel-collector:4317"
            assert exporter.kwargs["insecure"] is True
            assert exporter.kwargs["timeout"] == 3.0

        # Resource attributes
        tracer_provider = loader.set_tracer_calls[0]
        attrs = tracer_provider.resource.attributes
        assert attrs["service.name"] == "n-agent"
        assert attrs["service.version"]
        assert attrs["service.instance.id"] == snap["instance_id"]

        # Sampler: ParentBased(root=TraceIdRatioBased(ratio))
        sampler = tracer_provider.sampler
        assert type(sampler).__name__ == "ParentBased"
        assert sampler._root.rate == 0.5

        # Queue sizes and batch sizes via processor kwargs
        bsp = [
            p for p in tracer_provider.processors
            if isinstance(p, FakeBatchProcessor)
        ]
        assert len(bsp) == 1
        assert bsp[0].kwargs["max_queue_size"] == 2048
        assert bsp[0].kwargs["max_export_batch_size"] == 512
        assert bsp[0].kwargs["export_timeout_millis"] == 3000.0

        meter_provider = loader.set_meter_calls[0]
        reader = meter_provider.metric_readers[0]
        assert reader.kwargs["export_interval_millis"] == 15000.0
        assert reader.kwargs["export_timeout_millis"] == 3000.0

        logger_provider = loader.set_logger_calls[0]
        log_processors = [
            p for p in logger_provider.processors if isinstance(p, FakeBatchProcessor)
        ]
        assert len(log_processors) == 1
        assert log_processors[0].kwargs["max_queue_size"] == 2048
        assert log_processors[0].kwargs["max_export_batch_size"] == 512

        # delivery unknown before any export
        assert snap["signals"]["traces"]["delivery"] == "unknown"
        assert snap["signals"]["metrics"]["delivery"] == "unknown"
        assert snap["signals"]["logs"]["delivery"] == "unknown"
        assert snap["signals"]["profile"]["status"] == "disabled"

    def test_https_endpoint_is_secure(self):
        loader = make_enabled_loader()
        make_handle(
            loader,
            otel_exporter_otlp_endpoint="https://collector.example.com:4317",
        )
        for exporter in FakeExporter.instances:
            assert exporter.kwargs["insecure"] is False

    def test_idempotent_configure_reuses_handle(self):
        loader = make_enabled_loader()
        handle1 = make_handle(
            loader, otel_exporter_otlp_endpoint="http://otel-collector:4317"
        )
        handle2 = setup.configure_observability(
            _settings(
                otel_enabled=True,
                otel_exporter_otlp_endpoint="http://otel-collector:4317",
            ),
            sdk_loader=loader,
        )
        assert handle2 is handle1
        # No second assembly
        assert len(loader.set_tracer_calls) == 1
        assert len([e for e in FakeExporter.instances if isinstance(e, FakeSpanExporter)]) == 1

    def test_fresh_api_proxies_publish_mechanism(self):
        # get_tracer/get_meter return fresh API proxies both before and
        # after configure; configure publishes providers through the
        # loader's setters, which is what makes the proxies resolve.
        before = ts.get_tracer()
        assert type(before).__name__ == "ProxyTracer"
        loader = make_enabled_loader()
        make_handle(loader, otel_exporter_otlp_endpoint="http://otel-collector:4317")
        assert len(loader.set_tracer_calls) == 1
        assert len(loader.set_meter_calls) == 1
        after = ts.get_tracer()
        assert type(after).__name__ == "ProxyTracer"
        assert after is not before


# ---------------------------------------------------------------------------
# Partial failure / ownership
# ---------------------------------------------------------------------------


class TestFailureAndOwnership:
    def test_partial_failure_rolls_back(self):
        loader = FakeLoader(metric_exporter_cls=FailingMetricExporter)
        handle = make_handle(
            loader, otel_exporter_otlp_endpoint="http://otel-collector:4317"
        )
        assert handle.status == "degraded"
        snap = handle.snapshot()
        assert snap["status"] == "degraded"
        # nothing published
        assert loader.set_tracer_calls == []
        assert loader.set_meter_calls == []
        assert loader.set_logger_calls == []
        # already-constructed tracer provider was cleaned up
        built = list(FakeProvider.instances)
        assert built, "tracer provider should have been constructed before metrics"
        assert any(
            "shutdown" in [ev[0] for ev in p.events] for p in built
        ), "constructed providers must be shut down during rollback"

    def test_partial_failure_allows_retry(self):
        loader = FakeLoader(metric_exporter_cls=FailingMetricExporter)
        make_handle(loader, otel_exporter_otlp_endpoint="http://otel-collector:4317")
        FakeExporter.instances = []
        good = make_enabled_loader()
        handle = make_handle(
            good, otel_exporter_otlp_endpoint="http://otel-collector:4317"
        )
        assert handle.status == "healthy"
        assert len(good.set_tracer_calls) == 1

    def test_degraded_published_handle_is_cached(self):
        # A degraded handle that still published its global providers must
        # be cached: OTel globals are irreversible, so a retry would
        # re-assemble (new providers/worker threads) and leak the first
        # set. Includes profiling_enabled -- a supported config that is
        # degraded until the profiler (T7) attaches.
        loader = FakeLoader(claimed_tracer=True)
        settings = dict(
            otel_exporter_otlp_endpoint="http://otel-collector:4317",
            profiling_enabled=True,
            profiling_pyroscope_endpoint="http://pyroscope:4040",
        )
        handle = make_handle(loader, **settings)
        assert handle.status == "degraded"
        assert handle._published is True
        providers_after_first = len(FakeProvider.instances)

        handle2 = setup.configure_observability(
            _settings(otel_enabled=True, **settings), sdk_loader=loader
        )
        assert handle2 is handle
        # no re-assembly happened
        assert len(FakeProvider.instances) == providers_after_first
        assert len(loader.set_meter_calls) == 1
        assert len(loader.set_logger_calls) == 1

    def test_existing_global_provider_not_replaced(self):
        loader = FakeLoader(claimed_tracer=True)
        handle = make_handle(
            loader, otel_exporter_otlp_endpoint="http://otel-collector:4317"
        )
        # the external provider was not replaced
        assert loader.set_tracer_calls == []
        assert loader.set_meter_calls  # other signals still assembled
        snap = handle.snapshot()
        assert snap["status"] == "degraded"
        assert snap["signals"]["traces"]["status"] == "degraded"
        assert snap["signals"]["metrics"]["status"] == "healthy"


# ---------------------------------------------------------------------------
# Local-only
# ---------------------------------------------------------------------------


class TestLocalOnly:
    def test_no_network_exporter_bounded_metric_reader(self):
        loader = make_enabled_loader()
        handle = make_handle(loader, otel_exporter_otlp_endpoint="")
        assert handle.status == "healthy"
        snap = handle.snapshot()
        assert snap["mode"] == "local_only"
        assert FakeExporter.instances == []  # no network exporter at all
        assert len(loader.set_tracer_calls) == 1
        meter_provider = loader.set_meter_calls[0]
        reader = meter_provider.metric_readers[0]
        assert isinstance(reader, FakeMetricReader)
        assert not isinstance(reader.exporter, FakeExporter)
        # bounded local aggregation reader
        assert hasattr(reader.exporter, "export")
        for signal in snap["signals"].values():
            assert signal["delivery"] == "disabled"
        # no LoggerProvider in local-only mode (logs stay on app stderr)
        assert loader.set_logger_calls == []
        assert snap["signals"]["logs"]["status"] == "disabled"


# ---------------------------------------------------------------------------
# Snapshot contract
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_pure_json_contract(self):
        loader = make_enabled_loader()
        handle = make_handle(
            loader, otel_exporter_otlp_endpoint="http://otel-collector:4317"
        )
        snap = handle.snapshot()
        # pure JSON values only
        assert json.loads(json.dumps(snap)) == snap
        assert snap["status"] == "healthy"
        assert snap["mode"] == "otlp"
        assert snap["instance_id"]
        assert snap["sample_ratio"] == 1.0
        assert snap["semconv_version"] == ts.SEMCONV_VERSION
        names = [i["name"] for i in snap["instruments"]]
        assert len(names) == 12
        assert "gen_ai.client.inference.duration" in names
        assert "nagent.turns" in names

    def test_span_buffer_bounds_and_caps(self):
        loader = make_enabled_loader()
        handle = make_handle(
            loader, otel_exporter_otlp_endpoint="http://otel-collector:4317"
        )
        ring = ring_of(handle)
        long_name = "x" * 500
        attrs = {
            ts.GEN_AI_REQUEST_MODEL: "gpt-4o",
            "http.url": "http://secret.example.com/?q=leak",  # not whitelisted
            "sensitive.free.text": "y" * 500,  # not whitelisted
        }
        for i in range(120):
            ring.on_end(make_ended_span(name=long_name if i == 0 else f"op-{i}",
                                        attributes=attrs))
        snap = handle.snapshot()
        spans = snap["spans"]
        assert len(spans) == 100
        for entry in spans:
            for key, value in entry.items():
                if isinstance(value, str):
                    assert len(value) <= 256
        entry = spans[0]
        assert set(entry.keys()) == {
            "name", "trace_id", "span_id", "parent_span_id",
            "start_time", "end_time", "duration_ms", "status", "attributes",
        }
        assert entry["trace_id"] == "11111111111111111111111111111111"
        assert entry["attributes"] == {ts.GEN_AI_REQUEST_MODEL: "gpt-4o"}

    def test_unsampled_span_not_buffered(self):
        loader = make_enabled_loader()
        handle = make_handle(
            loader, otel_exporter_otlp_endpoint="http://otel-collector:4317"
        )
        ring = ring_of(handle)
        ring.on_end(make_ended_span(name="sampled", sampled=True))
        ring.on_end(make_ended_span(name="unsampled", sampled=False))
        snap = handle.snapshot()
        assert [s["name"] for s in snap["spans"]] == ["sampled"]


# ---------------------------------------------------------------------------
# Delivery status wrapper
# ---------------------------------------------------------------------------


class TestDeliveryStatus:
    def test_wrapper_records_success_failure_recovery(self):
        loader = make_enabled_loader()
        handle = make_handle(
            loader, otel_exporter_otlp_endpoint="http://otel-collector:4317"
        )
        tracer_provider = loader.set_tracer_calls[0]
        bsp = next(
            p for p in tracer_provider.processors if isinstance(p, FakeBatchProcessor)
        )
        wrapper = bsp.exporter
        assert isinstance(wrapper, setup._DeliveryStatusWrapper)

        wrapper.export([object()])
        snap = handle.snapshot()
        assert snap["signals"]["traces"]["delivery"] == "succeeded"
        assert snap["signals"]["traces"]["failure_count"] == 0
        assert snap["signals"]["traces"]["last_success"]

        wrapper.exporter.fail_exports = True
        wrapper.export([object()])
        snap = handle.snapshot()
        assert snap["signals"]["traces"]["delivery"] == "failed"
        assert snap["signals"]["traces"]["failure_count"] == 1
        assert snap["signals"]["traces"]["last_failure"]
        assert snap["signals"]["traces"]["current_error"]

        wrapper.exporter.fail_exports = False
        wrapper.export([object()])
        snap = handle.snapshot()
        # success clears the current transport fault but not the counter
        assert snap["signals"]["traces"]["delivery"] == "succeeded"
        assert snap["signals"]["traces"]["failure_count"] == 1
        assert snap["signals"]["traces"]["current_error"] is None

    def test_wrapper_exporter_exception_is_failure(self):
        loader = make_enabled_loader()
        handle = make_handle(
            loader, otel_exporter_otlp_endpoint="http://otel-collector:4317"
        )
        tracer_provider = loader.set_tracer_calls[0]
        bsp = next(
            p for p in tracer_provider.processors if isinstance(p, FakeBatchProcessor)
        )
        wrapper = bsp.exporter

        def boom(*args, **kwargs):
            raise RuntimeError("network down")

        wrapper.exporter.export = boom
        wrapper.export([object()])
        snap = handle.snapshot()
        assert snap["signals"]["traces"]["delivery"] == "failed"
        assert snap["signals"]["traces"]["failure_count"] == 1

    def test_force_flush_and_shutdown_never_propagate(self):
        loader = make_enabled_loader()
        handle = make_handle(
            loader, otel_exporter_otlp_endpoint="http://otel-collector:4317"
        )
        tracer_provider = loader.set_tracer_calls[0]
        bsp = next(
            p for p in tracer_provider.processors if isinstance(p, FakeBatchProcessor)
        )
        wrapper = bsp.exporter

        def boom(*args, **kwargs):
            raise RuntimeError("flush/shutdown blew up")

        wrapper.exporter.force_flush = boom
        wrapper.exporter.shutdown = boom
        assert wrapper.force_flush(timeout_millis=100) is False
        wrapper.shutdown()  # must not raise
        snap = handle.snapshot()
        assert snap["signals"]["traces"]["delivery"] == "failed"
        assert snap["signals"]["traces"]["failure_count"] == 2
        assert snap["signals"]["traces"]["current_error"] == "export_shutdown_failed"


# ---------------------------------------------------------------------------
# Ring buffer processor contract
# ---------------------------------------------------------------------------


class TestRingBufferProcessor:
    def test_only_ended_sampled_spans(self):
        ring = setup._RingBufferSpanProcessor()
        ring.on_start(SimpleNamespace(), None)  # start never buffers
        assert ring.snapshot_spans() == []
        ring.on_end(make_ended_span(name="ok"))
        assert len(ring.snapshot_spans()) == 1

    def test_concurrent_on_end_is_thread_safe(self):
        # The ring buffer's lock exists exactly for concurrent on_end from
        # SDK worker threads; stress it from many threads at once.
        ring = setup._RingBufferSpanProcessor()
        errors: list[BaseException] = []

        def worker(base: int) -> None:
            try:
                for i in range(200):
                    ring.on_end(
                        make_ended_span(name=f"t{base}-span-{i}", span_id=base * 1000 + i)
                    )
            except Exception as exc:  # pragma: no cover - failure evidence
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        entries = ring.snapshot_spans()
        assert len(entries) == 100
        # snapshot is a stable copy: further on_end does not mutate it
        ring.on_end(make_ended_span(name="late", span_id=999999))
        assert len(entries) == 100
        assert len(ring.snapshot_spans()) == 100


# ---------------------------------------------------------------------------
# Shutdown budget
# ---------------------------------------------------------------------------


class TestShutdown:
    def test_budget_order_and_drop(self, monkeypatch):
        monkeypatch.setattr(setup, "SHUTDOWN_BUDGET_SECONDS", 0.3)
        loader = make_enabled_loader()
        handle = make_handle(
            loader, otel_exporter_otlp_endpoint="http://otel-collector:4317"
        )
        order: list[str] = []
        handle.register_stop_callback(lambda: order.append("producer-stop"))

        tracer_provider = loader.set_tracer_calls[0]
        meter_provider = loader.set_meter_calls[0]
        tracer_provider.block_seconds = 2.0
        meter_provider.block_seconds = 2.0

        started = time.monotonic()
        handle.shutdown()
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, f"shutdown exceeded budget: {elapsed}"
        # producers stopped before any flush/shutdown of providers
        assert order == ["producer-stop"]
        with tracer_provider.event_lock:
            tracer_events = list(tracer_provider.events)
        assert tracer_events  # first provider got to flush
        with meter_provider.event_lock:
            meter_events = list(meter_provider.events)
        assert meter_events == []  # dropped after deadline
        assert handle.status == "degraded"

    def test_shutdown_repeat_safe(self):
        loader = make_enabled_loader()
        handle = make_handle(
            loader, otel_exporter_otlp_endpoint="http://otel-collector:4317"
        )
        handle.shutdown()
        handle.shutdown()  # must not raise

    def test_disabled_handle_shutdown_is_noop(self):
        handle = setup.configure_observability(_settings(otel_enabled=False))
        handle.shutdown()
        assert handle.snapshot()["status"] == "disabled"

    def test_register_stop_callback_after_shutdown_returns_false(self):
        loader = make_enabled_loader()
        handle = make_handle(
            loader, otel_exporter_otlp_endpoint="http://otel-collector:4317"
        )
        assert handle.register_stop_callback(lambda: None) is True
        handle.shutdown()
        # A late producer can no longer be stopped by this handle; the
        # registration result must say so explicitly instead of silently
        # no-op'ing.
        assert handle.register_stop_callback(lambda: None) is False
        # the late callback was not registered
        assert len(handle._stop_callbacks) == 1


# ---------------------------------------------------------------------------
# Sampler composition
# ---------------------------------------------------------------------------


class TestSampler:
    def _decision(self, sampler, parent_context=None):
        return sampler.should_sample(
            parent_context=parent_context,
            trace_id=0xABCDEF0123456789ABCDEF0123456789,
            name="invoke_agent n-agent",
        )

    def test_no_parent_uses_ratio(self):
        sampler = setup._build_sampler(1.0)
        assert self._decision(sampler).decision.value == 2  # RECORD_AND_SAMPLE
        sampler0 = setup._build_sampler(0.0)
        assert self._decision(sampler0).decision.value == 0  # DROP

    def test_remote_sampled_parent_inherited(self):
        sampler = setup._build_sampler(0.0)
        remote_ctx = SpanContext(
            trace_id=0xABCDEF0123456789ABCDEF0123456789,
            span_id=0x1234567890ABCDEF,
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        ctx = set_span_in_context(NonRecordingSpan(remote_ctx))
        assert self._decision(sampler, ctx).decision.value == 2

    def test_remote_unsampled_parent_inherited(self):
        sampler = setup._build_sampler(1.0)
        remote_ctx = SpanContext(
            trace_id=0xABCDEF0123456789ABCDEF0123456789,
            span_id=0x1234567890ABCDEF,
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.DEFAULT),
        )
        ctx = set_span_in_context(NonRecordingSpan(remote_ctx))
        assert self._decision(sampler, ctx).decision.value == 0


# ---------------------------------------------------------------------------
# Semantics registry parameterization
# ---------------------------------------------------------------------------


class TestSemanticsParameter:
    def test_semantics_module_is_parameterizable(self):
        stub = SimpleNamespace(
            SEMCONV_VERSION="stub-semconv-1",
            INSTRUMENTS={"nagent.turns": ts.INSTRUMENTS["nagent.turns"]},
        )
        handle = setup.configure_observability(
            _settings(otel_enabled=False), semantics=stub
        )
        snap = handle.snapshot()
        assert snap["semconv_version"] == "stub-semconv-1"
        assert [i["name"] for i in snap["instruments"]] == ["nagent.turns"]


# ---------------------------------------------------------------------------
# Real-SDK subprocess smoke (env-gated: NAGENT_OTEL_SDK_SMOKE=1)
#
# Fake loaders cannot catch real-SDK contract mismatches (proxy-name
# detection, _preferred_temporality delegation, SpanProcessor._on_ending,
# exporter shutdown kwargs). This smoke runs the default real loader in a
# subprocess so global providers stay isolated from the pytest process.
# ---------------------------------------------------------------------------


_SMOKE_SCRIPT = """
import json, time
from app.config import Settings
from app.application import telemetry_semantics as ts
from app.infrastructure.observability import setup

h = setup.configure_observability(Settings(_env_file=None, otel_enabled=True))
assert h.status == "healthy", h.status
assert h.snapshot()["mode"] == "local_only"
with ts.invoke_agent_span(session_id="smoke", source="cli", run_id="r1"):
    with ts.chat_span("gpt-4o", provider="openai"):
        pass
snap = h.snapshot()
names = [s["name"] for s in snap["spans"]]
assert names == ["chat gpt-4o", "invoke_agent n-agent"], names
ts.record_turn("cli", "completed")
t0 = time.monotonic()
h.shutdown()
dt = time.monotonic() - t0
assert dt < 5.0, dt
final = h.snapshot()
assert final["signals"]["metrics"].get("local_instruments") == ["nagent.turns"], final
print("SMOKE_OK", json.dumps({"spans": names, "shutdown_seconds": round(dt, 3)}))
"""


class TestRealSdkSmoke:
    def test_real_sdk_local_only_roundtrip(self):
        if not os.environ.get("NGAGENT_OTEL_SDK_SMOKE"):
            pytest.skip("set NAGENT_OTEL_SDK_SMOKE=1 to run the real-SDK smoke")
        repo_root = Path(__file__).resolve().parents[3]
        proc = subprocess.run(
            [sys.executable, "-c", _SMOKE_SCRIPT],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(repo_root),
        )
        assert proc.returncode == 0, (
            f"real-SDK smoke failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
        marker_line = next(
            line for line in proc.stdout.splitlines() if line.startswith("SMOKE_OK")
        )
        payload = json.loads(marker_line[len("SMOKE_OK"):])
        assert payload["spans"] == ["chat gpt-4o", "invoke_agent n-agent"]
        assert payload["shutdown_seconds"] < 5.0


class TestSystemMetricsWiring:
    """T-refine: system metrics start after publish, annotated on metrics extra."""

    def _settings_endpoint(self):
        return {"otel_exporter_otlp_endpoint": "http://collector:4317"}

    def test_started_after_publish_with_meter_provider(self, monkeypatch):
        seen = []

        def fake_instrument(mp):
            seen.append(mp)
            return True

        monkeypatch.setattr(
            "app.infrastructure.observability.instrumentors"
            ".instrument_system_metrics",
            fake_instrument,
        )
        loader = make_enabled_loader()
        handle = make_handle(loader, **self._settings_endpoint())
        assert handle._published is True
        # Called exactly once with the meter provider this assembly created.
        assert len(seen) == 1
        assert isinstance(seen[0], FakeProvider)
        metrics = handle.snapshot()["signals"]["metrics"]
        assert metrics["system_metrics"] == "started"

    def test_failure_annotated_not_degrading(self, monkeypatch):
        def boom(mp):
            raise RuntimeError("psutil missing")

        monkeypatch.setattr(
            "app.infrastructure.observability.instrumentors"
            ".instrument_system_metrics",
            boom,
        )
        loader = make_enabled_loader()
        handle = make_handle(loader, **self._settings_endpoint())
        metrics = handle.snapshot()["signals"]["metrics"]
        # Auxiliary failure: annotated on extra, never degrades the signal.
        assert metrics["system_metrics"] == "failed:RuntimeError"
        assert metrics["status"] == "healthy"

    def test_disabled_mode_skips_system_metrics(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            "app.infrastructure.observability.instrumentors"
            ".instrument_system_metrics",
            lambda mp: seen.append(mp) or True,
        )
        handle = make_handle(sdk_loader=make_enabled_loader(), otel_enabled=False)
        assert handle.mode == "disabled"
        assert seen == []
        assert "system_metrics" not in handle.snapshot()["signals"]["metrics"]
