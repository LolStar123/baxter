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
    active = 0,
    inputName = "orders.csv",
    inputOrigin = "synthetic";
function render() {
    const report = files["output/report.md"];
    const verified = tasks.length > 0 && tasks.every((task) => states[task.id] === "passed");
    $("#report-preview").hidden = !report || !verified;
    $("#input-name").textContent = inputName;
    const rows = Math.max(0, (inputs["input/orders.csv"] || "").trim().split(/\r?\n/).length - 1);
    $("#input-meta").textContent = `${rows} source rows · ${inputOrigin}`;
    const summary = files["output/summary.json"]
        ? JSON.parse(files["output/summary.json"])
        : null;
    $("#report-preview").innerHTML =
        report && verified && Array.isArray(summary)
            ? `<table><thead><tr><th>team</th><th>orders</th><th>units</th><th>revenue</th></tr></thead><tbody>${summary.map((r) => `<tr><td>${esc(r.team)}</td><td>${r.orders}</td><td>${r.units}</td><td>${Number(r.revenue).toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td></tr>`).join("")}</tbody></table>`
            : esc(report || "");
    $("#output-summary").textContent = "Report";
    const passed = Object.values(states).filter((s) => s === "passed").length;
    const failed = Object.values(states).some((s) => s === "failed");
    const reportPanel = $(".report-preview"), queue = $(".queue");
    reportPanel.hidden = !verified && !failed;
    queue.open = running || failed;
    if (verified) queue.before(reportPanel);
    else queue.after(reportPanel);
    $("#run-phase").textContent = running
        ? "Running"
        : failed
          ? "Blocked"
          : verified && report
            ? "Verified"
            : "Ready";
    $("#run-count").textContent = running || verified || failed
        ? `${passed} / ${tasks.length} jobs`
        : `${tasks.length} jobs`;
    const stageRoles = {
        scope: "product manager",
        schedule: "baxter",
        execute: "developer",
        verify: "verifier",
    };
    const stageState = (role) => {
        const roleTasks = tasks.filter((task) => task.role === role);
        const statesForRole = roleTasks.map((task) => states[task.id]);
        if (!statesForRole.length || statesForRole.every((state) => state === "pending" || !state)) return "pending";
        if (statesForRole.some((state) => state === "failed" || state === "blocked")) return "failed";
        if (statesForRole.some((state) => state === "running")) return "running";
        if (statesForRole.every((state) => state === "passed")) return "passed";
        return "active";
    };
    document.querySelectorAll("[data-beat]").forEach((node) => {
        const state = stageState(stageRoles[node.dataset.beat]);
        node.dataset.state = state;
        node.dataset.active = ["running", "active", "passed"].includes(state) ? "true" : "false";
    });
    const roleClass = (role) => String(role || "baxter").replaceAll(" ", "-");
    $("#tasks").innerHTML = tasks
        .map((t, i) => {
            const receipt = receipts.findLast((r) => r.id === t.id);
            return `<article class="task" data-state="${states[t.id]}"><span class="avatar ${roleClass(t.role)}" aria-hidden="true"></span><div><span class="role">${esc(t.role || "baxter")}</span><h3>${esc(t.title || t.id)}</h3>${receipt ? `<p class="proof">${esc(receipt.proof || receipt.error)}</p>` : ""}</div><span class="state ${states[t.id]}">${states[t.id]}</span></article>`;
        })
        .join("");
    for (const id of [
        "run",
        "break",
        "reset",
        "apply",
        "save-input",
        "capacity",
        "input-file",
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
            .join("") || "<p>No receipts yet.</p>";
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
            ? receipts.find((receipt) => !receipt.ok)?.error || "A prerequisite failed. Inspect the run thread."
            : "";
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
    if (running || !original) return;
    resetRun();
    running = true;
    $("#status").textContent = "running…";
    pump();
};
$("#file").onchange = showFile;
$("#save-input").onclick = () => {
    inputs[$("#file").value] = $("#content").value;
    if ($("#file").value === "input/orders.csv") inputOrigin = "edited";
    resetRun();
    $("#status").textContent =
        "Input saved.";
};
$("#break").onclick = () => {
    inputs["input/orders.csv"] = inputs["input/orders.csv"].replace(
        /(\n[^\n]*,)\d+(?=\n|$)/,
        "$1-1",
    );
    resetRun();
    $("#status").textContent =
        "Negative quantity set.";
};
$("#reset").onclick = () => {
    tasks = structuredClone(original.tasks);
    inputs = { ...original.files };
    inputName = "orders.csv";
    inputOrigin = "synthetic";
    $("#input-status").textContent = "";
    $("#task-error").textContent = "";
    $("#definitions").value = JSON.stringify(tasks, null, 2);
    resetRun();
    $("#status").textContent =
        "";
};
$("#input-file").onchange = async (event) => {
    const file = event.target.files[0];
    if (!file || running) return;
    try {
        if (!/\.csv$/i.test(file.name)) throw Error("Choose a .csv file with id, team, amount and quantity columns.");
        if (file.size > 250000) throw Error("Choose a CSV under 250 KB.");
        const text = await file.text();
        if (running) return;
        if (!text.trim()) throw Error("The CSV is empty. Choose a file with order rows.");
        inputs["input/orders.csv"] = text;
        inputName = file.name;
        inputOrigin = "uploaded";
        $("#input-status").textContent = "";
        resetRun();
        $("#status").textContent = "Input replaced.";
    } catch (error) {
        $("#input-status").textContent = error.message;
    } finally {
        event.target.value = "";
    }
};
$("#apply").onclick = () => {
    try {
        const next = JSON.parse($("#definitions").value);
        validateTasks(next);
        tasks = next;
        resetRun();
        $("#task-error").textContent = "";
        $("#status").textContent =
            "Workflow updated.";
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
    $("#status").textContent = `${e.message}. Refresh to retry.`;
    for (const id of ["run", "break", "reset", "apply", "save-input", "export", "input-file"]) $("#" + id).disabled = true;
}
