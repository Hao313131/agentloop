from __future__ import annotations

import hmac
import json

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse

from ..channels.whatsapp import deduper, parse_text_events, process_event, verify_signature
from ..db import new_session
from ..services import get_services

router = APIRouter(prefix="/api/channels", tags=["channels"])


def _accept(event_id: str, payload: dict) -> bool:
    with new_session() as db:
        return deduper.first_time(db, "whatsapp", event_id, payload)


@router.get("/whatsapp/{tenant_slug}/events")
async def whatsapp_verify(tenant_slug: str, request: Request):
    """Meta's subscription handshake: the challenge is echoed only when the verify token is
    this tenant's, so one tenant cannot subscribe another tenant's number to its webhook."""
    params = request.query_params
    app = get_services().settings.whatsapp_apps.get(tenant_slug)
    if app is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no WhatsApp channel is configured for this tenant")
    token = str(params.get("hub.verify_token") or "")
    if params.get("hub.mode") != "subscribe" or not hmac.compare_digest(token, app.verify_token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "verification failed")
    return PlainTextResponse(str(params.get("hub.challenge") or ""))


@router.post("/whatsapp/{tenant_slug}/events")
async def whatsapp_events(tenant_slug: str, request: Request, background: BackgroundTasks) -> dict:
    raw = await request.body()
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "request body must be a JSON object")
    settings = get_services().settings
    app = settings.whatsapp_apps.get(tenant_slug)
    if app is None and not settings.seed_demo:
        # Fail closed: without this tenant's number anyone who knows the URL could post fake
        # customer messages. The demo accepts unsigned events so the channel can be tried locally.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the WhatsApp channel is not configured for this tenant")
    if app is not None and not verify_signature(raw, request.headers.get("X-Hub-Signature-256"), app.app_secret):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "signature mismatch")

    accepted = 0
    for event in parse_text_events(body):
        if app is not None and event["phone_number_id"] and event["phone_number_id"] != app.phone_number_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "event belongs to another tenant's business number")
        # Stored before the acknowledgement, so a crash after it loses nothing (see retry_stuck).
        if await run_in_threadpool(_accept, event["event_id"], {**event, "tenant": tenant_slug}):
            # Meta wants a 200 quickly; the agent runs after we acknowledge.
            background.add_task(process_event, get_services(), event["event_id"])
            accepted += 1
    return {"accepted": accepted}
