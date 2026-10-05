"""Text embeddings behind a small interface.

- `FastEmbedEmbedder`: local ONNX model (default BAAI/bge-small-en-v1.5, 384-d). Runs in-process,
  so no document text leaves the machine during ingestion. Downloads the model once.
- `HashingEmbedder`: deterministic feature-hashing of words and word pairs. No download, not
  semantic. For tests, CI and offline environments; keyword search still carries retrieval there.
"""

import hashlib
import math
import re
import threading
from collections.abc import Sequence
from itertools import pairwise
from typing import Any, Protocol

from app.config import Settings
from app.db.models import EMBEDDING_DIM

_TOKEN = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    @property
    def model_name(self) -> str: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class HashingEmbedder:
    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self._dim = dim

    @property
    def model_name(self) -> str:
        return f"hashing-{self._dim}"

    def _embed(self, text: str) -> list[float]:
        tokens = _TOKEN.findall(text.lower())
        features = tokens + [f"{a} {b}" for a, b in pairwise(tokens)]
        vec = [0.0] * self._dim
        for feature in features:
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            idx = int.from_bytes(digest[:4], "little") % self._dim
            vec[idx] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


class FastEmbedEmbedder:
    # bge models expect this instruction prefix on queries (not on documents).
    _QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

    def __init__(self, model_name: str, cache_dir: str) -> None:
        self._name = model_name
        self._cache_dir = cache_dir
        self._model: Any = None
        self._lock = threading.Lock()  # queries run in worker threads; load the model once

    def _loaded(self) -> Any:
        # Loaded on first use (first search or ingestion), not at app startup: the first load
        # downloads the model, and a slow or blocked download must not stop the app starting.
        with self._lock:
            if self._model is None:
                from fastembed import TextEmbedding  # heavy import; only when used

                self._model = TextEmbedding(model_name=self._name, cache_dir=self._cache_dir)
            return self._model

    @property
    def model_name(self) -> str:
        return self._name

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [v.tolist() for v in self._loaded().embed(list(texts), batch_size=32)]

    def embed_query(self, text: str) -> list[float]:
        prefix = self._QUERY_PREFIX if "bge" in self._name.lower() else ""
        vector: list[float] = next(iter(self._loaded().embed([prefix + text]))).tolist()
        return vector


def create_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "hashing":
        return HashingEmbedder()
    return FastEmbedEmbedder(settings.embedding_model, settings.embedding_cache_dir)
