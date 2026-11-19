---
description: Run the behaviour checks against the real model and explain the failures
---
1. Run `cd backend && python -m evals.run` (needs the model key in `.env`).
2. Read `backend/evals/report.md`.
3. For each failed case, say in one line whether the cause is the instructions (`app/assistant/prompts.py`),
   missing content (`content/`), a tool (`app/assistant/tools.py`) or the check itself (`evals/cases.yaml`),
   and propose the smallest fix. Do not change a check just to make it pass.
4. Note any "guard replaced" entries: the visitor was protected, but the instructions may need tightening.
