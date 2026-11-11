"""Hybrid retrieval over a company's documents (spec §3): keyword (Postgres full-text, BM25-style ts_rank_cd) +
vector (pgvector cosine on 768-dim gemini-embedding-2), merged with Reciprocal Rank Fusion.

- Transcripts are chunked by speaker turn (role kept: management | analyst | moderator) and embedded fully.
- Annual reports are chunked by page section and embedded lazily, only when a deep dive or the copilot needs them.
Chunks keep document, page range, speaker and role, so every retrieved passage can be cited.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import delete, func, select, text

from .database import engine, session
from .models import Chunk, Document, Page

log = logging.getLogger("finn.retrieval")
MAX_CHARS = 1400
RRF_K = 60


def _split(text_: str, limit: int = MAX_CHARS) -> list[str]:
    text_ = re.sub(r"\s+", " ", text_).strip()
    if len(text_) <= limit:
        return [text_] if text_ else []
    parts, cur = [], ""
    for sent in re.split(r"(?<=[.!?])\s+", text_):
        if len(cur) + len(sent) + 1 > limit and cur:
            parts.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}".strip()
    if cur:
        parts.append(cur)
    return parts


def chunk_document(doc_id: int) -> int:
    """(Re)build chunks for one document from its stored pages. Returns the number of chunks."""
    from .research.transcripts import split_turns
    with session() as s:
        d = s.get(Document, doc_id)
        pages = [t for _, t in s.execute(select(Page.page_no, Page.text).where(Page.document_id == doc_id)
                                         .order_by(Page.page_no)).all()]
        kind = d.kind if d else None
    if not pages:
        return 0
    rows = []
    if kind == "transcript":
        for t in split_turns(pages):
            for part in _split(t["text"]):
                if len(part) > 40:
                    rows.append({"document_id": doc_id, "page_from": t["page"], "page_to": t["page"],
                                 "speaker": t["speaker"][:160], "role": t["role"], "section": None, "text": part})
    else:
        for n, ptxt in enumerate(pages, 1):
            for part in _split(ptxt):
                if len(part) > 80:
                    rows.append({"document_id": doc_id, "page_from": n, "page_to": n, "speaker": None, "role": None,
                                 "section": None, "text": part})
    with session() as s:
        s.execute(delete(Chunk).where(Chunk.document_id == doc_id))
        s.add_all([Chunk(**r) for r in rows])
    return len(rows)


def ensure_embedded(doc_ids: list[int], budget_key: str | None = None) -> dict:
    """Chunk documents that have no chunks yet, then embed every chunk still without a vector."""
    from .llm import router
    made = embedded = 0
    for did in doc_ids:
        with session() as s:
            has = s.scalar(select(func.count(Chunk.id)).where(Chunk.document_id == did))
        if not has:
            made += chunk_document(did)
    if engine().dialect.name != "postgresql":
        return {"chunked": made, "embedded": 0}
    with session() as s:
        todo = s.execute(select(Chunk.id, Chunk.text).where(Chunk.document_id.in_(doc_ids), Chunk.embedding.is_(None))).all()
    for i in range(0, len(todo), 100):
        batch = todo[i:i + 100]
        vecs = router.embed([t for _, t in batch], "RETRIEVAL_DOCUMENT", budget_key=budget_key)
        with session() as s:
            for (cid, _), v in zip(batch, vecs):
                s.get(Chunk, cid).embedding = v
        embedded += len(batch)
    return {"chunked": made, "embedded": embedded}


def search(query: str, doc_ids: list[int], k: int = 8, role: str | None = None, use_vectors: bool = True) -> list[dict]:
    """Hybrid search. Returns chunks ranked by RRF over the keyword and vector legs, each with its citation."""
    if not doc_ids:
        return []
    role_sql = "AND c.role = :role" if role else ""
    params = {"q": query, "ids": doc_ids, "n": k * 4, "role": role}
    with engine().connect() as conn:
        kw = conn.execute(text(f"""
            SELECT c.id, ts_rank_cd(c.tsv, websearch_to_tsquery('english', :q)) AS r
            FROM chunks c WHERE c.document_id = ANY(:ids) {role_sql}
              AND c.tsv @@ websearch_to_tsquery('english', :q)
            ORDER BY r DESC LIMIT :n"""), params).all()
        vec = []
        if use_vectors:
            try:
                from .llm import router
                qv = router.embed([query], "RETRIEVAL_QUERY")[0]
                params["v"] = "[" + ",".join(f"{x:.6f}" for x in qv) + "]"
                vec = conn.execute(text(f"""
                    SELECT c.id, c.embedding <=> CAST(:v AS vector) AS d FROM chunks c
                    WHERE c.document_id = ANY(:ids) AND c.embedding IS NOT NULL {role_sql}
                    ORDER BY d LIMIT :n"""), params).all()
            except Exception as e:  # noqa: BLE001 - keyword leg still answers
                log.warning("vector leg unavailable: %s", e)
        fused: dict[int, float] = {}
        for rank, (cid, _) in enumerate(kw):
            fused[cid] = fused.get(cid, 0) + 1 / (RRF_K + rank + 1)
        for rank, (cid, _) in enumerate(vec):
            fused[cid] = fused.get(cid, 0) + 1 / (RRF_K + rank + 1)
        top = sorted(fused, key=fused.get, reverse=True)[:k]
        if not top:
            return []
        rows = conn.execute(text("""
            SELECT c.id, c.document_id, c.page_from, c.speaker, c.role, c.text, d.kind, d.period, d.title, d.url
            FROM chunks c JOIN documents d ON d.id = c.document_id WHERE c.id = ANY(:ids)"""), {"ids": top}).mappings().all()
    by = {r["id"]: dict(r) for r in rows}
    kw_ids, vec_ids = {c for c, _ in kw}, {c for c, _ in vec}
    return [dict(by[c], score=round(fused[c], 5), legs=[x for x, s in (("keyword", kw_ids), ("vector", vec_ids)) if c in s])
            for c in top if c in by]
