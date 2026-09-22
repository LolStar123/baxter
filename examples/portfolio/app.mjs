import { validateTasks, nextWave } from "./model.mjs";
const $ = (s) => document.querySelector(s),
    esc = (s) =>
        String(s ?? "").replace(
            /[&<>"']/g,
            (c) =>
                ({
                    "&": "&amp;",
                    "<": "&lt;",
                    ">": "&gt;",
                    '"': "&quot;",
                    "'": "&#39;",
                })[c],
        );
let original,
    tasks = [],
    files = {},
    inputs = {},
    states = {},
    receipts = [],
    running = false,
    active = 0;
function render() {
    $("#tasks").innerHTML = tasks
        .map((t, i) => {
            const receipt = receipts.findLast((r) => r.id === t.id);
            return `<article class="task"><span class="number">${String(i + 1).padStart(2, "0")}</span><div><h3>${esc(t.title || t.id)}</h3><p>${esc(t.role || "developer")} / ${esc(t.action)} / waits for ${esc(t.depends.join(", ") || "nothing")}</p><p>${esc(t.writes.join(", "))}</p>${receipt ? `<p class="proof">${esc(receipt.proof || receipt.error)}</p>` : ""}</div><span class="state ${states[t.id]}">${states[t.id]}</span></article>`;
        })
        .join("");
    const passed = Object.values(states).filter((s) => s === "passed").length;
    $("#progress").textContent = `${passed} / ${tasks.length} passed`;
    for (const id of [
        "run",
        "break",
        "reset",
        "apply",
        "save-input",
        "capacity",
    ])
        $("#" + id).disabled = running;
    $("#export").disabled = !receipts.length;
    $("#definitions").disabled = running;
    const selected = $("#file").value;
    $("#file").innerHTML = Object.keys(files)
        .map((f) => `<option>${esc(f)}</option>`)
        .join("");
    if (files[selected] !== undefined) $("#file").value = selected;
    showFile();
    $("#receipts").innerHTML =
        receipts
            .map(
                (r) =>
                    `<div class="receipt"><b>${esc(r.id)} / ${r.ok ? "passed" : "failed"}</b><p>${esc(r.proof || r.error)}</p><p>${esc(r.outputs.join(", "))}</p><time>${r.elapsed.toFixed(2)} ms / ${esc(r.time)}</time></div>`,
            )
            .join("") || "<p>Run the workflow to collect receipts.</p>";
    window.__baxter = {
        ready: true,
        running,
        states: { ...states },
        files: Object.keys(files),
        receipts: receipts.length,
    };
}
function showFile() {
    const f = $("#file").value;
    $("#content").value = files[f] || "";
    $("#content").readOnly = running || !Object.hasOwn(inputs, f);
    $("#save-input").disabled = running || !Object.hasOwn(inputs, f);
}
function resetRun() {
    files = { ...inputs };
    states = Object.fromEntries(tasks.map((t) => [t.id, "pending"]));
    receipts = [];
    active = 0;
    render();
}
function pump() {
    const capacity = Number($("#capacity").value),
        wave = nextWave(tasks, states, capacity);
    for (const t of wave) {
        states[t.id] = "running";
        active++;
        const started = performance.now(),
            worker = new Worker("./worker.mjs", { type: "module" });
        let settled = false;
        const done = (result) => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            worker.terminate();
            active--;
            if (result.ok) {
                for (const path of Object.keys(result.outputs))
                    if (!t.writes.includes(path)) {
                        result = {
                            ok: false,
                            error: "Worker attempted an undeclared output",
                        };
                        break;
                    }
            }
            if (result.ok) Object.assign(files, result.outputs);
            states[t.id] = result.ok ? "passed" : "failed";
            receipts.push({
                id: t.id,
                ok: result.ok,
                proof: result.proof,
                error: result.error,
                outputs: Object.keys(result.outputs || {}),
                elapsed: performance.now() - started,
                time: new Date().toISOString(),
            });
            render();
            pump();
        };
        const timer = setTimeout(
            () =>
                done({
                    ok: false,
                    error: "Worker exceeded its 10 second execution limit",
                }),
            10000,
        );
        worker.onmessage = (e) => done(e.data);
        worker.onerror = (e) => done({ ok: false, error: e.message });
        worker.postMessage({
            task: t,
            files: Object.fromEntries(t.reads.map((f) => [f, files[f]])),
        });
    }
    if (!active) {
        for (const t of tasks)
            if (states[t.id] === "pending") states[t.id] = "blocked";
        running = false;
        const failed = Object.values(states).some((s) => s !== "passed");
        $("#status").textContent = failed
            ? "Handoff held. Inspect the failed receipt, fix the input, then rerun."
            : "Workflow finished. Every job passed; inspect the report and verification receipts.";
        render();
        if (!failed) {
            $("#file").value = files["output/report.md"]
                ? "output/report.md"
                : Object.keys(files).at(-1);
            showFile();
        }
    } else render();
}
$("#run").onclick = () => {
    resetRun();
    running = true;
    $("#status").textContent =
        "Running declared jobs and checking their outputs...";
    pump();
};
$("#file").onchange = showFile;
$("#save-input").onclick = () => {
    inputs[$("#file").value] = $("#content").value;
    resetRun();
    $("#status").textContent =
        "Input saved. Run the workflow to regenerate and verify its outputs.";
};
$("#break").onclick = () => {
    inputs["input/orders.csv"] = inputs["input/orders.csv"].replace(
        /(\n[^\n]*,)\d+(?=\n|$)/,
        "$1-1",
    );
    resetRun();
    $("#status").textContent =
        "One order now has a negative quantity. Run it and inspect the verifier.";
};
$("#reset").onclick = () => {
    tasks = structuredClone(original.tasks);
    inputs = { ...original.files };
    $("#definitions").value = JSON.stringify(tasks, null, 2);
    resetRun();
    $("#status").textContent =
        "Restored 240 synthetic source rows and the original workflow.";
};
$("#apply").onclick = () => {
    try {
        const next = JSON.parse($("#definitions").value);
        validateTasks(next);
        tasks = next;
        resetRun();
        $("#task-error").textContent = "";
        $("#status").textContent =
            "Task changes applied. Run the workflow to test them.";
    } catch (e) {
        $("#task-error").textContent = e.message;
    }
};
for (const [id, receiptsTab] of [
    ["show-artifacts", false],
    ["show-receipts", true],
])
    $("#" + id).onclick = () => {
        $("#artifact-panel").hidden = receiptsTab;
        $("#receipts").hidden = !receiptsTab;
        $("#show-artifacts").setAttribute("aria-pressed", !receiptsTab);
        $("#show-receipts").setAttribute("aria-pressed", receiptsTab);
    };
$("#export").onclick = () => {
    const result = { tasks, files, states, receipts },
        url = URL.createObjectURL(
            new Blob([JSON.stringify(result, null, 2)], {
                type: "application/json",
            }),
        );
    const a = document.createElement("a");
    a.href = url;
    a.download = "baxter-workflow-receipts.json";
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
};
try {
    const r = await fetch("data/workflow.json");
    if (!r.ok) throw Error("Example workflow could not load");
    original = await r.json();
    validateTasks(original.tasks);
    $("#reset").click();
} catch (e) {
    $("#status").textContent = e.message;
    throw e;
}
