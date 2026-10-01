"""Demo data: two tenants, users, knowledge documents, orders and eval cases.

Everything here is SYNTHETIC. Run ``python -m app.seed --reset`` to rebuild the
local database from scratch.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .llm.base import Embedder
from .models import EvalCase, Order, Refund, Tenant, User
from .rag.ingest import ingest_document
from .security import hash_password

DEMO_PASSWORD = "loopdesk-demo"
BASE_DIR = Path(__file__).resolve().parent.parent
SEED_DIR = BASE_DIR / "seed_data"
EVAL_DIR = BASE_DIR / "evals"

TENANTS = [
    {
        "slug": "harbour", "name": "Harbour & Home", "industry": "ecommerce", "auto_refund_limit": 75.0,
        "ticket_sla_hours": 24, "return_window_days": 30, "quality_window_days": 90,
        "users": [
            ("admin@harbour.demo", "Charlotte", "admin"),
            ("agent@harbour.demo", "Mia", "agent"),
            ("customer@harbour.demo", "Emma", "customer"),
            ("customer2@harbour.demo", "Jack", "customer"),
        ],
        "documents": [
            ("refund_policy.md", "Returns and refunds", "public"),
            ("shipping.md", "Delivery and tracking", "public"),
            ("membership.md", "Membership and points", "public"),
            ("invoice.md", "Tax invoices", "public"),
            ("internal_handbook.md", "Support handbook", "internal"),
            ("supplier_faq.md", "Supplier FAQ", "public"),
        ],
    },
    {
        "slug": "rostr", "name": "Rostr", "industry": "saas", "auto_refund_limit": 0.0, "ticket_sla_hours": 8,
        "users": [
            ("admin@rostr.demo", "Olivia", "admin"),
            ("customer@rostr.demo", "Noah", "customer"),
        ],
        "documents": [
            ("product_faq.md", "Product FAQ", "public"),
            ("sla.md", "SLA and support tickets", "public"),
        ],
    },
]


# Order numbers are fixed strings (eval cases, tests and the docs refer to them), but every
# timestamp is relative to the moment the data is created: after-sales windows and refund
# velocity limits are judged against "now", and absolute dates would quietly age every
# order out of its window. The date inside an order number is therefore just a label.
_SYDNEY = ZoneInfo("Australia/Sydney")


def _ago(days: float = 0, hours: float = 0) -> datetime:
    return datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(days=days, hours=hours)


def _event(when: datetime, desc: str) -> dict:
    return {"time": when.astimezone(_SYDNEY).strftime("%Y-%m-%d %H:%M"), "desc": desc}


def _orders(customer: User, other: User) -> list[Order]:
    t = customer.tenant_id
    return [
        # Delivered 3 days ago, small amount: the automatic refund path.
        Order(id="SO20260918001", tenant_id=t, customer_id=customer.id, amount=39.9, status="delivered",
              items=[{"name": "Cloud fleece cushion", "qty": 1, "price": 39.9}],
              paid_at=_ago(5, 2), delivered_at=_ago(3),
              shipment={"carrier": "Australia Post", "tracking_no": "36LB4X8821901234", "events": [
                  _event(_ago(4, 16), "Picked up (Alexandria NSW)"),
                  _event(_ago(3), "Delivered — signed for by the recipient")]}),
        Order(id="SO20261008002", tenant_id=t, customer_id=customer.id, amount=1299.0, status="shipped",
              items=[{"name": "RoboVac X1 robot vacuum", "qty": 1, "price": 1299.0}],
              paid_at=_ago(2, 5),
              shipment={"carrier": "Sendle", "tracking_no": "SND1402938475", "events": [
                  _event(_ago(1, 20), "Picked up (Alexandria NSW)"),
                  _event(_ago(1, 8), "Arrived at the Sydney sorting hub"),
                  _event(_ago(0, 3), "On board for delivery")]}),
        Order(id="SO20261012003", tenant_id=t, customer_id=customer.id, amount=268.0, status="paid",
              items=[{"name": "Pour-over coffee kit", "qty": 1, "price": 268.0}], paid_at=_ago(0, 10)),
        Order(id="SO20260801004", tenant_id=t, customer_id=customer.id, amount=159.0, status="refunded",
              items=[{"name": "Insulated drink bottle", "qty": 2, "price": 79.5}], paid_at=_ago(80),
              delivered_at=_ago(78)),
        # Delivered 45 days ago: past the 30-day change-of-mind window, inside the 90-day faulty window.
        Order(id="SO20261008010", tenant_id=t, customer_id=customer.id, amount=32.0, status="delivered",
              items=[{"name": "Ceramic mug", "qty": 1, "price": 32.0}],
              paid_at=_ago(47, 3), delivered_at=_ago(45),
              shipment={"carrier": "Australia Post", "tracking_no": "36LB4X8821915530", "events": [
                  _event(_ago(46, 18), "Picked up (Alexandria NSW)"),
                  _event(_ago(45), "Delivered — left in a safe place")]}),
        # Delivered 120 days ago: outside every after-sales window.
        Order(id="SO20260918011", tenant_id=t, customer_id=customer.id, amount=36.0, status="delivered",
              items=[{"name": "Canvas tote bag", "qty": 1, "price": 36.0}],
              paid_at=_ago(122, 4), delivered_at=_ago(120),
              shipment={"carrier": "Australia Post", "tracking_no": "36LB4X8821900917", "events": [
                  _event(_ago(121, 20), "Picked up (Alexandria NSW)"),
                  _event(_ago(120), "Delivered — left in a safe place")]}),
        Order(id="SO20261003005", tenant_id=t, customer_id=other.id, amount=899.0, status="delivered",
              items=[{"name": "Noise-cancelling headphones Pro", "qty": 1, "price": 899.0}],
              paid_at=_ago(16, 12), delivered_at=_ago(14, 12),
              shipment={"carrier": "Sendle", "tracking_no": "SND1400011122", "events": [
                  _event(_ago(14, 12), "Delivered — signed for by the recipient")]}),
    ]


def fixture_orders(tenant_slug: str, users_by_email: dict[str, User]) -> list[Order]:
    """Deterministic business data. Also used by the eval sandbox, so evaluations never
    depend on whatever state the live orders are in."""
    if tenant_slug != "harbour":
        return []
    emma, jack = users_by_email["customer@harbour.demo"], users_by_email["customer2@harbour.demo"]
    t = emma.tenant_id
    # Jack's recent small orders; three of them were refunded (see fixture_refunds).
    small = [
        ("SO20261001006", "Fidget popper", 35.0, "refunded", 8),
        ("SO20261005007", "Desk organiser", 42.0, "refunded", 6),
        ("SO20261010008", "Portable fan", 28.0, "refunded", 4),
        ("SO20261015009", "Bluetooth mini speaker", 45.0, "delivered", 2),
    ]
    extra = [
        Order(id=oid, tenant_id=t, customer_id=jack.id, amount=amount, status=status,
              items=[{"name": name, "qty": 1, "price": amount}],
              paid_at=_ago(delivered_days_ago + 2), delivered_at=_ago(delivered_days_ago))
        for oid, name, amount, status, delivered_days_ago in small
    ]
    return _orders(emma, jack) + extra


def fixture_refunds(tenant_slug: str, tenant_id: int) -> list[Refund]:
    """Jack's three small refunds inside the 30-day velocity window. Timestamps are
    relative to now so the fixture never ages out of the window."""
    if tenant_slug != "harbour":
        return []
    now = datetime.now(timezone.utc)
    return [
        Refund(tenant_id=tenant_id, order_id=oid, amount=amount, reason="Changed my mind", status="processing",
               created_at=now - timedelta(days=days_ago))
        for oid, amount, days_ago in (("SO20261001006", 35.0, 6), ("SO20261005007", 42.0, 4), ("SO20261010008", 28.0, 2))
    ]


def load_eval_cases(db: Session, tenant: Tenant, path: Path, suite: str = "default") -> int:
    if not path.exists():
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        db.add(EvalCase(
            tenant_id=tenant.id, key=data["key"], suite=suite, question=data["question"],
            role=data.get("role", "customer"), as_user=data.get("as_user"),
            expected_tools=data.get("expected_tools", []), forbidden_tools=data.get("forbidden_tools", []),
            must_include=data.get("must_include", []), must_not_include=data.get("must_not_include", []),
            expect_refusal=data.get("expect_refusal"), expect_approval=data.get("expect_approval"),
            expected_doc=data.get("expected_doc"), tags=data.get("tags", []), history=data.get("history"),
            source="seed", status="active",
        ))
        count += 1
    db.commit()
    return count


def seed_demo(db: Session, embedder: Embedder) -> bool:
    if db.execute(select(Tenant.id).limit(1)).first():
        return False
    for spec in TENANTS:
        tenant = Tenant(slug=spec["slug"], name=spec["name"], industry=spec["industry"],
                        auto_refund_limit=spec["auto_refund_limit"], ticket_sla_hours=spec["ticket_sla_hours"],
                        **{k: spec[k] for k in ("return_window_days", "quality_window_days") if k in spec})
        db.add(tenant)
        db.flush()
        users = {}
        password_hash = hash_password(DEMO_PASSWORD)
        for email, name, role in spec["users"]:
            user = User(tenant_id=tenant.id, email=email, name=name, role=role, password_hash=password_hash)
            db.add(user)
            users[email] = user
        db.flush()
        for filename, title, visibility in spec["documents"]:
            content = (SEED_DIR / spec["slug"] / filename).read_text(encoding="utf-8")
            ingest_document(db, tenant_id=tenant.id, title=title, content=content, embedder=embedder,
                            visibility=visibility, source=filename)
        for order in fixture_orders(spec["slug"], users):
            db.add(order)
        db.flush()
        db.add_all(fixture_refunds(spec["slug"], tenant.id))
        db.commit()
        load_eval_cases(db, tenant, EVAL_DIR / f"{spec['slug']}_default.jsonl")
        # "hard" suite: paraphrases the offline lexical embedder is known to miss; run with
        # --suite hard, deliberately outside the CI gate (it measures headroom, not regressions).
        load_eval_cases(db, tenant, EVAL_DIR / f"{spec['slug']}_hard.jsonl", suite="hard")
    return True


DEMO_TRAFFIC = [
    ("customer@harbour.demo", ["What are the conditions for a change-of-mind return?",
                               "And how long do refunds take to come through?"]),
    ("customer@harbour.demo", ["Where is my order SO20261008002?"]),
    ("customer@harbour.demo", ["I'd like a refund for SO20261008002"]),
    ("customer2@harbour.demo", ["Is there an extra shipping fee for remote areas?",
                                "How do I use my membership points?"]),
    ("customer2@harbour.demo", ["How do I join the trade-in program?"]),
    ("customer2@harbour.demo", ["Can you check order SO20261008002 for me?"]),
    ("customer2@harbour.demo", ["This service is terrible. I want to complain — put me through to a human!"]),
    ("agent@harbour.demo", ["What is the most a frontline agent can offer as a goodwill voucher?"]),
    # repeat of Emma's first question: answer-cache hit
    ("customer2@harbour.demo", ["What are the conditions for a change-of-mind return?"]),
    ("customer@rostr.demo", ["How quickly do you respond to a P1 incident?"]),
]


def seed_demo_traffic(db: Session, services) -> None:
    """A handful of real conversations so a fresh demo already has traces, a pending
    approval and a knowledge-gap answer to correct."""
    from .agent.runtime import collect
    from .models import Conversation

    for email, questions in DEMO_TRAFFIC:
        user = db.execute(select(User).where(User.email == email)).scalar_one()
        tenant = db.get(Tenant, user.tenant_id)
        conversation = Conversation(tenant_id=tenant.id, user_id=user.id)
        db.add(conversation)
        db.commit()
        for question in questions:
            collect(services.runtime(db).start(tenant=tenant, user=user, text=question, conversation=conversation))


def main() -> None:  # pragma: no cover - CLI
    from .config import get_settings
    from .db import get_engine, init_engine, new_session, prepare_database
    from .db import Base
    from .rag.embeddings import build_embedder

    parser = argparse.ArgumentParser(description="Seed LoopDesk demo data")
    parser.add_argument("--reset", action="store_true", help="drop all tables first")
    args = parser.parse_args()
    settings = get_settings()
    init_engine(settings.database_url)
    if args.reset:
        Base.metadata.drop_all(get_engine())
        from sqlalchemy import text

        with get_engine().begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    prepare_database(settings.database_url, settings.auto_migrate)
    with new_session() as db:
        created = seed_demo(db, build_embedder(settings))
    print("seeded demo data" if created else "database already has data; use --reset to rebuild")


if __name__ == "__main__":  # pragma: no cover
    main()
