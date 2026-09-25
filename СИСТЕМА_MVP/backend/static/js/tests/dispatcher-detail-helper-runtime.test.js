"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const BACKEND = path.resolve(__dirname, "..", "..", "..");
const DETAIL_SOURCE = fs.readFileSync(
    path.join(BACKEND, "static", "js", "dispatcher-detail-v1.js"),
    "utf8"
);

test("detail runtime owns the HTML escaping helper used by dynamic card markup", () => {
    assert.match(DETAIL_SOURCE, /function escapeHtml\(value\) \{/);
    assert.match(DETAIL_SOURCE, /return String\(value \|\| ""\)\.replace\(\/\[&<>"'\]\/g/);
});
