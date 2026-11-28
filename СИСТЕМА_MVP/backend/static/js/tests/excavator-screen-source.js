const fs = require("node:fs");
const path = require("node:path");

const backend = path.resolve(__dirname, "../../..");

function excavatorScreenSource(templatePath = path.join(backend, "templates/trips/excavator_work.html")) {
    const html = fs.readFileSync(templatePath, "utf8");
    const scripts = [...html.matchAll(/\{% static 'js\/(excavator-work-[\w-]+\.js)' %\}/g)];
    return [html, ...scripts.map(([, filename]) => fs.readFileSync(path.join(backend, "static/js", filename), "utf8"))].join("\n");
}

module.exports = {excavatorScreenSource};
