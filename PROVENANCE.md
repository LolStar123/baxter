# Baxter working desk

The public repository retains the original sanitised orchestration modules, including concurrency control, triage, task review and independent verification. The browser desk is a separate executable illustration of those coordination rules, using deterministic JavaScript actions rather than external agents.

The 240 input rows are synthetic and deliberately contain repeated IDs. Ten actual jobs parse, validate, deduplicate, aggregate, report, reconcile, hash and package their data. Each output is generated from input bytes at runtime. Receipts record the actual result and measured duration; no pass/fail outcome is a preset display flag.

The same model is runnable through tools/run_workflow.mjs. Custom workflows may use the declared built-in actions, not arbitrary shell commands. The original full system has broader capabilities, integrations and verification policies; this demo does not claim to emulate all of them. No inbox access, API keys, personal task queue or private operational state is included.
