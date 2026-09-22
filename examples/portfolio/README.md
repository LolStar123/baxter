# baxter: working example

Schedule overlapping tasks, change capacity and see failed evidence held for repair.

**[Open the demo](https://lolstar123.github.io/baxter/)** · [Calculation / workflow code](model.mjs) · [Checks](model.test.mjs)

![Example output](preview.png)

## Run it

From the repository root, with Python 3 and Node.js 22:

```sh
python -m http.server 8000 --directory examples/portfolio
```

Open http://localhost:8000. Change an input, or edit the JSON fixture, then export the computed result as JSON or CSV.

```sh
node --test examples/portfolio/model.test.mjs
```

## What it does

Triage the request, define a task and reserve the files it needs. Work with conflicting edits waits its turn. A separate verification step checks the result before Baxter marks it done.

## Scope and source

A deterministic scheduler and verification example. It does not contact inboxes or launch paid agents.

Public baxter_usage.py, baxter_lanes.py, baxter_verify.py and PRD workflow.

`model.mjs` is the small public implementation. `app.mjs` connects its inputs and outputs to the browser. No package install or network key is needed to run the example. GitHub Pages runs the same files after the checks pass.
