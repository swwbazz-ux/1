const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const APP_CSS_PATH = path.resolve(
    __dirname,
    "..",
    "..",
    "css",
    "dispatcher-control-v1.css"
);
const APP_CSS = fs.readFileSync(APP_CSS_PATH, "utf8");

function extractAtRule(source, marker, requiredContent) {
    let searchFrom = 0;
    while (searchFrom < source.length) {
        const markerIndex = source.indexOf(marker, searchFrom);
        assert.notEqual(markerIndex, -1, `CSS at-rule was not found: ${marker}`);
        const openBrace = source.indexOf("{", markerIndex);
        assert.notEqual(openBrace, -1, `CSS at-rule has no opening brace: ${marker}`);
        let depth = 0;
        for (let index = openBrace; index < source.length; index += 1) {
            if (source[index] === "{") depth += 1;
            if (source[index] === "}") {
                depth -= 1;
                if (depth === 0) {
                    const atRule = source.slice(openBrace + 1, index);
                    if (atRule.includes(requiredContent)) {
                        return atRule;
                    }
                    searchFrom = index + 1;
                    break;
                }
            }
        }
    }
    assert.fail(`CSS at-rule does not contain: ${requiredContent}`);
}

function extractRule(source, selector) {
    const selectorIndex = source.indexOf(selector);
    assert.notEqual(selectorIndex, -1, `CSS selector was not found: ${selector}`);
    const openBrace = source.indexOf("{", selectorIndex + selector.length);
    const closeBrace = source.indexOf("}", openBrace);
    assert.notEqual(openBrace, -1, `CSS selector has no opening brace: ${selector}`);
    assert.notEqual(closeBrace, -1, `CSS selector has no closing brace: ${selector}`);
    return source.slice(openBrace + 1, closeBrace);
}

test("mobile Dispatcher keeps a full-width center column when both garages are empty", () => {
    const mobileCss = extractAtRule(
        APP_CSS,
        "@media (max-width: 1180px)",
        ".dispatcher-board {"
    );
    const bothEmptySelector =
        ".dispatcher-board.is-excavator-garage-empty.is-truck-garage-empty";
    const boardRule = extractRule(mobileCss, bothEmptySelector);

    assert.match(
        boardRule,
        /grid-template-columns:\s*minmax\(0,\s*1fr\)\s*;/,
        "Both-empty mobile state must override the wider desktop grid."
    );
    assert.match(
        boardRule,
        /column-gap:\s*10px\s*;/,
        "Both-empty mobile state must restore the normal mobile column gap."
    );
});

test("mobile Dispatcher removes the desktop empty-garage offset from complexes", () => {
    const mobileCss = extractAtRule(
        APP_CSS,
        "@media (max-width: 1180px)",
        ".dispatcher-board {"
    );
    const complexesRule = extractRule(
        mobileCss,
        ".dispatcher-board.is-excavator-garage-empty.is-truck-garage-empty .dispatcher-complexes"
    );

    assert.match(
        complexesRule,
        /margin-left:\s*0\s*;/,
        "The center workspace must not retain the hidden desktop garage offset."
    );
});

test("mobile Dispatcher expands the unified header instead of clipping navigation", () => {
    const mobileCss = extractAtRule(
        APP_CSS,
        "@media (max-width: 1180px)",
        ".dispatcher-board {"
    );
    const topbarRule = extractRule(
        mobileCss,
        ".dispatcher-board.is-excavator-garage-empty.is-truck-garage-empty .dispatcher-topbar.dispatcher-unified-topbar"
    );
    const headerRule = extractRule(
        mobileCss,
        ".dispatcher-board.is-excavator-garage-empty.is-truck-garage-empty .dispatcher-topbar.dispatcher-unified-topbar .dispatcher-header-row"
    );

    for (const rule of [topbarRule, headerRule]) {
        assert.match(rule, /height:\s*auto\s*;/);
        assert.match(rule, /min-height:\s*0\s*;/);
        assert.match(rule, /max-height:\s*none\s*;/);
    }
});
