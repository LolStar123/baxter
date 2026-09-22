export function validateTasks(tasks) {
    if (!Array.isArray(tasks) || !tasks.length || tasks.length > 50)
        throw Error("Add at least one task.");
    const ids = new Set();
    for (const t of tasks) {
        if (!/^[a-z0-9-]+$/.test(t.id) || ids.has(t.id))
            throw Error(
                "Task IDs must be unique lowercase words with hyphens.",
            );
        ids.add(t.id);
        if (!ACTIONS.includes(t.action))
            throw Error("Unknown action: " + t.action);
        if (
            !Array.isArray(t.depends) ||
            !Array.isArray(t.reads) ||
            !Array.isArray(t.writes) ||
            !t.reads.length ||
            !t.writes.length
        )
            throw Error("Declare dependencies, inputs and outputs.");
        for (const f of [...t.reads, ...t.writes])
            if (
                !/^[a-zA-Z0-9_./-]+$/.test(f) ||
                f.startsWith("/") ||
                f.includes("..")
            )
                throw Error("Use safe relative artifact paths.");
    }
    const visit = (id, stack, done) => {
        if (stack.has(id)) throw Error("Dependency cycle at " + id);
        if (done.has(id)) return;
        const t = tasks.find((t) => t.id === id);
        if (!t) throw Error("Missing dependency: " + id);
        stack.add(id);
        for (const d of t.depends) visit(d, stack, done);
        stack.delete(id);
        done.add(id);
    };
    const done = new Set();
    for (const t of tasks) visit(t.id, new Set(), done);
    return true;
}
export const ACTIONS = [
    "parse",
    "validate",
    "dedupe",
    "aggregate",
    "report",
    "reconcile",
    "copy",
    "hash",
    "manifest",
];
export function conflict(a, b) {
    return (
        a.writes.some((f) => [...b.reads, ...b.writes].includes(f)) ||
        b.writes.some((f) => a.reads.includes(f))
    );
}
export function nextWave(tasks, states, capacity) {
    const running = tasks.filter((t) => states[t.id] === "running"),
        selected = [];
    for (const task of tasks) {
        if (
            states[task.id] !== "pending" ||
            !task.depends.every((id) => states[id] === "passed")
        )
            continue;
        if (running.length + selected.length >= capacity) break;
        if ([...running, ...selected].some((t) => conflict(t, task))) continue;
        selected.push(task);
    }
    return selected;
}
const json = (value) => JSON.stringify(value, null, 2);
export async function execute(task, files) {
    for (const f of task.reads)
        if (typeof files[f] !== "string") throw Error("Missing input: " + f);
    const text = files[task.reads[0]],
        read = () => JSON.parse(text);
    let value, proof;
    switch (task.action) {
        case "parse": {
            const lines = text.trim().split(/\r?\n/),
                header = lines.shift().split(",");
            if (header.join(",") !== "id,team,amount,quantity")
                throw Error("Expected id,team,amount,quantity CSV columns.");
            value = lines.map((line, i) => {
                const a = line.split(",");
                if (a.length !== 4 || a.some((v) => !v.trim()))
                    throw Error("Malformed CSV row " + (i + 2));
                return {
                    id: a[0],
                    team: a[1],
                    amount: Number(a[2]),
                    quantity: Number(a[3]),
                };
            });
            proof = `Parsed ${value.length} rows`;
            break;
        }
        case "validate": {
            const rows = read();
            const bad = rows.filter(
                (r) =>
                    !r.id ||
                    !r.team ||
                    !Number.isFinite(r.amount) ||
                    r.amount < 0 ||
                    !Number.isInteger(r.quantity) ||
                    r.quantity <= 0,
            );
            if (bad.length)
                throw Error(
                    `${bad.length} invalid rows: amount must be non-negative and quantity a positive integer.`,
                );
            value = { valid: true, rows: rows.length };
            proof = `Checked every field in ${rows.length} rows`;
            break;
        }
        case "dedupe": {
            const rows = read(),
                seen = new Set();
            value = rows.filter((r) => {
                if (seen.has(r.id)) return false;
                seen.add(r.id);
                return true;
            });
            proof = `Kept first occurrence of each ID: ${rows.length} -> ${value.length}`;
            break;
        }
        case "aggregate": {
            const rows = read(),
                groups = Object.create(null);
            for (const r of rows) {
                const g = (groups[r.team] ??= {
                    team: r.team,
                    orders: 0,
                    units: 0,
                    revenue: 0,
                });
                g.orders++;
                g.units += r.quantity;
                g.revenue += r.amount * r.quantity;
            }
            value = Object.values(groups);
            proof = `Aggregated ${rows.length} orders across ${value.length} teams`;
            break;
        }
        case "report": {
            const rows = read();
            value =
                "# Team order report\n\n| Team | Orders | Units | Revenue |\n| --- | ---: | ---: | ---: |\n" +
                rows
                    .map(
                        (r) =>
                            `| ${r.team} | ${r.orders} | ${r.units} | ${r.revenue.toFixed(2)} |`,
                    )
                    .join("\n");
            proof = `Wrote ${rows.length} team rows`;
            break;
        }
        case "reconcile": {
            const rows = read(),
                summary = JSON.parse(files[task.reads[1]]);
            for (const team of new Set(rows.map((r) => r.team))) {
                const source = rows.filter((r) => r.team === team),
                    target = summary.find((r) => r.team === team);
                if (
                    !target ||
                    target.orders !== source.length ||
                    target.units !==
                        source.reduce((n, r) => n + r.quantity, 0) ||
                    Math.abs(
                        target.revenue -
                            source.reduce(
                                (n, r) => n + r.amount * r.quantity,
                                0,
                            ),
                    ) > 1e-8
                )
                    throw Error("Reconciliation failed for " + team);
            }
            if (summary.length !== new Set(rows.map((r) => r.team)).size)
                throw Error("Unexpected extra team");
            value = {
                passed: true,
                checkedOrders: rows.length,
                checkedTeams: summary.length,
            };
            proof = "Independent totals match every team";
            break;
        }
        case "copy":
            value = text;
            proof = `Copied ${text.length} characters`;
            break;
        case "hash": {
            const bytes = new TextEncoder().encode(text),
                hash = await crypto.subtle.digest("SHA-256", bytes);
            value = {
                file: task.reads[0],
                sha256: [...new Uint8Array(hash)]
                    .map((x) => x.toString(16).padStart(2, "0"))
                    .join(""),
                bytes: bytes.length,
            };
            proof = "SHA-256 calculated from actual input bytes";
            break;
        }
        case "manifest":
            value = task.reads.map((path) => ({
                path,
                bytes: new TextEncoder().encode(files[path]).length,
            }));
            proof = `Manifest includes ${value.length} existing artifacts`;
            break;
        default:
            throw Error("Unsupported action");
    }
    if (task.writes.length !== 1)
        throw Error("These actions produce exactly one output.");
    return {
        outputs: {
            [task.writes[0]]: typeof value === "string" ? value : json(value),
        },
        proof,
    };
}
