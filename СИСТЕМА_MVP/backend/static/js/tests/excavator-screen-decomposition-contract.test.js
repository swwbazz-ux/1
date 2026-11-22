const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const backend = path.resolve(__dirname, "../../..");
const read = (...segments) => fs.readFileSync(path.join(backend, ...segments), "utf8");

test("Excavator loads its ordered work modules and caches exactly their versioned URLs", () => {
    const html = read("templates/trips/excavator_work.html");
    const worker = read("trips/views.py");
    const manifest = read("../../.github/deploy/production-files.txt");
    const version = html.match(/data-eo-pwa-version="(excavator-mobile-shell-v\d+)"/)[1];
    const scripts = [...html.matchAll(/<script src="\{% static 'js\/(excavator-work-[\w-]+\.js)' %\}\?v=([^"]+)"><\/script>/g)];
    assert.deepEqual(scripts.map(([, name]) => name), ["feedback", "runtime", "screen", "pwa", "boot"]
        .map(name => `excavator-work-${name}-v1.js`));
    assert.doesNotMatch(html, /<script\s*>/);
    for (const [, name, scriptVersion] of scripts) {
        assert.equal(scriptVersion, version);
        assert.ok(worker.includes(`"/static/js/${name}?v=${version}"`));
        assert.ok(manifest.split(/\r?\n/).includes(`СИСТЕМА_MVP/backend/static/js/${name}`));
        assert.doesNotMatch(read("static/js", name), /\{[{%]/);
    }
    const screen = read("static/js/excavator-work-screen-v1.js");
    assert.match(screen, /ExcavatorDashboardDrag\.attach\(/);
    assert.doesNotMatch(screen, /\b(activeTruckDrag|createTruckDragPreview|finishTruckDrag)\b/);
});
