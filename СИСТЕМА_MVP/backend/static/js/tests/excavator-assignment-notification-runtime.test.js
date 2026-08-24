const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(
    path.resolve(__dirname, "../../../templates/trips/excavator_work.html"),
    "utf8"
);

function extractFunction(name) {
    const marker = `function ${name}(`;
    const start = source.indexOf(marker);
    assert.notEqual(start, -1, `${name} must exist in the excavator template.`);
    const bodyStart = source.indexOf("{", start);
    let depth = 0;
    let quote = "";
    let escaped = false;
    for (let index = bodyStart; index < source.length; index += 1) {
        const char = source[index];
        if (quote) {
            if (escaped) escaped = false;
            else if (char === "\\") escaped = true;
            else if (char === quote) quote = "";
            continue;
        }
        if (char === '"' || char === "'" || char === "`") {
            quote = char;
            continue;
        }
        if (char === "{") depth += 1;
        if (char === "}") depth -= 1;
        if (depth === 0) return source.slice(start, index + 1);
    }
    throw new Error(`Could not extract ${name}.`);
}

function createRuntime() {
    const calls = {alerts: 0, notices: []};
    const context = vm.createContext({
        Object,
        calls,
        playExcavatorAssignmentAlert() {
            calls.alerts += 1;
        },
        showExcavatorNotice(message) {
            calls.notices.push(message);
        }
    });
    vm.runInContext(
        extractFunction("announceExcavatorAssignmentChanges"),
        context,
        {filename: "excavator_work.assignment-notification.js"}
    );
    return context;
}

test("a new assigned truck produces one operator alert with its number", () => {
    const runtime = createRuntime();

    runtime.announceExcavatorAssignmentChanges(
        {"12": "12"},
        {"12": "12", "38": "38"}
    );

    assert.equal(runtime.calls.alerts, 1);
    assert.deepEqual(runtime.calls.notices, ["Назначен самосвал 38."]);
});

test("an unchanged assignment snapshot produces no duplicate alert", () => {
    const runtime = createRuntime();

    runtime.announceExcavatorAssignmentChanges(
        {"38": "38"},
        {"38": "38"}
    );

    assert.equal(runtime.calls.alerts, 0);
    assert.deepEqual(runtime.calls.notices, []);
});
