"""Create (or update) the QStash schedules. Run once after deploying; running it again only updates them.

    python -m scripts.qstash_setup                 # hourly /api/jobs/sweep (JOB_RUNNER=qstash)
    python -m scripts.qstash_setup --keep-awake    # also GET /health every 10 minutes (Render free sleeps)
    python -m scripts.qstash_setup --no-keep-awake # remove that (after moving to an always-on instance)
"""
from __future__ import annotations

import asyncio
import sys

from app.config import get_settings
from app.integrations.base import IntegrationError
from app.jobs.qstash import QStash


async def main() -> int:
    s = get_settings()
    try:
        q = QStash(s.qstash_url, s.qstash_token, s.public_api_url)
        await q.ensure_sweep_schedule()
        print(f"Hourly sweep scheduled: {q.sweep_url}")
        if "--keep-awake" in sys.argv:
            await q.ensure_keep_awake()
            print(f"Keep-awake scheduled: GET {q.health_url} every 10 minutes")
        elif "--no-keep-awake" in sys.argv:
            await q.remove_keep_awake()
            print("Keep-awake schedule removed.")
        await q.aclose()
    except IntegrationError as exc:
        print(f"Failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
