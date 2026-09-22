import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { validateTasks, nextWave, execute, conflict } from "./model.mjs";
const workflow = JSON.parse(
    readFileSync(new URL("./data/workflow.json", import.meta.url)),
);
test("actual complete workflow produces independently reconciled artifacts", async () => {
    validateTasks(workflow.tasks);
    const files = { ...workflow.files },
        states = Object.fromEntries(
            workflow.tasks.map((t) => [t.id, "pending"]),
        );
    while (true) {
        const wave = nextWave(workflow.tasks, states, 3);
        if (!wave.length) break;
        for (const t of wave) {
            const r = await execute(t, files);
            Object.assign(files, r.outputs);
            states[t.id] = "passed";
        }
    }
    assert.ok(Object.values(states).every((s) => s === "passed"));
    assert.equal(JSON.parse(files["proof/totals.json"]).checkedOrders, 220);
    assert.equal(JSON.parse(files["proof/schema.json"]).rows, 240);
    assert.match(files["output/report.md"], /research/);
    assert.equal(JSON.parse(files["proof/input-hash.json"]).sha256.length, 64);
});
test("bad input fails verification instead of receiving success", async () => {
    const task = workflow.tasks.find((t) => t.action === "validate");
    await assert.rejects(
        () =>
            execute(task, {
                [task.reads[0]]: JSON.stringify([
                    { id: "1", team: "x", amount: 2, quantity: -1 },
                ]),
            }),
        /invalid/,
    );
    const reconcile = workflow.tasks.find((t) => t.action === "reconcile");
    await assert.rejects(
        () =>
            execute(reconcile, {
                [reconcile.reads[0]]:
                    '[{"id":"1","team":"x","amount":10,"quantity":2}]',
                [reconcile.reads[1]]:
                    '[{"team":"x","orders":1,"units":2,"revenue":999}]',
            }),
        /Reconciliation/,
    );
});
test("dependencies, capacity and read-write conflicts are enforced", () => {
    const a = { id: "a", depends: [], reads: ["a"], writes: ["b"] },
        b = { id: "b", depends: [], reads: ["b"], writes: ["c"] },
        c = { id: "c", depends: ["a"], reads: ["x"], writes: ["y"] };
    assert.ok(conflict(a, b));
    assert.deepEqual(
        nextWave(
            [a, b, c],
            { a: "pending", b: "pending", c: "pending" },
            3,
        ).map((t) => t.id),
        ["a"],
    );
    assert.equal(nextWave([a, b], { a: "running", b: "pending" }, 1).length, 0);
});
test("cycles, undeclared dependencies and unsafe paths fail validation", () => {
    const t = structuredClone(workflow.tasks);
    t[0].depends = ["package"];
    assert.throws(() => validateTasks(t), /cycle/);
    t[0].depends = ["unknown"];
    assert.throws(() => validateTasks(t), /Missing/);
    t[0].depends = [];
    t[0].writes = ["../private"];
    assert.throws(() => validateTasks(t), /relative/);
});
