"""Observe consumed HTTP bytes without reading ahead or changing business IO."""
from __future__ import annotations

import json
import asyncio
import zlib
from email.parser import BytesParser
from email.policy import default as email_policy
from urllib.parse import parse_qs

import httpx
from opentelemetry import trace

from . import logging as logs

_content_limit = 2048


def configure_capture_limit(max_bytes):
    global _content_limit
    _content_limit = max_bytes


class Body:
    def __init__(self, headers):
        self.headers = dict(headers)
        self.data = bytearray()
        self.observed = 0
        self.overflow = False
        self.complete = False
        encoding = self.headers.get("content-encoding", "").lower()
        self.decoder = zlib.decompressobj(31 if encoding == "gzip" else 15) if encoding in {"gzip", "deflate"} else None

    def append(self, chunk):
        self.observed += len(chunk)
        remaining = 4 * _content_limit - len(self.data)
        if remaining <= 0:
            self.overflow = True
            return
        try:
            value = self.decoder.decompress(chunk, remaining + 1) if self.decoder else chunk
            self.data.extend(value[:remaining])
            self.overflow |= len(value) > remaining
        except Exception:
            self.overflow = True

    def value(self):
        content_type = self.headers.get("content-type", "").lower()
        if content_type.startswith(("image/", "audio/", "video/", "application/octet-stream")):
            return {"type": "binary", "bytes": self.observed}
        text = bytes(self.data).decode("utf-8", errors="replace")
        if not self.complete or self.overflow:
            text = logs._sanitize_partial(text)
        if content_type.startswith("multipart/"):
            message = BytesParser(policy=email_policy).parsebytes(
                b"Content-Type: " + self.headers.get("content-type", "").encode("latin1")
                + b"\r\nMIME-Version: 1.0\r\n\r\n" + bytes(self.data))
            fields = {}
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition") or "part"
                raw = part.get_payload(decode=True) or b""
                fields[name] = {"filename": part.get_filename(), "bytes": len(raw),
                                "content_type": part.get_content_type()} if part.get_filename() else raw.decode("utf-8", errors="replace")
            return fields
        try:
            return json.loads(text)
        except ValueError:
            if "application/x-www-form-urlencoded" in content_type:
                return parse_qs(text, keep_blank_values=True)
            return text


def _query(url):
    return parse_qs(url.query.decode("utf-8", errors="replace"), keep_blank_values=True)


class Exchange:
    def __init__(self, span, request=None, scope=None):
        self.span = span
        self.ready = False
        self.end_requested = False
        self.projected = False
        self.error = None
        self.request_headers = dict(request.headers) if request is not None else {
            k.decode("latin1"): v.decode("latin1") for k, v in scope.get("headers", [])}
        self.request = Body(self.request_headers)
        self.response = Body({})
        self.query = _query(request.url) if request is not None else parse_qs(
            scope.get("query_string", b"").decode("utf-8", errors="replace"), keep_blank_values=True)
        self.context = logs._safe_execution_context()
        if request is not None:
            try:
                self.request.append(request.content)
                self.request.complete = True
            except httpx.RequestNotRead:
                pass

    def finish(self, *, complete=False, error=None):
        if self.ready:
            return
        self.response.complete = complete
        self.error = error
        self.ready = True
        if self.end_requested:
            self.span.end()

    def project(self):
        if self.projected:
            return
        self.projected = True
        fields = {
            "http_request": {"body": self.request.value(), "query": self.query, "headers": self.request_headers},
            "http_response": {"body": self.response.value(), "headers": self.response.headers},
        }
        if self.error is not None:
            fields["error"] = str(self.error)
        result = logs.project_observation_details(**fields)
        if not result:
            self.span._http_attributes["nagent.observation.error"] = "details_unavailable"
        for key, body in (("http_request", self.request), ("http_response", self.response)):
            if key in result:
                result[key]["truncated"] |= body.overflow
                result[key]["observed_bytes"] = body.observed
                if body.overflow or not body.complete:
                    result[key]["original_bytes"] = None
                if not body.complete:
                    result[key]["incomplete"] = True
        self.span._http_details = result
        self.span._http_incomplete = not self.response.complete and self.error is None
        self.span._http_context = self.context
        self.span._http_capture = None
        # Keep only the bounded sanitized projection, release raw byte buffers.
        self.request.data.clear()
        self.response.data.clear()
        self.request_headers.clear()
        self.request.headers.clear()
        self.response.headers.clear()
        self.query.clear()


class SyncStream(httpx.SyncByteStream):
    def __init__(self, stream, body, exchange=None):
        self.stream, self.body, self.exchange = stream, body, exchange

    def __iter__(self):
        try:
            for chunk in self.stream:
                self.body.append(chunk)
                yield chunk
            self.body.complete = True
            if self.exchange:
                self.exchange.finish(complete=True)
        except BaseException as exc:
            if self.exchange:
                self.exchange.span._http_error = type(exc).__name__
                self.exchange.span._http_cancelled = isinstance(exc, asyncio.CancelledError)
                self.exchange.finish(error=exc)
            raise

    def close(self):
        try:
            self.stream.close()
        except BaseException as exc:
            if self.exchange:
                self.exchange.span._http_error = type(exc).__name__
                self.exchange.finish(error=exc)
            raise
        finally:
            if self.exchange:
                self.exchange.finish()


class AsyncStream(httpx.AsyncByteStream):
    def __init__(self, stream, body, exchange=None):
        self.stream, self.body, self.exchange = stream, body, exchange

    async def __aiter__(self):
        try:
            async for chunk in self.stream:
                self.body.append(chunk)
                yield chunk
            self.body.complete = True
            if self.exchange:
                self.exchange.finish(complete=True)
        except BaseException as exc:
            if self.exchange:
                self.exchange.span._http_error = type(exc).__name__
                self.exchange.span._http_cancelled = isinstance(exc, asyncio.CancelledError)
                self.exchange.finish(error=exc)
            raise

    async def aclose(self):
        try:
            await self.stream.aclose()
        except BaseException as exc:
            if self.exchange:
                self.exchange.span._http_error = type(exc).__name__
                self.exchange.finish(error=exc)
            raise
        finally:
            if self.exchange:
                self.exchange.finish()


def _start(args, kwargs):
    span = trace.get_current_span()
    if logs._details_projector is None or not getattr(span, "_http_client", False):
        return None, None
    request = args[0] if args else kwargs["request"]
    exchange = Exchange(span, request=request)
    span._http_capture = exchange
    return request, exchange


def _response(response, exchange, stream_type):
    exchange.response = Body(response.headers)
    if response.is_stream_consumed:
        exchange.response.decoder = None  # Response.content is already decoded.
        exchange.response.append(response.content)
        exchange.finish(complete=True)
    else:
        response.stream = stream_type(response.stream, exchange.response, exchange)
    return response


def wrap_sync_transport(wrapped, instance, args, kwargs):
    request, exchange = _start(args, kwargs)
    if exchange is None:
        return wrapped(*args, **kwargs)
    original = request.stream
    if not exchange.request.complete:
        request.stream = SyncStream(original, exchange.request)
    try:
        return _response(wrapped(*args, **kwargs), exchange, SyncStream)
    except BaseException as exc:
        exchange.finish(error=exc)
        raise
    finally:
        request.stream = original


async def wrap_async_transport(wrapped, instance, args, kwargs):
    request, exchange = _start(args, kwargs)
    if exchange is None:
        return await wrapped(*args, **kwargs)
    original = request.stream
    if not exchange.request.complete:
        request.stream = AsyncStream(original, exchange.request)
    try:
        return _response(await wrapped(*args, **kwargs), exchange, AsyncStream)
    except BaseException as exc:
        exchange.finish(error=exc)
        raise
    finally:
        request.stream = original
