#!/usr/bin/env python3
"""
rag_app/app.py

RAG demo with:
- TF-IDF (content-based) + optional FAISS (semantic) via Azure OpenAI Embeddings
- SBERT fallback if Azure env is not configured
- Multi-root doc scanning via DOC_ROOTS (or DOC_ROOT / ./docs fallback)
- Eager index build at startup, /health, /reindex, and guarded /answer
- Diagnostics: /diag/azure, /diag/deployments, /diag/embed_try, /diag/docs

ENV (.env) examples:
    # ---- Docs (multi-root) ----
    DOC_ROOTS=C:\path\to\docs1;C:\path\to\docs2
    # or single-root fallback
    DOC_ROOT=C:\path\to\docs

    # ---- Optional: FAISS on/off ----
    USE_FAISS=1

    # ---- Azure OpenAI Embeddings (preferred) ----
    AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com
    AZURE_OPENAI_API_KEY=********
    AZURE_OPENAI_API_VERSION=2024-10-21
    AZURE_OPENAI_EMBED_DEPLOYMENT=text-embedding-3-large   # deployment name

    # ---- Optional SBERT (if not using Azure) ----
    # SBERT_MODEL_DIR=C:\models\all-MiniLM-L6-v2

    # ---- Azure OpenAI Chat for answer synthesis (optional) ----
    AZURE_OPENAI_DEPLOYMENT=gpt-4o-mini  # your chat deployment name

    # ---- (Corp proxies / custom CA) optional ----
    # CA_BUNDLE=C:\path\to\corp-ca.pem
"""

import os, time, argparse, asyncio, logging
from pathlib import Path
from typing import List, Dict, Tuple, Optional, Any

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from dotenv import load_dotenv, find_dotenv

# Load .env next to this file (override to ensure latest values)
load_dotenv(dotenv_path=str(Path(__file__).parent / ".env"), override=True)

USE_FAISS = os.getenv("USE_FAISS", "1") == "1"
if USE_FAISS:
    import faiss  # type: ignore

log = logging.getLogger("uvicorn.error")

# --------------------------------------------------------------------------------------
# Multi-root docs
# --------------------------------------------------------------------------------------

def _parse_doc_roots() -> List[Path]:
    raw = os.getenv("DOC_ROOTS")
    if raw:
        # Allow both ';' (Windows) and ',' separators
        parts = [p.strip() for p in raw.replace(",", ";").split(";") if p.strip()]
        return [Path(p) for p in parts]
    # Fallbacks: DOC_ROOT or ./docs
    single = os.getenv("DOC_ROOT", str(Path(__file__).parent / "docs"))
    return [Path(single)]

DOC_ROOTS: List[Path] = _parse_doc_roots()

def load_corpora(roots: List[Path]) -> List[Tuple[str, str]]:
    """
    Recursively read .txt/.md from multiple roots.
    Document "name" is 'rootname/relative/path.ext' to avoid collisions and
    show provenance in citations.
    """
    docs: List[Tuple[str, str]] = []
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if p.is_file() and p.suffix.lower() in {".txt", ".md"}:
                try:
                    rel = p.relative_to(root)
                    # Normalize path separators in name for cross-platform readability
                    name = f"{root.name}/{rel.as_posix()}"

                    docs.append((name, p.read_text(encoding="utf-8", errors="ignore")))
                except Exception:
                    continue
    return docs

def chunk_text(text: str, max_len: int = 500, overlap: int = 60) -> List[str]:
    chunks: List[str] = []
    cur = 0
    n = len(text)
    while cur < n:
        end = min(n, cur + max_len)
        chunk = text[cur:end].strip()
        if chunk:
            chunks.append(chunk)
        if end == n:
            break
        cur = max(0, end - overlap)
    return chunks

# --------------------------------------------------------------------------------------
# Embeddings: Azure OpenAI (preferred) or SBERT fallback
# --------------------------------------------------------------------------------------

def _l2_normalize(vecs: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return vecs / norms

class AzureOpenAIEmbeddingBackend:
    def __init__(self):
        from openai import AzureOpenAI
        import httpx

        http_client = None
        ca_bundle = os.getenv("CA_BUNDLE")
        if ca_bundle:
            http_client = httpx.Client(verify=ca_bundle)

        self.client = AzureOpenAI(
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
            api_key=os.getenv("AZURE_OPENAI_API_KEY"),
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
            http_client=http_client,
        )
        self.deployment = os.getenv("AZURE_OPENAI_EMBED_DEPLOYMENT")
        if not self.deployment:
            raise RuntimeError("AZURE_OPENAI_EMBED_DEPLOYMENT is not set")

    def embed_texts(self, texts: List[str], batch_size: int = 64) -> np.ndarray:
        out = []
        for i in range(0, len(texts), batch_size):
            chunk = texts[i:i + batch_size]
            resp = self.client.embeddings.create(model=self.deployment, input=chunk)
            out.extend([d.embedding for d in resp.data])
        arr = np.asarray(out, dtype="float32")
        return _l2_normalize(arr)

    def embed_query(self, text: str) -> np.ndarray:
        resp = self.client.embeddings.create(model=self.deployment, input=[text])
        arr = np.asarray([resp.data[0].embedding], dtype="float32")
        return _l2_normalize(arr)[0]

class SBERTEmbeddingBackend:
    def __init__(self):
        from sentence_transformers import SentenceTransformer  # imported only if used
        local_dir = os.getenv("SBERT_MODEL_DIR")  # optional offline path
        self.model = SentenceTransformer(local_dir or "sentence-transformers/all-MiniLM-L6-v2")

    def embed_texts(self, texts: List[str], batch_size: int = 64) -> np.ndarray:
        arr = self.model.encode(
            texts, batch_size=batch_size, show_progress_bar=False, normalize_embeddings=True
        ).astype("float32")
        return arr

    def embed_query(self, text: str) -> np.ndarray:
        arr = self.model.encode([text], normalize_embeddings=True).astype("float32")
        return arr[0]

def get_embedding_backend():
    """Prefer Azure if configured; else SBERT."""
    if all([
        os.getenv("AZURE_OPENAI_ENDPOINT"),
        os.getenv("AZURE_OPENAI_API_KEY"),
        os.getenv("AZURE_OPENAI_EMBED_DEPLOYMENT"),
    ]):
        return AzureOpenAIEmbeddingBackend()
    return SBERTEmbeddingBackend()

# --------------------------------------------------------------------------------------
# Hybrid Index
# --------------------------------------------------------------------------------------

class HybridIndex:
    def __init__(self):
        self.vectorizer: Optional[TfidfVectorizer] = None
        self.tfidf = None
        self.emb: Optional[np.ndarray] = None
        self.faiss_index = None
        self.texts: List[str] = []
        self.meta: List[Tuple[str, int]] = []
        self._embed_backend = None
        self._built: bool = False

    def build(self, corpus: List[Tuple[str, str]]) -> Dict[str, Any]:
        t0 = time.time()
        # reset if rebuilding
        self.vectorizer = None
        self.tfidf = None
        self.emb = None
        self.faiss_index = None
        self.texts = []
        self.meta = []
        self._embed_backend = None
        self._built = False

        # guard: empty corpus
        if not corpus:
            self._built = True  # mark built to avoid 503s
            return {"chunks": 0, "build_sec": round(time.time() - t0, 3)}

        # chunk corpus → self.texts / self.meta
        for doc_name, content in corpus:
            for i, ch in enumerate(chunk_text(content)):
                self.texts.append(ch)
                self.meta.append((doc_name, i))

        # TF-IDF
        self.vectorizer = TfidfVectorizer(ngram_range=(1, 2), max_features=20000)
        self.tfidf = self.vectorizer.fit_transform(self.texts)

        # FAISS (semantic) with safe fallback
        if USE_FAISS and len(self.texts) > 0:
            try:
                backend = get_embedding_backend()
                self._embed_backend = backend
                self.emb = backend.embed_texts(self.texts)
                d = self.emb.shape[1]
                self.faiss_index = faiss.IndexFlatIP(d)  # cosine via normalized inner product
                self.faiss_index.add(self.emb.astype("float32"))
            except Exception as e:
                log.exception("Semantic embedding build failed; continuing with TF-IDF only: %s", e)
                self.emb = None
                self.faiss_index = None
                self._embed_backend = None

        self._built = True
        return {"chunks": len(self.texts), "build_sec": round(time.time() - t0, 3)}

    def search(self, query: str, k: int = 6, alpha: float = 0.6):
        assert self._built, "Index not built"
        if not self.vectorizer or self.tfidf is None:
            return [], {"tfidf_ms": 0.0, "faiss_ms": 0.0, "merge_ms": 0.0}

        t0 = time.time()
        # TF-IDF scores
        q_tfidf = self.vectorizer.transform([query])
        sims_tfidf = cosine_similarity(q_tfidf, self.tfidf)[0]

        corpus_n = len(self.texts)
        if corpus_n == 0:
            return [], {"tfidf_ms": 0.0, "faiss_ms": 0.0, "merge_ms": 0.0}

        pool = min(max(k * 4, 20), corpus_n)  # widen but cap at corpus size
        tfidf_top_idx = np.argpartition(sims_tfidf, corpus_n - pool)[-pool:]
        tfidf_pairs = [(int(i), float(sims_tfidf[i])) for i in tfidf_top_idx]
        t1 = time.time()

        # FAISS semantic scores
        faiss_pairs: List[Tuple[int, float]] = []
        if USE_FAISS and self.faiss_index is not None and self._embed_backend is not None:
            q_vec = self._embed_backend.embed_query(query).astype("float32").reshape(1, -1)
            D, I = self.faiss_index.search(q_vec, pool)
            faiss_pairs = [(int(I[0][j]), float(D[0][j])) for j in range(min(len(I[0]), pool))]
        t2 = time.time()

        # Merge with per-source min-max normalization
        scores: Dict[int, float] = {}

        def add_scores(pairs, weight: float):
            if not pairs:
                return
            vals = np.array([s for _, s in pairs], dtype="float32")
            vmin = float(vals.min()) if len(vals) else 0.0
            vmax = float(vals.max()) if len(vals) else 1.0
            denom = (vmax - vmin) if vmax > vmin else 1.0
            for (idx, s) in pairs:
                ns = (float(s) - vmin) / denom
                scores[idx] = scores.get(idx, 0.0) + weight * ns

        add_scores(tfidf_pairs, 1.0 - alpha)
        add_scores(faiss_pairs, alpha)

        merged = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:k]
        timings = {
            "tfidf_ms": round((t1 - t0) * 1000, 2),
            "faiss_ms": round((t2 - t1) * 1000, 2),
            "merge_ms": round((time.time() - t2) * 1000, 2),
        }
        return merged, timings

# --------------------------------------------------------------------------------------
# FastAPI app
# --------------------------------------------------------------------------------------

app = FastAPI(title="Travel Policy RAG Demo")
INDEX = HybridIndex()

class QueryIn(BaseModel):
    query: str
    k: int = 6

def pack_context(items: List[Tuple[int, float]], max_chars: int = 1800):
    ctx: List[str] = []
    cites: List[Dict[str, Any]] = []
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
    # If no Azure chat key/deployment, fall back to extractive
    if not (os.getenv("AZURE_OPENAI_API_KEY") and os.getenv("AZURE_OPENAI_DEPLOYMENT") and os.getenv("AZURE_OPENAI_ENDPOINT")):
        lines = [l.strip() for l in context.splitlines() if l.strip()]
        bullets = [f"- {l[:220]}" for l in lines[:8]]
        return "Extractive summary (no Azure Chat configured):\n" + "\n".join(bullets)

    # Azure OpenAI chat
    from openai import AzureOpenAI
    import httpx

    http_client = None
    ca_bundle = os.getenv("CA_BUNDLE")
    if ca_bundle:
        http_client = httpx.Client(verify=ca_bundle)

    client = AzureOpenAI(
        azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        api_key=os.getenv("AZURE_OPENAI_API_KEY"),
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        http_client=http_client,
    )
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT")
    resp = client.chat.completions.create(
        model=deployment,
        messages=[
            {"role": "system", "content": "You are a terse, source-grounded travel policy assistant."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=400,
    )
    return resp.choices[0].message.content.strip()

# ---------- Diagnostics ----------
import httpx
from openai import AzureOpenAI

@app.get("/diag/azure")
def diag_azure():
    return {
        "endpoint": os.getenv("AZURE_OPENAI_ENDPOINT"),
        "api_version": os.getenv("AZURE_OPENAI_API_VERSION"),
        "embed_deployment": os.getenv("AZURE_OPENAI_EMBED_DEPLOYMENT"),
        "chat_deployment": os.getenv("AZURE_OPENAI_DEPLOYMENT"),
        "roots": [str(p) for p in DOC_ROOTS],
    }

@app.get("/diag/deployments")
def diag_deployments():
    try:
        http_client = httpx.Client(verify=os.getenv("CA_BUNDLE")) if os.getenv("CA_BUNDLE") else None
        client = AzureOpenAI(
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
            api_key=os.getenv("AZURE_OPENAI_API_KEY"),
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
            http_client=http_client,
        )
        models = client.models.list()
        return {"deployments": [m.id for m in models.data]}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/diag/embed_try")
def diag_embed_try():
    """Make one tiny embed call using the env the server has loaded."""
    try:
        http_client = httpx.Client(verify=os.getenv("CA_BUNDLE")) if os.getenv("CA_BUNDLE") else None
        client = AzureOpenAI(
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
            api_key=os.getenv("AZURE_OPENAI_API_KEY"),
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
            http_client=http_client,
        )
        dep = os.getenv("AZURE_OPENAI_EMBED_DEPLOYMENT")
        r = client.embeddings.create(model=dep, input=["ping"])
        dim = len(r.data[0].embedding)
        return {"deployment_used": dep, "dim": dim}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/diag/docs")
def diag_docs(limit: int = 100):
    docs = sorted({doc for (doc, _chunk) in INDEX.meta})
    return {"total_docs": len(docs), "sample": docs[:limit], "roots": [str(p) for p in DOC_ROOTS]}

# ---------- Build index at startup ----------
def _build_index_from_env():
    corpus = load_corpora(DOC_ROOTS)
    return INDEX.build(corpus)

@app.on_event("startup")
async def _startup_build_index():
    loop = asyncio.get_running_loop()
    log.info(f"Building index from {', '.join(str(p) for p in DOC_ROOTS)} ...")
    try:
        stats = await loop.run_in_executor(None, _build_index_from_env)
        log.info(f"Index build complete. {stats}")
    except Exception as e:
        log.exception("Index build failed: %s", e)

# ---------- API ----------
@app.get("/health")
def health():
    return {
        "status": "ok",
        "built": INDEX._built,
        "chunks": len(INDEX.texts),
        "roots": [str(p) for p in DOC_ROOTS],
    }

@app.post("/answer")
def answer(payload: QueryIn):
    q = payload.query.strip()
    if not q:
        raise HTTPException(status_code=400, detail="Missing 'query'.")
    if not INDEX._built:
        raise HTTPException(status_code=503, detail="Index is building or unavailable. Try again shortly.")
    t0 = time.time()
    items, retr_timings = INDEX.search(q, k=payload.k)
    if not items:
        raise HTTPException(status_code=404, detail="No results in index.")
    ctx, cites = pack_context(items)
    ans = llm_answer(q, ctx)
    return {
        "answer": ans,
        "citations": cites,
        "timings": {"retrieve_ms": retr_timings, "total_ms": round((time.time() - t0) * 1000, 2)}
    }

@app.post("/reindex")
def reindex():
    try:
        INDEX._built = False
        stats = _build_index_from_env()
        return {"status": "rebuilt", **stats, "roots": [str(p) for p in DOC_ROOTS]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Reindex failed: {e}")

# --------------------------------------------------------------------------------------
# CLI (optional)
# --------------------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.rebuild:
        cps = load_corpora(DOC_ROOTS)
        print(f"Building index from {', '.join(str(p) for p in DOC_ROOTS)} ...")
        print(INDEX.build(cps))

    uvicorn.run("app:app", host="0.0.0.0", port=args.port, reload=False)
