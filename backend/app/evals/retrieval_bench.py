"""Retrieval latency as the knowledge base grows.

    python -m app.evals.retrieval_bench --sizes 1000,5000,20000 --queries 40 --report retrieval.json

Builds a throwaway SQLite database with the demo tenant plus N synthetic chunks (sentences of
the demo knowledge base recombined with made-up product names, so vocabulary and lengths are
realistic), then times building the tenant's index and hybrid_search per question. Offline
hashing embeddings; numbers are for comparing implementations on one machine, not capacity.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import tempfile
import time
from pathlib import Path

QUESTIONS = ["how long does a refund take", "who pays return postage", "how do membership points work",
             "how do I get a tax invoice", "what does membership include", "can I pay on delivery",
             "how long does overseas delivery take", "how do I change the delivery address",
             "how many days for a card refund", "what if my parcel is lost"]
PRODUCTS = ["Atlas air fryer", "Orion robot vacuum", "Kettle", "Hand cream", "Running shoes", "Desk lamp",
            "Storage box", "Backpack", "Electric kettle", "Bluetooth headphones"]


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - benchmark CLI
    parser = argparse.ArgumentParser(description="LoopDesk retrieval benchmark")
    parser.add_argument("--sizes", default="1000,5000,20000")
    parser.add_argument("--queries", type=int, default=40)
    parser.add_argument("--report")
    args = parser.parse_args(argv)

    from sqlalchemy import select

    from ..config import Settings
    from ..db import init_engine, new_session, prepare_database
    from ..models import Chunk, Document, Tenant
    from ..rag.embeddings import pack
    from ..rag.ingest import ingest_document
    from ..rag.retrieval import hybrid_search, index_manager
    from ..seed import seed_demo
    from ..services import Services
    from ..textutil import split_sentences

    settings = Settings()
    settings.database_url = f"sqlite:///{Path(tempfile.mkdtemp()) / 'retrieval.db'}"
    init_engine(settings.database_url)
    prepare_database(settings.database_url, True)
    services = Services(settings)
    rng = random.Random(42)
    results = []
    with new_session() as db:
        seed_demo(db, services.embedder)
        tenant = db.execute(select(Tenant).where(Tenant.slug == "harbour")).scalar_one()
        sentences = [s for c in db.execute(select(Chunk).where(Chunk.tenant_id == tenant.id)).scalars()
                     for s in split_sentences(c.text) if len(s) > 6]
        current = db.execute(select(Chunk).where(Chunk.tenant_id == tenant.id)).scalars().all().__len__()
        for size in [int(s) for s in args.sizes.split(",")]:
            while current < size:
                batch = min(500, size - current)
                document = Document(tenant_id=tenant.id, title=f"Synthetic document {current}", content="", visibility="public",
                                    source="bench", version=1, chunk_count=batch)
                db.add(document)
                db.flush()
                texts = [f"{rng.choice(PRODUCTS)} {current + i}: " + " ".join(rng.sample(sentences, 3)) for i in range(batch)]
                vectors = services.embedder.embed([f"Synthetic section\n{t}" for t in texts])
                db.add_all(Chunk(tenant_id=tenant.id, document_id=document.id, ord=i, heading="Synthetic section", text=text,
                                 embedding=pack(vector)) for i, (text, vector) in enumerate(zip(texts, vectors)))
                db.commit()
                current += batch
            index_manager.invalidate(tenant.id)
            index_manager._tokens.pop(tenant.id, None)  # cold: every chunk is tokenized
            started = time.perf_counter()
            index_manager.get(db, tenant.id)
            build_s = time.perf_counter() - started
            signature = []  # the per-search freshness check (two aggregate queries)
            for _ in range(args.queries):
                started = time.perf_counter()
                index_manager.signature(db, tenant.id)
                signature.append((time.perf_counter() - started) * 1000)
            latencies = []
            for n in range(args.queries):
                started = time.perf_counter()
                hybrid_search(db, tenant.id, QUESTIONS[n % len(QUESTIONS)], services.embedder, {"public"},
                              top_k=4, min_relevance=settings.min_relevance)
                latencies.append((time.perf_counter() - started) * 1000)
            # An administrator edits one document: the next search pays for the rebuild.
            ingest_document(db, tenant_id=tenant.id, title=f"Additional note {current}", content="## Additional note\n- You can follow the progress of a refund under the order details.",
                            embedder=services.embedder)
            started = time.perf_counter()
            index_manager.get(db, tenant.id)
            rebuild_s = time.perf_counter() - started
            row = {"chunks": current, "index_build_s": round(build_s, 2), "rebuild_after_edit_s": round(rebuild_s, 2),
                   "p50_ms": round(statistics.median(latencies), 1), "p95_ms": round(_percentile(latencies, 0.95), 1),
                   "freshness_check_ms": round(statistics.median(signature), 2)}
            results.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    if args.report:
        Path(args.report).write_text(json.dumps({"levels": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
