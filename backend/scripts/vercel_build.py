"""Vercel build step (see backend/pyproject.toml). Vercel deploys only backend/, so this copies content/
and the embedding model into backend/vercel_bundle/, where app/config.py looks when ../content is absent.

    python -m scripts.vercel_build
"""
import shutil
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
BUNDLE = BACKEND / "vercel_bundle"
MODEL = "BAAI/bge-small-en-v1.5"


def main() -> int:
    source = BACKEND.parent / "content"
    if not source.is_dir():
        print(f"content/ not found at {source}: in Vercel, keep 'Include files outside the root directory "
              "in the Build Step' turned on (Settings > Build and Deployment > Root Directory).", file=sys.stderr)
        return 1
    shutil.rmtree(BUNDLE, ignore_errors=True)
    shutil.copytree(source, BUNDLE / "content")
    print(f"copied {sum(1 for p in (BUNDLE / 'content').rglob('*') if p.is_file())} content files")

    from fastembed import TextEmbedding
    TextEmbedding(MODEL, cache_dir=str(BUNDLE / "fastembed"))
    size = sum(p.stat().st_size for p in (BUNDLE / "fastembed").rglob("*") if p.is_file())
    print(f"embedding model {MODEL}: {size / 1e6:.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
