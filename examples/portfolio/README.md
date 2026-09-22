# Baxter workflow desk

Serve with `python -m http.server 8000 --directory examples/portfolio`, then open http://localhost:8000. Run `node --test examples/portfolio/model.test.mjs` and `node tools/run_workflow.mjs` from the repository root.

The browser runner executes the same jobs in Web Workers. Tests verify actual artifacts, reconciliation failures, dependency cycles and file conflicts. For browser checks, install Playwright and Chromium, then run `python tools/browser_audit.py`.

[System guide](../../README.md) | [Scope](../../PROVENANCE.md)
