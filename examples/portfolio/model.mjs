export const defaults = {
  capacity: 2,
  repairPassed: false,
  tasks: [
    {
      id: "parser-fix",
      files: ["parser.py"],
      proof: "regression fails before, passes after",
      passed: true,
    },
    {
      id: "parser-format",
      files: ["parser.py"],
      proof: "format and syntax checks",
      passed: true,
    },
    {
      id: "docs-refresh",
      files: ["README.md"],
      proof: "links resolve",
      passed: true,
    },
    {
      id: "pricing-fix",
      files: ["pricing.py"],
      proof: "EV regression",
      passed: false,
    },
  ],
};
export const controls = [
  {
    key: "capacity",
    label: "Concurrent work lanes",
    type: "number",
    min: 1,
    max: 4,
    step: 1,
  },
  {
    key: "repairPassed",
    label: "Pricing repair now passes verification",
    type: "checkbox",
  },
];
export function schedule(tasks, capacity) {
  if (!Number.isInteger(capacity) || capacity < 1)
    throw Error("Choose at least one whole lane.");
  if (
    tasks.some((t) => !t.id || !t.files?.length || !t.proof) ||
    new Set(tasks.map((t) => t.id)).size !== tasks.length
  )
    throw Error("Tasks need unique IDs, declared files and a proof.");
  const pending = [...tasks],
    batches = [];
  while (pending.length) {
    const used = new Set(),
      batch = [];
    for (let i = 0; i < pending.length && batch.length < capacity; ) {
      const t = pending[i];
      if (t.files.some((f) => used.has(f))) {
        i++;
        continue;
      }
      batch.push(t);
      t.files.forEach((f) => used.add(f));
      pending.splice(i, 1);
    }
    batches.push(batch);
  }
  return batches;
}
export function run(i) {
  const tasks = i.tasks.map((t) => ({
      ...t,
      passed: t.id === "pricing-fix" ? i.repairPassed : t.passed,
    })),
    batches = schedule(tasks, i.capacity);
  return {
    summary: "Tasks sharing a file cannot share a work batch",
    metrics: {
      "work batches": batches.length,
      "verified complete": tasks.filter((t) => t.passed).length,
      "held for repair": tasks.filter((t) => !t.passed).length,
    },
    columns: ["batch", "task", "reserved files", "independent proof", "result"],
    rows: batches.flatMap((batch, n) =>
      batch.map((t) => [
        n + 1,
        t.id,
        t.files.join(", "),
        t.proof,
        t.passed ? "verified" : "repair required",
      ]),
    ),
    steps: [
      "Turn the request into a scoped task",
      "Declare files and a falsifiable acceptance check",
      "Schedule non-conflicting work together",
      "Accept only independently checked results",
    ],
    artifact: { batches },
  };
}
