from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class SearchHit:
    rank: int
    score: float
    source: str
    text: str


class StoryEmbeddingSearch:
    """POC local embedding retrieval over game/world/story.json.

    - Embeddings: sentence-transformers (default: BAAI/bge-small-en-v1.5)
    - Vector index: FAISS HNSW (IndexHNSWFlat + cosine via normalized vectors)
    - GPU verification: requires CUDA and expected GPU substring (default: "4060")
    """

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.story_path = (self.repo_root / "game" / "world" / "story.json").resolve()
        self.index_root = (self.repo_root / "game" / "rag_poc").resolve()
        self.index_path = (self.index_root / "story_hnsw.faiss").resolve()
        self.meta_path = (self.index_root / "story_hnsw_meta.json").resolve()

        self.embed_model_name = (
            os.getenv("LLM_WORLD_RAG_EMBED_MODEL") or "BAAI/bge-small-en-v1.5"
        ).strip()

        self.chunk_chars = int((os.getenv("LLM_WORLD_RAG_CHUNK_CHARS") or "1000").strip() or "1000")
        self.chunk_overlap = int((os.getenv("LLM_WORLD_RAG_CHUNK_OVERLAP") or "120").strip() or "120")
        self.batch_size = int((os.getenv("LLM_WORLD_RAG_EMBED_BATCH") or "32").strip() or "32")

        self._faiss = None
        self._np = None
        self._torch = None
        self._SentenceTransformer = None
        self._embedder = None

        self._index = None
        self._chunks: List[Dict[str, str]] = []
        self._fingerprint = ""

    def _import_deps(self) -> None:
        if self._faiss is not None:
            return
        import faiss  # type: ignore
        import numpy as np  # type: ignore
        import torch  # type: ignore
        from sentence_transformers import SentenceTransformer  # type: ignore

        self._faiss = faiss
        self._np = np
        self._torch = torch
        self._SentenceTransformer = SentenceTransformer

    def _expected_gpu_substr(self) -> str:
        return (os.getenv("LLM_WORLD_RAG_EXPECT_GPU_SUBSTR") or "4060").strip().lower()

    def verify_gpu(self) -> Dict[str, Any]:
        self._import_deps()
        torch = self._torch

        if not bool(torch.cuda.is_available()):
            raise RuntimeError(
                "CUDA is not available. RAG POC requires GPU for embeddings. "
                "Install a CUDA-enabled PyTorch build and NVIDIA driver."
            )

        idx = int(torch.cuda.current_device())
        name = str(torch.cuda.get_device_name(idx) or "")
        expected = self._expected_gpu_substr()
        ok_expected = (not expected) or (expected in name.lower())
        if not ok_expected:
            raise RuntimeError(
                f"CUDA device is '{name}', expected to contain '{expected}'. "
                "Set LLM_WORLD_RAG_EXPECT_GPU_SUBSTR to adjust this check if needed."
            )

        return {
            "cuda": True,
            "device_index": idx,
            "device_name": name,
            "expected_substr": expected,
            "expected_match": ok_expected,
        }

    def _story_fingerprint(self) -> str:
        if not self.story_path.exists():
            raise RuntimeError(f"story.json not found: {self.story_path}")
        st = self.story_path.stat()
        return f"{st.st_size}:{int(st.st_mtime)}"

    def _load_story(self) -> Any:
        if not self.story_path.exists():
            return []
        raw = self.story_path.read_text(encoding="utf-8")
        try:
            return json.loads(raw)
        except Exception as exc:
            raise RuntimeError(f"Invalid story.json: {exc}") from exc

    def _add_doc(self, out: List[Dict[str, str]], *, source: str, payload: Dict[str, Any]) -> None:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if text.strip():
            out.append({"source": source, "text": text})

    def _extract_documents(self, story: Any) -> List[Dict[str, str]]:
        docs: List[Dict[str, str]] = []
        arcs = story if isinstance(story, list) else []

        seen_turns: set[str] = set()

        for arc_idx, arc in enumerate(arcs):
            if not isinstance(arc, dict):
                continue

            arc_name = str(arc.get("name") or f"Arc {arc_idx}")
            arc_summary = str(arc.get("summary") or "").strip()
            if arc_summary:
                self._add_doc(
                    docs,
                    source=f"arc[{arc_idx}] {arc_name} summary",
                    payload={"name": arc_name, "summary": arc_summary},
                )

            paragraphs = arc.get("paragraphs") if isinstance(arc.get("paragraphs"), list) else []
            for p_idx, para in enumerate(paragraphs):
                if not isinstance(para, dict):
                    continue
                p_name = str(para.get("name") or f"Paragraph {p_idx}")
                p_summary = str(para.get("summary") or "").strip()
                if p_summary:
                    self._add_doc(
                        docs,
                        source=f"arc[{arc_idx}]/paragraph[{p_idx}] {p_name} summary",
                        payload={"name": p_name, "summary": p_summary},
                    )

                p_turns = para.get("turns") if isinstance(para.get("turns"), list) else []
                for t_idx, turn in enumerate(p_turns):
                    if not isinstance(turn, dict):
                        continue
                    fp = json.dumps(
                        {
                            "start_time": str(turn.get("start_time") or ""),
                            "end_time": str(turn.get("end_time") or ""),
                            "location": str(turn.get("location") or ""),
                            "narration": str(turn.get("narration") or ""),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    if fp in seen_turns:
                        continue
                    seen_turns.add(fp)
                    self._add_doc(
                        docs,
                        source=f"arc[{arc_idx}]/paragraph[{p_idx}]/turn[{t_idx}]",
                        payload=turn,
                    )

            ongoing = arc.get("ongoing_paragraph") if isinstance(arc.get("ongoing_paragraph"), dict) else {}
            ongoing_turns = ongoing.get("turns") if isinstance(ongoing.get("turns"), list) else []
            for t_idx, turn in enumerate(ongoing_turns):
                if not isinstance(turn, dict):
                    continue
                fp = json.dumps(
                    {
                        "start_time": str(turn.get("start_time") or ""),
                        "end_time": str(turn.get("end_time") or ""),
                        "location": str(turn.get("location") or ""),
                        "narration": str(turn.get("narration") or ""),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                if fp in seen_turns:
                    continue
                seen_turns.add(fp)
                self._add_doc(
                    docs,
                    source=f"arc[{arc_idx}]/ongoing/turn[{t_idx}]",
                    payload=turn,
                )

        return docs

    def _chunk_text(self, text: str) -> List[str]:
        s = str(text or "").strip()
        if not s:
            return []

        size = max(200, int(self.chunk_chars))
        overlap = max(0, min(int(self.chunk_overlap), size // 2))

        out: List[str] = []
        i = 0
        n = len(s)
        while i < n:
            j = min(n, i + size)
            chunk = s[i:j].strip()
            if chunk:
                out.append(chunk)
            if j >= n:
                break
            i = j - overlap
        return out

    def _build_chunks(self) -> List[Dict[str, str]]:
        story = self._load_story()
        docs = self._extract_documents(story)
        chunks: List[Dict[str, str]] = []
        for doc in docs:
            source = str(doc.get("source") or "story")
            for k, chunk in enumerate(self._chunk_text(str(doc.get("text") or ""))):
                chunks.append({
                    "source": source,
                    "chunk_id": f"{source}#c{k}",
                    "text": chunk,
                })
        return chunks

    def _meta_load(self) -> Dict[str, Any]:
        if not self.meta_path.exists():
            return {}
        try:
            return json.loads(self.meta_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _meta_save(self, meta: Dict[str, Any]) -> None:
        self.index_root.mkdir(parents=True, exist_ok=True)
        tmp = self.meta_path.with_suffix(self.meta_path.suffix + ".tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.meta_path)

    def _get_embedder(self):
        if self._embedder is not None:
            return self._embedder
        self.verify_gpu()
        device = "cuda"
        self._embedder = self._SentenceTransformer(self.embed_model_name, device=device)
        return self._embedder

    def ensure_index(self, *, force_rebuild: bool = False) -> Dict[str, Any]:
        self._import_deps()
        faiss = self._faiss
        np = self._np

        fp = self._story_fingerprint()
        self._fingerprint = fp

        meta = self._meta_load()
        reusable = (
            (not force_rebuild)
            and self.index_path.exists()
            and bool(meta)
            and str(meta.get("story_fingerprint") or "") == fp
            and str(meta.get("embed_model") or "") == self.embed_model_name
        )

        if reusable:
            self._index = faiss.read_index(str(self.index_path))
            self._chunks = meta.get("chunks") if isinstance(meta.get("chunks"), list) else []
            return {
                "rebuilt": False,
                "chunks": len(self._chunks),
                "fingerprint": fp,
            }

        chunks = self._build_chunks()
        if not chunks:
            raise RuntimeError("No chunks extracted from story.json")

        model = self._get_embedder()
        texts = [str(c.get("text") or "") for c in chunks]

        emb = model.encode(
            texts,
            batch_size=max(1, int(self.batch_size)),
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        emb = np.asarray(emb, dtype=np.float32)

        dim = int(emb.shape[1])
        index = faiss.IndexHNSWFlat(dim, 32, faiss.METRIC_INNER_PRODUCT)
        index.hnsw.efConstruction = 80
        index.hnsw.efSearch = 64
        index.add(emb)

        self.index_root.mkdir(parents=True, exist_ok=True)
        faiss.write_index(index, str(self.index_path))

        meta = {
            "version": 1,
            "story_fingerprint": fp,
            "embed_model": self.embed_model_name,
            "chunk_chars": int(self.chunk_chars),
            "chunk_overlap": int(self.chunk_overlap),
            "chunks": chunks,
        }
        self._meta_save(meta)

        self._index = index
        self._chunks = chunks

        return {
            "rebuilt": True,
            "chunks": len(chunks),
            "fingerprint": fp,
        }

    def search(self, question: str, *, top_k: int = 6, force_rebuild: bool = False) -> Dict[str, Any]:
        q = str(question or "").strip()
        if not q:
            raise ValueError("question is required")

        self._import_deps()
        np = self._np

        t0 = time.perf_counter()
        gpu = self.verify_gpu()

        idx_info = self.ensure_index(force_rebuild=force_rebuild)

        model = self._get_embedder()

        t1 = time.perf_counter()
        qvec = model.encode(
            [q],
            batch_size=1,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        qvec = np.asarray(qvec, dtype=np.float32)

        k = max(1, int(top_k))
        D, I = self._index.search(qvec, k)
        t2 = time.perf_counter()

        hits: List[SearchHit] = []
        row_scores = D[0] if len(D) > 0 else []
        row_idx = I[0] if len(I) > 0 else []
        for rank, (score, idx) in enumerate(zip(row_scores, row_idx), start=1):
            ii = int(idx)
            if ii < 0 or ii >= len(self._chunks):
                continue
            chunk = self._chunks[ii]
            hits.append(
                SearchHit(
                    rank=rank,
                    score=float(score),
                    source=str(chunk.get("source") or "story"),
                    text=str(chunk.get("text") or ""),
                )
            )

        return {
            "gpu": gpu,
            "index": idx_info,
            "timings_s": {
                "setup": round(t1 - t0, 3),
                "query_and_search": round(t2 - t1, 3),
                "total": round(t2 - t0, 3),
            },
            "hits": [h.__dict__ for h in hits],
        }
