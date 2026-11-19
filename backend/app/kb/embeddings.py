"""Text embeddings for knowledge search, computed on this server (no API cost, no data leaves it)."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

from app.config import BUNDLE_DIR, PROJECT_ROOT, Settings

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
                    cache = await asyncio.to_thread(writable_cache, Path(self.cache_dir))
                    self._model = await asyncio.to_thread(TextEmbedding, self.model_name, cache_dir=str(cache),
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


def writable_cache(folder: Path) -> Path:
    """The model folder, or a copy of it in the temp folder when it is read-only (a serverless bundle):
    the model library writes lock files next to the model."""
    if not folder.is_dir() or os.access(folder, os.W_OK):
        return folder
    copy = Path(tempfile.gettempdir()) / "fastembed"
    if not copy.is_dir():
        partial = Path(tempfile.mkdtemp(prefix="fastembed-"))
        shutil.copytree(folder, partial, dirs_exist_ok=True)
        try:
            partial.rename(copy)
        except OSError:  # another request finished the copy first
            shutil.rmtree(partial, ignore_errors=True)
    return copy


def default_cache() -> str:
    """The model built into a serverless bundle, else a folder in the project, not the system temp folder
    (which macOS empties on restart, forcing a 67 MB download again)."""
    bundled = BUNDLE_DIR / "fastembed"
    return str(bundled if bundled.is_dir() else PROJECT_ROOT / ".cache" / "fastembed")


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "local":
        cache = settings.embedding_cache_dir or default_cache()
        return LocalEmbedder(settings.local_embedding_model, cache, settings.embedding_threads)
    return NoEmbedder()
