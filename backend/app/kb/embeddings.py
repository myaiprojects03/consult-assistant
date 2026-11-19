"""Text embeddings for knowledge search, computed on this server (no API cost, no data leaves it)."""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

from app.config import PROJECT_ROOT, Settings

log = logging.getLogger(__name__)


class Embedder(ABC):
    dim: int = 384

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        """Vectors for texts, or None when embeddings are unavailable (keyword search still works)."""


class NoEmbedder(Embedder):
    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        return None


class LocalEmbedder(Embedder):
    """fastembed (ONNX, CPU). The model loads on first use; if it cannot load, search falls back to
    keywords and the reason is logged once."""

    def __init__(self, model_name: str, cache_dir: str, threads: int = 0):
        self.model_name, self.cache_dir, self.threads = model_name, cache_dir, threads or None
        self._model = None
        self._failed = False
        self._lock = asyncio.Lock()

    async def _load(self):
        async with self._lock:
            if self._model is None and not self._failed:
                try:
                    from fastembed import TextEmbedding
                    self._model = await asyncio.to_thread(TextEmbedding, self.model_name, cache_dir=self.cache_dir,
                                                         threads=self.threads)
                except Exception as exc:  # model download blocked, disk full, ...
                    self._failed = True
                    log.warning("embedding model %s unavailable, using keyword search only: %s", self.model_name, exc)
        return self._model

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        model = await self._load()
        if model is None or not texts:
            return None
        vectors = await asyncio.to_thread(lambda: [v.tolist() for v in model.embed(texts)])
        return vectors


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "local":
        # A folder in the project, not the system temp folder (which macOS empties on restart, forcing a
        # 67 MB download again)
        cache = settings.embedding_cache_dir or str(PROJECT_ROOT / ".cache" / "fastembed")
        return LocalEmbedder(settings.local_embedding_model, cache, settings.embedding_threads)
    return NoEmbedder()
