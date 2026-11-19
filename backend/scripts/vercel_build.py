"""Vercel build step (backend/vercel.json; it runs after requirements.txt is installed). Vercel deploys only
backend/, so this copies content/ and the embedding model into backend/vercel_bundle/, where app/config.py
looks when ../content is absent. Keep backend/ free of a pyproject.toml: Vercel would install from it instead
of requirements.txt.

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

    try:
        from fastembed import TextEmbedding
        TextEmbedding(MODEL, cache_dir=str(BUNDLE / "fastembed"))
    except Exception as exc:  # the API then downloads it into the temp folder on first use
        shutil.rmtree(BUNDLE / "fastembed", ignore_errors=True)
        print(f"WARNING: embedding model not bundled ({exc}); it will download on the first cold start")
        return 0
    size = sum(p.stat().st_size for p in (BUNDLE / "fastembed").rglob("*") if p.is_file())
    print(f"embedding model {MODEL}: {size / 1e6:.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
