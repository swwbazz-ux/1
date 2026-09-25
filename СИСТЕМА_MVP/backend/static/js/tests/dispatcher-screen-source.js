"use strict";

const fs = require("node:fs");
const path = require("node:path");

const BACKEND = path.resolve(__dirname, "..", "..", "..");
const TEMPLATE_PATH = path.join(
    BACKEND,
    "templates",
    "trips",
    "dispatcher_control.html"
);
const INCLUDE_FILES = [
    [
        "trips/includes/dispatcher_board.html",
        path.join(BACKEND, "templates", "trips", "includes", "dispatcher_board.html"),
    ],
    [
        "trips/includes/dispatcher_service_lists.html",
        path.join(BACKEND, "templates", "trips", "includes", "dispatcher_service_lists.html"),
    ],
    [
        "trips/includes/dispatcher_equipment_detail.html",
        path.join(BACKEND, "templates", "trips", "includes", "dispatcher_equipment_detail.html"),
    ],
    [
        "trips/includes/dispatcher_push_invite.html",
        path.join(BACKEND, "templates", "trips", "includes", "dispatcher_push_invite.html"),
    ],
];

function normalize(source) {
    return source.replace(/\r\n?/g, "\n");
}

function dispatcherScreenSource() {
    let source = normalize(fs.readFileSync(TEMPLATE_PATH, "utf8"));
    for (const [templateName, filePath] of INCLUDE_FILES) {
        const marker = `{% include "${templateName}" %}`;
        if (!source.includes(marker)) {
            throw new Error(`Dispatcher template include is missing: ${templateName}`);
        }
        source = source.replace(marker, normalize(fs.readFileSync(filePath, "utf8")));
    }
    return source;
}

module.exports = {dispatcherScreenSource};
