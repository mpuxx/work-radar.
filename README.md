# Work Radar

Finds UK contract work in business analysis, delivery, transformation, AI, CMS and service design,
from official public job feeds and APIs. Checks every 15 minutes on GitHub Actions; the app is on GitHub Pages.

- `python -m workradar collect` – one scheduled pass (due sources → import → dedupe → alerts → export)
- `python -m workradar discover` – weekly source discovery and validation
- `python -m workradar check-now <source_id|all>`
- `python -m unittest discover -s tests`

See HANDOVER.md, OPERATIONS.md, VALIDATION.md and COSTS.md.
State (the SQLite database and the latest SOURCE_REGISTRY.csv) lives on the `state` branch.
No secrets are stored in this repository: API keys are GitHub Actions secrets.
