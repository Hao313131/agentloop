"""Request IDs, structured access logs and the unhandled-error contract.

Every request gets an ID (the caller's ``X-Request-ID`` if it is a sane token, otherwise a
new one). It is returned in the response header, attached to every log line, stored in
the agent run's state, and included in 500 responses, so a user reporting "it broke" can
quote one ID that leads straight to the logs and the trace.

Logs are one JSON object per line (LOOPDESK_LOG_FORMAT=json, the default) so they can be
shipped to ELK / Loki / Cloud Logging without parsing rules.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from contextvars import ContextVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
tenant_var: ContextVar[int | None] = ContextVar("tenant_id", default=None)
user_var: ContextVar[int | None] = ContextVar("user_id", default=None)
# Who the request is for, filled in by authentication. Sync dependencies run in a worker
# thread on a copy of the context, so a ContextVar they set never reaches the middleware's
# access log line; a mutable object created by the middleware is shared by both copies.
request_scope_var: ContextVar[dict | None] = ContextVar("request_scope", default=None)


def identify(tenant_id: int, user_id: int) -> None:
    """Record the authenticated tenant and user for this request's log lines."""
    tenant_var.set(tenant_id)
    user_var.set(user_id)
    scope = request_scope_var.get()
    if scope is not None:
        scope.update(tenant_id=tenant_id, user_id=user_id)

# Baseline browser hardening for the API and the bundled console. The CSP allows only
# same-origin scripts, styles (inline style attributes are used by React), images (plus
# data: for the inline favicon) and XHR/fetch. HSTS belongs at the TLS terminator.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; "
        "form-action 'self'"
    ),
}

logger = logging.getLogger("loopdesk")
access_logger = logging.getLogger("loopdesk.access")
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
            "tenant_id": tenant_var.get() or (request_scope_var.get() or {}).get("tenant_id"),
            "user_id": user_var.get() or (request_scope_var.get() or {}).get("user_id"),
        }
        for key in ("method", "path", "status", "latency_ms", "run_id", "audit_anchor"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(fmt: str = "json", level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if fmt == "json" else logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger("loopdesk")
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    root.propagate = False


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if _SAFE_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        tenant_token, user_token = tenant_var.set(None), user_var.set(None)
        scope_token = request_scope_var.set({"tenant_id": None, "user_id": None})
        started = time.perf_counter()
        status = 500
        try:
            try:
                response = await call_next(request)
            except Exception:
                # Handled here, while the request id is still in context (an app-level
                # exception handler runs outside this middleware, after the reset).
                logger.exception("unhandled error on %s %s", request.method, request.url.path)
                response = _error_response(request_id)
            status = response.status_code
            response.headers["X-Request-ID"] = request_id
            # Time inside the app (for a stream: until the first byte). Client latency minus
            # this is network, queueing and the HTTP server: the load benchmark reports both.
            response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - started) * 1000:.1f}"
            for header, value in SECURITY_HEADERS.items():
                response.headers.setdefault(header, value)
            return response
        finally:
            if request.url.path.startswith("/api"):
                access_logger.info(
                    "request", extra={"method": request.method, "path": request.url.path, "status": status,
                                      "latency_ms": int((time.perf_counter() - started) * 1000)},
                )
            request_id_var.reset(token)
            tenant_var.reset(tenant_token)
            user_var.reset(user_token)
            request_scope_var.reset(scope_token)


def _error_response(request_id: str) -> JSONResponse:
    return JSONResponse(
        {"detail": "Something went wrong on our side. Please try again; quote request_id if you need it "
                   "looked into.", "request_id": request_id},
        status_code=500,
    )


_FIELD_LABELS = {
    "content": "message", "email": "email", "password": "password", "correction": "correction",
    "comment": "comment", "note": "note", "query": "query", "title": "title", "question": "question",
}


def _validation_message(error: dict) -> str:
    field = next((p for p in reversed(error.get("loc", ())) if isinstance(p, str) and p != "body"), "")
    label = _FIELD_LABELS.get(field, field or "request body")
    kind, ctx = error.get("type", ""), error.get("ctx") or {}
    if kind == "missing":
        return f"{label} is required"
    if kind == "string_too_long":
        return f"{label} must be at most {ctx.get('max_length')} characters"
    if kind == "string_too_short":
        return f"{label} must not be empty" if ctx.get("min_length") == 1 \
            else f"{label} must be at least {ctx.get('min_length')} characters"
    if kind in ("greater_than_equal", "greater_than"):
        return f"{label} must not be less than {ctx.get('ge', ctx.get('gt'))}"
    if kind in ("less_than_equal", "less_than"):
        return f"{label} must not be greater than {ctx.get('le', ctx.get('lt'))}"
    if kind == "json_invalid":
        return "the request body is not valid JSON"
    return f"{label} has the wrong format"


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Readable messages, and no echo of the input: FastAPI's default 422 body repeats
    the rejected value, i.e. a 2,000-character message, or a password that was too long."""
    errors = [{"field": ".".join(str(p) for p in e.get("loc", ()) if p != "body"), "type": e.get("type"),
               "message": _validation_message(e)} for e in exc.errors()]
    return JSONResponse({"detail": "; ".join(dict.fromkeys(e["message"] for e in errors)), "errors": errors,
                         "request_id": request_id_var.get()}, status_code=422)


def install(app: FastAPI) -> None:
    app.add_middleware(RequestContextMiddleware)
    app.add_exception_handler(RequestValidationError, _validation_error)
