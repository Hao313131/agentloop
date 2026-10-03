"""WhatsApp Business (Meta Cloud API) channel.

Inbound: Meta posts to /api/channels/whatsapp/{tenant}/events.
- ``GET``: the one-off subscription handshake (hub.mode / hub.verify_token / hub.challenge);
- ``POST``: message events. The payload may carry several messages, so each one is stored,
  acknowledged with 200 immediately, and answered in the background. Meta re-sends events it
  did not see acknowledged, so message IDs are de-duplicated in the database, where every
  worker process sees them. The stored event is also an inbox: a worker claims it before
  answering, and if that worker dies after the acknowledgement (Meta will not resend),
  ``retry_stuck`` (run every minute by ``python -m app.ops.channel_retry``) claims it again.
Outbound: the tenant's own business number, via the Graph API ``/{phone_number_id}/messages``.

Each tenant has its own app (LOOPDESK_WHATSAPP_APPS): the receiving business number in the
payload decides the tenant, and replies go out from that number. When an app secret is
configured, the ``X-Hub-Signature-256`` header is verified against the raw body, so a forged
POST cannot inject customer messages. A tenant without an app gets dry-run replies (returned,
not sent). Failures inside the background task are logged and answered with a fallback reply,
because FastAPI's BackgroundTasks would otherwise swallow them silently.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..agent.runtime import MODEL_ERROR_TEXT, collect
from ..config import Settings
from ..db import new_session
from ..models import ChannelEvent, Conversation, Tenant, User
from ..security import hash_password

API = "https://graph.facebook.com/v21.0"
logger = logging.getLogger(__name__)


class EventDeduper:
    """Accepted event IDs, kept in the channel_events table for a day. An in-memory set
    only knows the events its own process received: with several workers, or after a
    restart, a re-sent event was answered twice. One insert-if-absent decides, so of two
    deliveries arriving at the same time exactly one goes ahead."""

    retention = timedelta(days=1)

    def __init__(self) -> None:
        self._swept: datetime | None = None  # last time this process deleted expired IDs

    def first_time(self, db: Session, channel: str, event_id: str, payload: dict | None = None) -> bool:
        """Store the event (with what is needed to answer it) unless it is already stored."""
        now = datetime.now(timezone.utc)
        insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
        first = db.execute(
            insert(ChannelEvent).values(channel=channel, event_id=event_id, received_at=now,
                                        status="received", payload=payload)
            .on_conflict_do_nothing(index_elements=[ChannelEvent.channel, ChannelEvent.event_id])
            .returning(ChannelEvent.event_id)
        ).first() is not None
        if self._swept is None or now - self._swept > timedelta(hours=1):
            # Answered or given-up events only: an unanswered one is still work to do.
            db.execute(delete(ChannelEvent).where(ChannelEvent.received_at < now - self.retention,
                                                  ChannelEvent.status.in_(("done", "failed"))))
            self._swept = now
        db.commit()
        return first


deduper = EventDeduper()


class WhatsAppClient:
    """Sends as the tenant's own business number. The access token belongs to the tenant's
    app, so one tenant's reply can never go out from another tenant's number."""

    def __init__(self, settings: Settings, tenant_slug: str, transport: httpx.BaseTransport | None = None):
        self.app = settings.whatsapp_apps.get(tenant_slug)
        self.transport = transport

    @property
    def configured(self) -> bool:
        return self.app is not None

    def send_text(self, to: str, text: str, reply_to: str | None = None) -> dict:
        """Send a text message; ``reply_to`` quotes the customer's message in the chat."""
        body: dict = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": text},
        }
        if reply_to:
            body["context"] = {"message_id": reply_to}
        if not self.configured:
            return {"sent": False, "mode": "dry_run", "body": body}
        with httpx.Client(timeout=10.0, transport=self.transport) as client:
            response = client.post(
                f"{API}/{self.app.phone_number_id}/messages", json=body,
                headers={"Authorization": f"Bearer {self.app.access_token}"},
            )
            data = response.json()
        error = (data.get("error") or {}) if isinstance(data, dict) else {}
        return {"sent": bool(data.get("messages")) if isinstance(data, dict) else False,
                "mode": "live", "code": error.get("code"), "msg": error.get("message")}


def verify_signature(raw_body: bytes, header: str | None, app_secret: str | None) -> bool:
    """Meta signs the raw body with the app secret: ``sha256=<hex>`` in X-Hub-Signature-256.
    Without a configured secret (the local demo) the check is skipped."""
    if not app_secret:
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.removeprefix("sha256="), expected)


def _obj(value) -> dict:
    return value if isinstance(value, dict) else {}


def parse_text_events(body: dict) -> list[dict]:
    """Every text message in one webhook delivery. Malformed parts are ignored rather than
    raising: Meta would keep re-sending a 500. Status callbacks (delivered, read) carry no
    ``messages`` and are ignored by the same path."""
    if _obj(body).get("object") != "whatsapp_business_account":
        return []
    events: list[dict] = []
    for entry in body.get("entry") or []:
        for change in _obj(entry).get("changes") or []:
            value = _obj(_obj(change).get("value"))
            phone_number_id = str(_obj(value.get("metadata")).get("phone_number_id") or "")
            names = {str(_obj(c).get("wa_id") or ""): str(_obj(_obj(c).get("profile")).get("name") or "")
                     for c in value.get("contacts") or []}
            for message in value.get("messages") or []:
                message = _obj(message)
                if message.get("type") != "text":
                    continue
                text = str(_obj(message.get("text")).get("body") or "").strip()
                message_id = message.get("id")
                sender = str(message.get("from") or "")
                if not text or not sender or not isinstance(message_id, str) or not 0 < len(message_id) <= 128:
                    # Without an ID a re-sent copy cannot be recognised; Meta always sends one.
                    logger.warning("whatsapp message without a usable id ignored")
                    continue
                events.append({
                    "event_id": message_id,
                    "phone_number_id": phone_number_id,
                    "wa_id": sender,
                    "name": names.get(sender, ""),
                    "text": text,
                })
    return events


MAX_ATTEMPTS = 3
QUEUED_GRACE = timedelta(seconds=30)  # an accepted event's own background task normally starts sooner


def stale_after(settings: Settings) -> timedelta:
    """How long a claim may last before its worker counts as dead: twice the run deadline,
    at least five minutes, so a slow run is never answered twice."""
    return timedelta(seconds=max(300.0, 2 * settings.run_deadline_seconds))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _claim(db: Session, event_id: str, now: datetime, stale_before: datetime):
    """One conditional UPDATE: an unclaimed event, or one whose worker died. Of two workers
    (the event's own task and the sweeper, or two sweepers) exactly one gets it."""
    row = db.execute(
        update(ChannelEvent).where(
            ChannelEvent.channel == "whatsapp", ChannelEvent.event_id == event_id,
            ChannelEvent.attempts < MAX_ATTEMPTS,
            or_(ChannelEvent.status == "received",
                and_(ChannelEvent.status == "processing", ChannelEvent.claimed_at < stale_before)),
        ).values(status="processing", attempts=ChannelEvent.attempts + 1, claimed_at=now)
        .returning(ChannelEvent.payload).execution_options(synchronize_session=False)
    ).first()
    db.commit()
    return row


def _settle(event_id: str, status: str) -> None:
    with new_session() as db:
        db.execute(update(ChannelEvent).where(ChannelEvent.channel == "whatsapp", ChannelEvent.event_id == event_id)
                   .values(status=status).execution_options(synchronize_session=False))
        db.commit()


def process_event(services, event_id: str, now: datetime | None = None) -> dict | None:
    """Claim an accepted event and answer it; None when another worker has it. An event whose
    answer failed got the fallback reply (handle_message) and is settled as failed, not retried.
    ``now`` (a sweep's clock) only decides what counts as stale. The claim is stamped with the
    moment it is made: a sweep answering a backlog for minutes would otherwise back-date its
    later claims, and the next sweep would take them over while they are being answered."""
    now = now or _utcnow()
    with new_session() as db:
        claimed = _claim(db, event_id, _utcnow(), now - stale_after(services.settings))
    if claimed is None or not claimed.payload:
        return None
    payload = claimed.payload
    result = handle_message(services, payload["tenant"], payload)
    _settle(event_id, "failed" if result.get("fallback") else "done")
    return result


def retry_stuck(services, now: datetime | None = None) -> dict[str, int]:
    """Answer accepted events that no worker finished: never started (the process died right
    after the acknowledgement) or claimed by a worker that died. After MAX_ATTEMPTS the
    customer gets the fallback reply instead of silence. Business effects of a repeated run
    are idempotent (refund claim, handoff ticket, pending-approval dedup); the reply itself
    can repeat only if a worker dies between replying and settling."""
    now = now or datetime.now(timezone.utc)
    stale_before = now - stale_after(services.settings)
    stuck = or_(and_(ChannelEvent.status == "received", ChannelEvent.received_at < now - QUEUED_GRACE),
                and_(ChannelEvent.status == "processing", ChannelEvent.claimed_at < stale_before))
    with new_session() as db:
        rows = db.execute(select(ChannelEvent.event_id, ChannelEvent.attempts)
                          .where(ChannelEvent.channel == "whatsapp", stuck)).all()
    counts = {"answered": 0, "gave_up": 0}
    for event_id, attempts in rows:
        if attempts < MAX_ATTEMPTS:
            counts["answered"] += process_event(services, event_id, now) is not None
            continue
        with new_session() as db:  # give up once, even with several sweepers
            payload = db.execute(
                update(ChannelEvent).where(ChannelEvent.channel == "whatsapp", ChannelEvent.event_id == event_id, stuck)
                .values(status="failed").returning(ChannelEvent.payload).execution_options(synchronize_session=False)
            ).scalar_one_or_none()
            db.commit()
        if payload:
            logger.error("whatsapp event %s gave up after %d attempts", event_id, attempts)
            WhatsAppClient(services.settings, payload["tenant"]).send_text(
                payload["wa_id"], MODEL_ERROR_TEXT, reply_to=payload["event_id"])
            counts["gave_up"] += 1
    return counts


def handle_message(services, tenant_slug: str, parsed: dict) -> dict:
    """Run the agent for one WhatsApp message and reply. Executed as a background task:
    the HTTP response has already been sent, so an exception here would vanish without a
    trace and the customer would wait forever. Log it and send a fallback reply instead."""
    try:
        return _answer(services, tenant_slug, parsed)
    except Exception:
        logger.exception("whatsapp message %s for tenant %s failed", parsed.get("event_id"), tenant_slug)
        try:
            return {**WhatsAppClient(services.settings, tenant_slug).send_text(
                parsed["wa_id"], MODEL_ERROR_TEXT, reply_to=parsed["event_id"]), "fallback": True}
        except Exception:
            logger.exception("whatsapp fallback reply to %s failed", parsed.get("event_id"))
            return {"sent": False, "error": "fallback reply failed", "fallback": True}


def _get_or_create(db: Session, query, make):
    found = db.execute(query).scalar_one_or_none()
    if found is not None:
        return found
    db.add(make())
    try:
        db.commit()
    except IntegrityError:  # created concurrently by another worker: use theirs
        db.rollback()
    return db.execute(query).scalar_one()


def _answer(services, tenant_slug: str, parsed: dict) -> dict:
    with new_session() as db:
        tenant = db.execute(select(Tenant).where(Tenant.slug == tenant_slug)).scalar_one_or_none()
        if tenant is None or not parsed["text"]:
            return {"skipped": True}
        email = f"whatsapp_{parsed['wa_id']}@{tenant_slug}.channel".lower()
        # Get or create, both guarded by unique indexes: a customer's first two messages can be
        # answered at the same time (by two workers, or a worker and the retry sweeper).
        user = _get_or_create(db, select(User).where(User.email == email), lambda: User(
            tenant_id=tenant.id, email=email,
            name=parsed.get("name") or f"WhatsApp {parsed['wa_id'][-4:]}", role="customer",
            password_hash=hash_password(secrets.token_urlsafe(24))))
        conversation = _get_or_create(db, select(Conversation).where(
            Conversation.tenant_id == tenant.id, Conversation.channel == "whatsapp",
            Conversation.external_id == parsed["wa_id"], Conversation.user_id == user.id,
        ), lambda: Conversation(tenant_id=tenant.id, user_id=user.id, channel="whatsapp",
                                external_id=parsed["wa_id"], title="WhatsApp chat"))
        summary = collect(services.runtime(db).start(tenant=tenant, user=user, text=parsed["text"],
                                                     conversation=conversation))
        answer = (summary["final"] or {}).get("text") or MODEL_ERROR_TEXT
        return WhatsAppClient(services.settings, tenant_slug).send_text(
            parsed["wa_id"], answer, reply_to=parsed["event_id"])
