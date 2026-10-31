"""Observe the external-memory urllib transport using the shared HTTP contract."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager

import httpx
from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from . import logging as logs
from .http_capture import Body, Exchange
from .instrumentors import _FilteringTracer, _httpx_request_hook


class _Observation:
    def __init__(self, span=None, exchange=None):
        self.span, self.exchange = span, exchange

    def response(self, response):
        try:
            self.span.set_attribute("http.response.status_code", response.status)
            if self.exchange:
                self.exchange.response = Body({k.lower(): v for k, v in response.headers.items()})
        except Exception:
            pass

    def chunk(self, chunk):
        try:
            if self.exchange:
                self.exchange.response.append(chunk)
        except Exception:
            pass

    def finish(self, *, complete=False, error=None):
        try:
            if error is not None and self.span is not None:
                self.span._http_error = type(error).__name__[:64]
                self.span._http_cancelled = isinstance(error, asyncio.CancelledError)
                self.span.set_status(Status(StatusCode.ERROR))
                if isinstance(getattr(error, "code", None), int):
                    self.span.set_attribute("http.response.status_code", error.code)
            if self.exchange:
                self.exchange.finish(complete=complete, error=error)
        except Exception:
            pass


@contextmanager
def observe_urllib_request(request):
    """Capture only consumed bytes; observation failures never retry business IO."""
    observation = _Observation()
    span = None
    token = None
    try:
        if trace.get_current_span().get_span_context().is_valid:
            tracer = _FilteringTracer(trace.get_tracer_provider().get_tracer(__name__))
            span = tracer.start_span("HTTP", kind=SpanKind.CLIENT)
            # Use the same safe target/caller projection and details as HTTPX.
            projected = httpx.Request(request.get_method(), request.full_url,
                                      headers=dict(request.header_items()), content=request.data or b"")
            _httpx_request_hook(span, projected)
            from opentelemetry import context

            token = context.attach(trace.set_span_in_context(span))
            carrier = {}
            propagate.inject(carrier)
            for key, value in carrier.items():
                request.add_header(key, value)
            exchange = Exchange(span, request=projected) if logs._details_projector is not None else None
            if exchange:
                span._http_capture = exchange
            observation = _Observation(span, exchange)
    except Exception:
        pass
    error = None
    try:
        yield observation
    except BaseException as exc:
        error = exc
        raise
    finally:
        observation.finish(error=error)
        try:
            if span is not None:
                span.end()
        except Exception:
            pass
        finally:
            if token is not None:
                try:
                    from opentelemetry import context

                    context.detach(token)
                except Exception:
                    pass
