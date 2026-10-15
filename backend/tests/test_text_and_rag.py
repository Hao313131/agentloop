import pytest

from app.rag.chunking import UnsupportedFile, chunk_markdown, parse_upload
from app.rag.embeddings import HashingEmbedder
from app.textutil import extract_numbers, strip_citations, tokenize


def test_tokenizer_drops_question_words_and_keeps_domain_terms():
    tokens = tokenize("How long do refunds take?")
    assert "refund" in tokens and "long" in tokens
    assert "how" not in tokens and "do" not in tokens


def test_numbers_ignore_identifiers_and_thousand_separators():
    assert extract_numbers("Order SO20261008002 total $1,299.00, expected in 1-3 business days") == [1299.0, 1.0, 3.0]


def test_strip_citations():
    assert strip_citations("Refunds go back to the original payment method[2].") == \
        ("Refunds go back to the original payment method.", [2])


def test_chunks_keep_heading_path_and_never_cross_sections():
    doc = ("# Returns\n\n## Change of mind\n- You can return an item within 30 days.\n\n"
           "## Refund timing\n- Card refunds take 1-3 business days.\n")
    chunks = chunk_markdown(doc)
    assert [c.heading for c in chunks] == ["Returns > Change of mind", "Returns > Refund timing"]
    assert "business days" not in chunks[0].text


def test_long_section_is_split_with_overlap():
    body = "".join(f"Rule {i} is a sentence long enough that the section has to be split. " for i in range(40))
    chunks = chunk_markdown(f"## Rules\n{body}", max_chars=120, overlap=1)
    assert len(chunks) > 3
    last_sentence = chunks[0].text.splitlines()[-1]
    assert chunks[1].text.startswith(last_sentence)


def test_parse_upload_strips_a_bom_and_reads_legacy_exports():
    assert parse_upload("faq.md", "Returns policy\n".encode("utf-8-sig")) == ("Returns policy\n", None)
    # A plain-text export from an older Windows app is Windows-1252, not UTF-8.
    assert parse_upload("faq.txt", "“Returns” policy".encode("cp1252")) == ("“Returns” policy", None)
    with pytest.raises(UnsupportedFile):
        parse_upload("faq.docx", b"PK\x03\x04")


def test_hashing_embedder_is_normalized_and_similar_texts_are_closer():
    e = HashingEmbedder(256)
    a, b, c = e.embed(["how long do refunds take", "how long does a refund take", "how do I get a tax invoice"])
    dot = lambda x, y: sum(i * j for i, j in zip(x, y))
    assert abs(dot(a, a) - 1) < 1e-6
    assert dot(a, b) > dot(a, c)


def test_hybrid_search_ranks_policy_and_hides_internal_docs(client, login):
    staff = login("agent@harbour.demo")
    r = client.post("/api/kb/search", json={"query": "how long do refunds take", "scope": "customer"},
                    headers=staff).json()
    assert r["hits"][0]["doc_title"] == "Returns and refunds"
    assert r["hits"][0]["bm25_rank"] == 1
    customer_view = client.post("/api/kb/search", json={"query": "goodwill voucher", "scope": "customer"},
                                headers=staff).json()
    assert all(h["doc_title"] != "Support handbook" for h in customer_view["hits"])
    staff_view = client.post("/api/kb/search", json={"query": "goodwill voucher", "scope": "staff"},
                             headers=staff).json()
    assert staff_view["hits"][0]["doc_title"] == "Support handbook"


def test_injected_chunk_is_flagged_at_ingestion(client, login):
    admin = login("admin@harbour.demo")
    docs = client.get("/api/kb/documents", headers=admin).json()
    supplier = next(d for d in docs if d["title"] == "Supplier FAQ")
    assert supplier["flagged_chunks"] == 1
    detail = client.get(f"/api/kb/documents/{supplier['id']}", headers=admin).json()
    flagged = [c for c in detail["chunks"] if c["flagged_injection"]]
    assert "ignore all previous instructions" in flagged[0]["text"]


def test_query_expansion_folds_variants_and_adds_canonical_terms():
    from app.textutil import expand_query

    assert expand_query("I want my money back") == "I want my money back refund", "the colloquial phrase is added, not replaced"
    expanded = expand_query("how do I get a receipt")
    assert "tax invoice" in expanded and expanded.count("tax invoice") == 1
    assert expand_query("how much is postage?") == "how much is postage? shipping fee"
    assert expand_query("what about lay-by") == "what about lay-by", "no expansion without a matching phrase"


def test_order_numbers_are_found_next_to_running_text():
    from app.textutil import ORDER_ID_RE

    for text in ("my order is SO20261008002, where is it", "SO20261008002 tracking", "order so20261008002 please",
                 "(SO20261008002)"):
        assert ORDER_ID_RE.search(text).group(0).upper() == "SO20261008002", text
    assert ORDER_ID_RE.search("XSO20261008002").group(0).upper() == "SO20261008002", \
        "a customer may type the number straight after a word"
    assert ORDER_ID_RE.search("SO202610080021234") is None, "a longer number is not truncated into a valid one"


def test_relevance_gate_scores_the_canonical_wording():
    from app.textutil import canonical_query

    # Colloquial words are outside the knowledge base's vocabulary and would only dilute coverage.
    assert tokenize(canonical_query("can I get a receipt?")) == ["tax", "invoice"]
    assert tokenize(canonical_query("do you ship overseas?")) == ["ship", "internationally"]
    assert tokenize(canonical_query("when do I get my money back?")) == ["refund"]
    assert canonical_query("what about lay-by") == "what about lay-by", "no synonym, no change"


def test_phrases_stay_separate_words_for_the_policy_vocabulary():
    from app.textutil import tokenize

    assert tokenize("who pays return postage") == ["pay", "return", "postage"]
    assert "shipping" in tokenize("an $8.00 shipping fee") and "fee" in tokenize("an $8.00 shipping fee")
    assert "returnlabel" not in tokenize("Gold members get two free return labels every month")


def test_unknown_compound_is_not_covered_by_its_sub_word(client, login):
    # "cash on delivery" is not in the knowledge base; "delivery" is ("Delivery and tracking").
    staff = login("admin@harbour.demo")
    r = client.post("/api/kb/search", json={"query": "do you offer cash on delivery", "scope": "customer"}, headers=staff)
    assert not any(h["passes_threshold"] for h in r.json()["hits"]), \
        "a sub-word alone must not carry an unknown compound"
    r = client.post("/api/kb/search", json={"query": "how long is delivery", "scope": "customer"}, headers=staff)
    assert any(h["passes_threshold"] for h in r.json()["hits"]), "the same sub-word on its own still counts"


def test_tokenize_cache_hands_out_copies():
    from app.textutil import tokenize

    first = tokenize("How long do refunds take?")
    first.append("mutated by the caller")
    assert tokenize("How long do refunds take?") == ["long", "refund", "take"], \
        "a caller mutating its list must not poison the cache"
    long_text = "refund " * 600  # longer than the cache limit: still tokenized, just not cached
    assert tokenize(long_text)[:2] == ["refund", "refund"]


def _scan_bm25(index, q_tokens, allowed, k, k1=1.5, b=0.75):
    """The original full scan, kept as the reference the inverted index must match."""
    scores, unique = [], set(q_tokens)
    for i, c in enumerate(index.chunks):
        if c.visibility not in allowed:
            continue
        score = 0.0
        for t in unique:
            tf = c.tf.get(t)
            if tf:
                score += index.idf(t) * tf * (k1 + 1) / (tf + k1 * (1 - b + b * c.length / (index.avgdl or 1)))
        if score > 0:
            scores.append((i, score))
    scores.sort(key=lambda x: -x[1])
    return scores[:k]


def _scan_vector(index, qvec, allowed, k):
    scores = [(i, sum(a * b for a, b in zip(qvec, index.matrix[i].tolist())))
              for i, c in enumerate(index.chunks) if c.visibility in allowed]
    scores.sort(key=lambda x: -x[1])
    return [s for s in scores[:k] if s[1] > 1e-6]


def test_the_index_ranks_exactly_like_a_full_scan(client):
    # BM25 now walks an inverted index and vector scores are one matrix product (20,000 chunks:
    # 391 ms -> see docs/PERFORMANCE.md). Rankings must not move: same chunks, same order.
    from sqlalchemy import select

    from app.db import new_session
    from app.models import Tenant
    from app.rag.retrieval import index_manager
    from app.services import get_services
    from app.textutil import expand_query, tokenize

    questions = ["how long do refunds take", "who pays return postage", "how do I use my points",
                 "how do I get a tax invoice", "do you offer cash on delivery", "express shipping",
                 "how long does delivery take", "goodwill voucher for an upset customer",
                 "SO20261008002 where is it", "membership tiers"]
    embedder = get_services().embedder
    with new_session() as db:
        tenant = db.execute(select(Tenant).where(Tenant.slug == "harbour")).scalar_one()
        index = index_manager.get(db, tenant.id)
        for question in questions:
            query = expand_query(question)
            q_tokens, qvec = tokenize(query), embedder.embed([query])[0]
            for allowed in ({"public"}, {"public", "internal"}):
                assert index.bm25(q_tokens, allowed, 12) == _scan_bm25(index, q_tokens, allowed, 12), question
                got, expected = index.vector(qvec, allowed, 12), _scan_vector(index, qvec, allowed, 12)
                assert [i for i, _ in got] == [i for i, _ in expected], question
                assert all(abs(a - b) < 1e-5 for (_, a), (_, b) in zip(got, expected))


def test_a_rebuilt_index_never_reuses_the_tokens_of_another_text(client, login):
    # Rebuilds reuse each chunk's tokens. SQLite can give a new chunk the id of a deleted one,
    # so the cache is keyed by the text too: the new chunk must be searched by its own words.
    from sqlalchemy import select

    from app.db import new_session
    from app.models import Chunk

    admin = login("admin@harbour.demo")

    def search(query):
        return client.post("/api/kb/search", json={"query": query}, headers=admin).json()["hits"]

    first = client.post("/api/kb/documents/text", headers=admin,
                        json={"title": "Dive watch", "content": "## Water resistance\n- The dive watch is rated to 50 metres."}).json()
    assert any(h["document_id"] == first["id"] for h in search("is the watch waterproof"))
    with new_session() as db:
        old_ids = set(db.execute(select(Chunk.id).where(Chunk.document_id == first["id"])).scalars())
    client.delete(f"/api/kb/documents/{first['id']}", headers=admin)
    second = client.post("/api/kb/documents/text", headers=admin,
                         json={"title": "Folding bike", "content": "## Weight\n- The folding bike weighs twelve kilograms."}).json()
    with new_session() as db:
        new_ids = set(db.execute(select(Chunk.id).where(Chunk.document_id == second["id"])).scalars())
        if db.get_bind().dialect.name == "sqlite":
            assert old_ids & new_ids, "the scenario needs a reused id (SQLite without AUTOINCREMENT)"
    assert any(h["document_id"] == second["id"] and h["bm25_rank"] for h in search("how much does the folding bike weigh")), \
        "found by its own words, not only by its vector"
    assert not any(h["document_id"] == second["id"] and h["bm25_rank"] for h in search("is the watch waterproof")), \
        "the deleted document's words must not match the new chunk"


def test_every_worker_sees_a_knowledge_change_through_one_cheap_lookup(client, login):
    # The freshness check used to count the tenant's chunks and sum its document versions on
    # every search (2.6 ms at 50,000 chunks, twice per chat request). It is now one primary-key
    # lookup of a version that each chunk change bumps in its own transaction.
    from sqlalchemy import event, select

    from app.db import get_engine, new_session
    from app.models import Tenant
    from app.rag.retrieval import IndexManager, index_manager

    admin = login("admin@harbour.demo")
    other_worker = IndexManager()
    with new_session() as db:
        tenant_id = db.execute(select(Tenant.id).where(Tenant.slug == "harbour")).scalar_one()
        before = other_worker.get(db, tenant_id)
        statements = []
        listener = lambda *args: statements.append(args[2])  # noqa: E731 - the statement text
        event.listen(get_engine(), "before_cursor_execute", listener)
        try:
            version = index_manager.signature(db, tenant_id)
        finally:
            event.remove(get_engine(), "before_cursor_execute", listener)
        assert len(statements) == 1 and "kb_version" in statements[0]

    doc = client.post("/api/kb/documents/text", headers=admin,
                      json={"title": "Dive watch", "content": "## Water resistance\n- The dive watch is rated to 50 metres."}).json()
    with new_session() as db:
        assert index_manager.signature(db, tenant_id) != version
        after_create = other_worker.get(db, tenant_id)
        assert after_create is not before and after_create.n == before.n + doc["chunk_count"]
    client.put(f"/api/kb/documents/{doc['id']}", headers=admin, json={
        "title": "Dive watch", "content": "## Water resistance\n- The dive watch is rated to 100 metres.",
        "visibility": "public", "version": doc["version"]})
    with new_session() as db:
        assert any("100 metres" in c.text for c in other_worker.get(db, tenant_id).chunks)
    client.delete(f"/api/kb/documents/{doc['id']}", headers=admin)
    with new_session() as db:
        assert other_worker.get(db, tenant_id).n == before.n


def test_a_query_vector_of_another_width_falls_back_to_bm25(client):
    # Code review: after switching the embedding model without re-indexing, the matrix product
    # raised on every search (500 on /api/kb/search; the chat tool failed). The old loop simply
    # mis-scored. Vector scores are skipped until the documents are re-indexed; BM25 still answers.
    from sqlalchemy import select

    from app.db import new_session
    from app.models import Tenant
    from app.rag.embeddings import HashingEmbedder
    from app.rag.retrieval import hybrid_search

    with new_session() as db:
        tenant = db.execute(select(Tenant).where(Tenant.slug == "harbour")).scalar_one()
        hits, debug = hybrid_search(db, tenant.id, "how long do refunds take", HashingEmbedder(1024), {"public"}, top_k=4)
    assert hits and debug.vector == [] and debug.bm25
    assert any("business days" in h.text for h in hits)
