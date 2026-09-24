"use strict";

const fs = require("node:fs");
const path = require("node:path");

const DEFAULT_CSS_FILES = Object.freeze([
    "dispatcher-control-v1.css",
    "dispatcher-workspace-v1.css",
    "dispatcher-detail-v1.css",
    "dispatcher-adaptive-v1.css",
    "dispatcher-detail-overrides-v1.css",
    "dispatcher-canvas-v1.css",
]);

const NESTED_AT_RULE = /^@(media|supports|layer|container|scope|document)\b/i;
const NON_STYLE_AT_RULE = /^@(keyframes|-webkit-keyframes|font-face|page|property|counter-style)\b/i;

function normalizeWhitespace(value) {
    return String(value || "")
        .replace(/\/\*[\s\S]*?\*\//g, " ")
        .replace(/\s+/g, " ")
        .trim();
}

function createLineLookup(source) {
    const starts = [0];
    for (let index = 0; index < source.length; index += 1) {
        if (source.charCodeAt(index) === 10) {
            starts.push(index + 1);
        }
    }
    return (offset) => {
        let low = 0;
        let high = starts.length;
        while (low < high) {
            const middle = Math.floor((low + high) / 2);
            if (starts[middle] <= offset) {
                low = middle + 1;
            } else {
                high = middle;
            }
        }
        return low;
    };
}

function findRuleTerminator(source, start, end) {
    let quote = null;
    let inComment = false;
    let parentheses = 0;
    let brackets = 0;
    for (let index = start; index < end; index += 1) {
        const character = source[index];
        const next = source[index + 1];
        if (inComment) {
            if (character === "*" && next === "/") {
                inComment = false;
                index += 1;
            }
            continue;
        }
        if (quote) {
            if (character === "\\") {
                index += 1;
            } else if (character === quote) {
                quote = null;
            }
            continue;
        }
        if (character === "/" && next === "*") {
            inComment = true;
            index += 1;
        } else if (character === "'" || character === '"') {
            quote = character;
        } else if (character === "(") {
            parentheses += 1;
        } else if (character === ")") {
            parentheses = Math.max(0, parentheses - 1);
        } else if (character === "[") {
            brackets += 1;
        } else if (character === "]") {
            brackets = Math.max(0, brackets - 1);
        } else if (parentheses === 0 && brackets === 0 && (character === "{" || character === ";")) {
            return { index, character };
        }
    }
    return null;
}

function findClosingBrace(source, openingBrace, end) {
    let depth = 1;
    let quote = null;
    let inComment = false;
    for (let index = openingBrace + 1; index < end; index += 1) {
        const character = source[index];
        const next = source[index + 1];
        if (inComment) {
            if (character === "*" && next === "/") {
                inComment = false;
                index += 1;
            }
            continue;
        }
        if (quote) {
            if (character === "\\") {
                index += 1;
            } else if (character === quote) {
                quote = null;
            }
            continue;
        }
        if (character === "/" && next === "*") {
            inComment = true;
            index += 1;
        } else if (character === "'" || character === '"') {
            quote = character;
        } else if (character === "{") {
            depth += 1;
        } else if (character === "}") {
            depth -= 1;
            if (depth === 0) {
                return index;
            }
        }
    }
    throw new Error(`Unclosed CSS block at offset ${openingBrace}`);
}

function splitDeclarations(body) {
    const declarations = [];
    let start = 0;
    let quote = null;
    let inComment = false;
    let parentheses = 0;

    function appendDeclaration(end) {
        const chunk = normalizeWhitespace(body.slice(start, end));
        start = end + 1;
        if (!chunk) {
            return;
        }
        let propertyEnd = -1;
        let localQuote = null;
        let localComment = false;
        let localParentheses = 0;
        for (let index = 0; index < chunk.length; index += 1) {
            const character = chunk[index];
            const next = chunk[index + 1];
            if (localComment) {
                if (character === "*" && next === "/") {
                    localComment = false;
                    index += 1;
                }
                continue;
            }
            if (localQuote) {
                if (character === "\\") {
                    index += 1;
                } else if (character === localQuote) {
                    localQuote = null;
                }
                continue;
            }
            if (character === "/" && next === "*") {
                localComment = true;
                index += 1;
            } else if (character === "'" || character === '"') {
                localQuote = character;
            } else if (character === "(") {
                localParentheses += 1;
            } else if (character === ")") {
                localParentheses = Math.max(0, localParentheses - 1);
            } else if (character === ":" && localParentheses === 0) {
                propertyEnd = index;
                break;
            }
        }
        if (propertyEnd <= 0) {
            return;
        }
        declarations.push({
            property: chunk.slice(0, propertyEnd).trim().toLowerCase(),
            value: normalizeWhitespace(chunk.slice(propertyEnd + 1)),
        });
    }

    for (let index = 0; index <= body.length; index += 1) {
        const character = body[index];
        const next = body[index + 1];
        if (inComment) {
            if (character === "*" && next === "/") {
                inComment = false;
                index += 1;
            }
            continue;
        }
        if (quote) {
            if (character === "\\") {
                index += 1;
            } else if (character === quote) {
                quote = null;
            }
            continue;
        }
        if (character === "/" && next === "*") {
            inComment = true;
            index += 1;
        } else if (character === "'" || character === '"') {
            quote = character;
        } else if (character === "(") {
            parentheses += 1;
        } else if (character === ")") {
            parentheses = Math.max(0, parentheses - 1);
        } else if ((character === ";" && parentheses === 0) || index === body.length) {
            appendDeclaration(index);
        }
    }
    return declarations;
}

function parseCssSource(name, source) {
    const rules = [];
    const lineAt = createLineLookup(source);

    function parseRegion(start, end, context) {
        let cursor = start;
        while (cursor < end) {
            while (cursor < end && /\s/.test(source[cursor])) {
                cursor += 1;
            }
            if (cursor >= end) {
                break;
            }
            if (source[cursor] === "/" && source[cursor + 1] === "*") {
                const commentEnd = source.indexOf("*/", cursor + 2);
                cursor = commentEnd < 0 ? end : commentEnd + 2;
                continue;
            }
            const terminator = findRuleTerminator(source, cursor, end);
            if (!terminator) {
                break;
            }
            const prelude = normalizeWhitespace(source.slice(cursor, terminator.index));
            if (!prelude) {
                cursor = terminator.index + 1;
                continue;
            }
            if (terminator.character === ";") {
                cursor = terminator.index + 1;
                continue;
            }
            const closingBrace = findClosingBrace(source, terminator.index, end);
            if (NESTED_AT_RULE.test(prelude)) {
                parseRegion(terminator.index + 1, closingBrace, context.concat(prelude));
            } else if (!NON_STYLE_AT_RULE.test(prelude) && !prelude.startsWith("@")) {
                const body = source.slice(terminator.index + 1, closingBrace);
                rules.push({
                    file: name,
                    line: lineAt(cursor),
                    selector: prelude,
                    context: context.join(" || "),
                    normalizedBody: normalizeWhitespace(body),
                    declarations: splitDeclarations(body),
                });
            }
            cursor = closingBrace + 1;
        }
    }

    parseRegion(0, source.length, []);
    return rules;
}

function auditCssSources(sources) {
    const rules = sources.flatMap(({ name, source }) => parseCssSource(name, source));
    const selectorGroups = new Map();
    const exactRuleGroups = new Map();

    for (const rule of rules) {
        const selectorKey = `${rule.context}\n${rule.selector}`;
        const exactKey = `${selectorKey}\n${rule.normalizedBody}`;
        if (!selectorGroups.has(selectorKey)) {
            selectorGroups.set(selectorKey, []);
        }
        selectorGroups.get(selectorKey).push(rule);
        if (!exactRuleGroups.has(exactKey)) {
            exactRuleGroups.set(exactKey, []);
        }
        exactRuleGroups.get(exactKey).push(rule);
    }

    const repeatedSelectors = [];
    const exactRedundantDeclarations = [];
    const conflictingProperties = [];
    let additiveRepeatedSelectors = 0;

    for (const group of selectorGroups.values()) {
        if (group.length < 2) {
            continue;
        }
        const propertyGroups = new Map();
        for (const rule of group) {
            for (const declaration of rule.declarations) {
                if (!propertyGroups.has(declaration.property)) {
                    propertyGroups.set(declaration.property, []);
                }
                propertyGroups.get(declaration.property).push({
                    value: declaration.value,
                    location: `${rule.file}:${rule.line}`,
                });
            }
        }

        let hasOverlappingProperty = false;
        for (const [property, occurrences] of propertyGroups.entries()) {
            if (occurrences.length < 2) {
                continue;
            }
            hasOverlappingProperty = true;
            const byValue = new Map();
            for (const occurrence of occurrences) {
                if (!byValue.has(occurrence.value)) {
                    byValue.set(occurrence.value, []);
                }
                byValue.get(occurrence.value).push(occurrence);
            }
            for (const [value, identicalOccurrences] of byValue.entries()) {
                if (identicalOccurrences.length < 2) {
                    continue;
                }
                const keeper = identicalOccurrences.at(-1);
                for (const redundant of identicalOccurrences.slice(0, -1)) {
                    exactRedundantDeclarations.push({
                        selector: group[0].selector,
                        context: group[0].context || "(root)",
                        property,
                        value,
                        source: redundant.location,
                        shadowedBy: keeper.location,
                    });
                }
            }
            if (byValue.size > 1) {
                conflictingProperties.push({
                    selector: group[0].selector,
                    context: group[0].context || "(root)",
                    property,
                    occurrences,
                });
            }
        }
        if (!hasOverlappingProperty) {
            additiveRepeatedSelectors += 1;
        }
        repeatedSelectors.push({
            selector: group[0].selector,
            context: group[0].context || "(root)",
            locations: group.map((rule) => `${rule.file}:${rule.line}`),
        });
    }

    const duplicateRuleGroups = [...exactRuleGroups.values()]
        .filter((group) => group.length > 1)
        .map((group) => ({
            selector: group[0].selector,
            context: group[0].context || "(root)",
            locations: group.map((rule) => `${rule.file}:${rule.line}`),
        }));

    return {
        files: sources.map(({ name }) => name),
        totalRules: rules.length,
        uniqueContextSelectors: selectorGroups.size,
        repeatedContextSelectors: repeatedSelectors.length,
        additiveRepeatedSelectors,
        exactDuplicateRuleGroups: duplicateRuleGroups.length,
        exactRedundantDeclarationCount: exactRedundantDeclarations.length,
        conflictingPropertyCount: conflictingProperties.length,
        selectorsWithConflicts: new Set(conflictingProperties.map((item) => `${item.context}\n${item.selector}`)).size,
        duplicateRuleGroups,
        exactRedundantDeclarations,
        conflictingProperties,
        repeatedSelectors,
    };
}

function auditDefaultDispatcherCss() {
    const cssDirectory = path.resolve(__dirname, "..", "static", "css");
    return auditCssSources(DEFAULT_CSS_FILES.map((name) => ({
        name,
        source: fs.readFileSync(path.join(cssDirectory, name), "utf8"),
    })));
}

if (require.main === module) {
    process.stdout.write(`${JSON.stringify(auditDefaultDispatcherCss(), null, 2)}\n`);
}

module.exports = {
    DEFAULT_CSS_FILES,
    auditCssSources,
    auditDefaultDispatcherCss,
    normalizeWhitespace,
    parseCssSource,
    splitDeclarations,
};
