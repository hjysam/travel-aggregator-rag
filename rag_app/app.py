#!/usr/bin/env python3
import os, time, argparse, json, hashlib
from typing import List, Dict, Tuple
from pathlib import Path

import numpy as np
from fastapi import FastAPI
from pydantic import BaseModel
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

USE_FAISS = os.getenv("USE_FAISS", "1") == "1"
if USE_FAISS:
    import faiss

# Embeddings
_EMB = None
def get_embedder():
    global _EMB
    if _EMB is None:
        from sentence_transformers import SentenceTransformer
        _EMB = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    return _EMB

# Optional LLM (OpenAI-compatible)
_OPENAI = None
def have_llm():
    return bool(os.getenv("OPENAI_API_KEY"))

def get_openai():
    global _OPENAI
    if _OPENAI is None:
        from openai import OpenAI
        _OPENAI = OpenAI(
            base_url=os.getenv("OPENAI_BASE_URL", None),
            api_key=os.getenv("OPENAI_API_KEY", None),
        )
    return _OPENAI

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

def load_corpus(folder: Path):
    docs = []
    for p in folder.glob("**/*"):
        if p.is_file() and p.suffix.lower() in {".txt", ".md"}:
            docs.append((p.name, p.read_text(encoding="utf-8", errors="ignore")))
    return docs

def chunk_text(text: str, max_len: int = 500, overlap: int = 60):
    chunks = []
    cur = 0
    while cur < len(text):
        end = min(len(text), cur + max_len)
        chunk = text[cur:end].strip()
        if chunk:
            chunks.append(chunk)
        if end == len(text): break
        cur = max(0, end - overlap)
    return chunks

class HybridIndex:
    def __init__(self):
        self.vectorizer = None
        self.tfidf = None
        self.emb = None
        self.faiss_index = None
        self.texts = []
        self.meta = []
        self._built = False

    def build(self, corpus):
        t0 = time.time()
        for doc_name, content in corpus:
            for i, ch in enumerate(chunk_text(content)):
                self.texts.append(ch)
                self.meta.append((doc_name, i))

        self.vectorizer = TfidfVectorizer(ngram_range=(1,2), max_features=20000)
        self.tfidf = self.vectorizer.fit_transform(self.texts)

        if USE_FAISS and len(self.texts) > 0:
            emb_model = get_embedder()
            self.emb = emb_model.encode(self.texts, batch_size=64, show_progress_bar=False, normalize_embeddings=True)
            import faiss
            d = self.emb.shape[1]
            self.faiss_index = faiss.IndexFlatIP(d)
            self.faiss_index.add(self.emb.astype("float32"))

        self._built = True
        return {"chunks": len(self.texts), "build_sec": round(time.time() - t0, 3)}

    def search(self, query: str, k: int = 6, alpha: float = 0.6):
        assert self._built, "Index not built"
        t0 = time.time()
        q_tfidf = self.vectorizer.transform([query])
        sims_tfidf = cosine_similarity(q_tfidf, self.tfidf)[0]
        tfidf_top_idx = np.argpartition(sims_tfidf, -k*4)[-k*4:]
        tfidf_pairs = [(int(i), float(sims_tfidf[i])) for i in tfidf_top_idx]

        t1 = time.time()
        faiss_pairs = []
        if USE_FAISS and self.faiss_index is not None:
            emb_model = get_embedder()
            q_emb = emb_model.encode([query], normalize_embeddings=True)[0].astype("float32")
            D, I = self.faiss_index.search(q_emb.reshape(1, -1), k*4)
            faiss_pairs = [(int(I[0][j]), float(D[0][j])) for j in range(min(len(I[0]), k*4))]
        t2 = time.time()

        scores = {}
        def add_scores(pairs, weight):
            if not pairs: return
            vals = np.array([s for _, s in pairs])
            norm = (vals - vals.min()) / (vals.ptp() + 1e-9) if len(vals) else vals
            for (idx, _s), ns in zip(pairs, norm):
                scores[idx] = scores.get(idx, 0.0) + weight * float(ns)

        add_scores(tfidf_pairs, 1.0 - alpha)
        add_scores(faiss_pairs, alpha)

        merged = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:k]
        timings = {
            "tfidf_ms": round((t1 - t0) * 1000, 2),
            "faiss_ms": round((t2 - t1) * 1000, 2),
            "merge_ms": round((time.time() - t2) * 1000, 2),
        }
        return merged, timings

app = FastAPI(title="Travel Policy RAG Demo")
INDEX = HybridIndex()
DOC_ROOT = Path(os.getenv("DOC_ROOT", Path(__file__).parent / "docs"))

class QueryIn(BaseModel):
    query: str
    k: int = 6

def pack_context(items, max_chars=1800):
    ctx = []
    cites = []
    total = 0
    for idx, score in items:
        txt = INDEX.texts[idx]
        doc, chunk_id = INDEX.meta[idx]
        entry = f"[{doc}#{chunk_id}] {txt}"
        if total + len(entry) > max_chars:
            break
        ctx.append(entry)
        cites.append({"doc": doc, "chunk_id": int(chunk_id), "score": round(float(score), 3)})
        total += len(entry) + 1
    return "\n\n".join(ctx), cites

def llm_answer(query: str, context: str) -> str:
    prompt = f"""Answer **only** from context. If insufficient, say so.
Cite sources inline like [doc#chunk].
Q: {query}

Context:
{context}
"""
    if not bool(os.getenv("OPENAI_API_KEY")):
        # Extractive fallback
        lines = [l.strip() for l in context.splitlines() if l.strip()]
        bullets = [f"- {l[:220]}" for l in lines[:8]]
        return "Extractive summary (no LLM configured):\n" + "\n".join(bullets)

    from openai import OpenAI
    client = OpenAI(base_url=os.getenv("OPENAI_BASE_URL", None), api_key=os.getenv("OPENAI_API_KEY"))
    resp = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL","gpt-4o-mini"),
        messages=[
            {"role":"system","content":"You are a terse, source-grounded travel policy assistant."},
            {"role":"user","content": prompt}
        ],
        temperature=0.2, max_tokens=400
    )
    return resp.choices[0].message.content.strip()

@app.get("/health")
def health():
    return {"status":"ok", "built": INDEX._built, "chunks": len(INDEX.texts)}

@app.post("/answer")
def answer(payload: QueryIn):
    q = payload.query.strip()
    t0 = time.time()
    items, retr_timings = INDEX.search(q, k=payload.k)
    ctx, cites = pack_context(items)
    ans = llm_answer(q, ctx)
    return {
        "answer": ans,
        "citations": cites,
        "timings": {"retrieve_ms": retr_timings, "total_ms": round((time.time()-t0)*1000,2)}
    }

def build_index(doc_root: Path):
    corpus = load_corpus(doc_root)
    return INDEX.build(corpus)

if __name__ == "__main__":
    import argparse, uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.rebuild:
        print("Building index...")
        print(build_index(DOC_ROOT))
    uvicorn.run("app:app", host="0.0.0.0", port=args.port, reload=False)
