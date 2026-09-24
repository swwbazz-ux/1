"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
    DEFAULT_CSS_FILES,
    auditCssSources,
    auditDefaultDispatcherCss,
} = require("../../../tools/audit_dispatcher_css.cjs");

test("dispatcher CSS audit distinguishes exact redundancy from intentional overrides", () => {
    const report = auditCssSources([
        {
            name: "sample.css",
            source: `
                .tile { color: red; padding: 4px; }
                .tile { color: red; padding: 6px; }
                @media (max-width: 600px) { .tile { color: red; } }
            `,
        },
    ]);

    assert.equal(report.totalRules, 3);
    assert.equal(report.repeatedContextSelectors, 1);
    assert.equal(report.exactRedundantDeclarationCount, 1);
    assert.equal(report.conflictingPropertyCount, 1);
    assert.equal(report.exactDuplicateRuleGroups, 0);
});

test("dispatcher CSS audit characterizes the current six-file cascade", () => {
    const report = auditDefaultDispatcherCss();

    assert.deepEqual(report.files, DEFAULT_CSS_FILES);
    assert.equal(report.totalRules, 1977);
    assert.equal(report.uniqueContextSelectors, 1919);
    assert.equal(report.repeatedContextSelectors, 53);
    assert.equal(report.additiveRepeatedSelectors, 17);
    assert.equal(report.exactDuplicateRuleGroups, 0);
    assert.equal(report.exactRedundantDeclarationCount, 1);
    assert.equal(report.conflictingPropertyCount, 69);
    assert.equal(report.selectorsWithConflicts, 36);
});
