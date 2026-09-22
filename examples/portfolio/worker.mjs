import { execute } from "./model.mjs";
self.onmessage = async ({ data }) => {
    try {
        self.postMessage({
            ok: true,
            ...(await execute(data.task, data.files)),
        });
    } catch (e) {
        self.postMessage({ ok: false, error: e.message });
    }
};
