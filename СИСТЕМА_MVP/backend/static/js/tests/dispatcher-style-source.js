"use strict";

const fs = require("node:fs");
const path = require("node:path");

const BACKEND = path.resolve(__dirname, "..", "..", "..");
const DISPATCHER_STYLE_FILES = [
    "dispatcher-control-v1.css",
    "dispatcher-workspace-v1.css",
    "dispatcher-detail-v1.css",
    "dispatcher-adaptive-v1.css",
    "dispatcher-detail-overrides-v1.css",
];

function dispatcherStyleSource() {
    return DISPATCHER_STYLE_FILES
        .map((name) => fs.readFileSync(path.join(BACKEND, "static", "css", name), "utf8"))
        .join("");
}

module.exports = {
    DISPATCHER_STYLE_FILES,
    dispatcherStyleSource,
};
