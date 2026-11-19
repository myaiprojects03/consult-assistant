---
description: Add or update an approved answer in the knowledge base
argument-hint: <question and approved answer>
---
Add this to the assistant's approved knowledge: $ARGUMENTS

1. Decide the kind: a frequently asked question goes in `content/faqs.yaml`, a service in
   `content/services.yaml`, a policy in `content/policies.yaml`, a company fact in `content/company.yaml`.
2. If an entry on the same topic exists, update it instead of adding a duplicate. Otherwise add an entry
   with a new unique `slug` (lowercase, hyphens), a `title` phrased the way visitors ask, and a `body` of
   2 to 4 plain sentences.
3. Never add prices unless the user gives the exact figure; if they do, also add it to
   `allowed_price_figures` in `content/rules.yaml`, or the output guard will remove it.
4. Run `cd backend && pytest tests/test_content_and_storage.py -q` to check the content still loads.
5. Tell the user to restart the backend (content syncs at startup) and show the entry you wrote.
