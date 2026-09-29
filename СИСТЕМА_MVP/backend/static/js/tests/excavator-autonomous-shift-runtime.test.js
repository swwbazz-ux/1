const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const template = fs.readFileSync(
    path.resolve(__dirname, '../../../templates/trips/excavator_work.html'),
    'utf8',
);

function extractBraceBlock(source, signature) {
    const start = source.indexOf(signature);
    assert.notEqual(start, -1, `missing ${signature}`);
    const brace = source.indexOf('{', start);
    let depth = 0;
    let quote = '';
    let escaped = false;
    for (let index = brace; index < source.length; index += 1) {
        const char = source[index];
        if (quote) {
            if (escaped) escaped = false;
            else if (char === '\\') escaped = true;
            else if (char === quote) quote = '';
            continue;
        }
        if (char === '"' || char === "'" || char === '`') { quote = char; continue; }
        if (char === '{') depth += 1;
        if (char === '}') {
            depth -= 1;
            if (depth === 0) return source.slice(start, index + 1);
        }
    }
    throw new Error(`unterminated ${signature}`);
}

function element(dataset = {}) {
    const classes = new Set();
    return {
        dataset: {...dataset},
        classList: {
            toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); },
            remove(...names) { names.forEach(name => classes.delete(name)); },
            contains(name) { return classes.has(name); },
        },
        attributes: {},
        disabled: true,
        draggable: false,
        textContent: '',
        value: '',
        setAttribute(name, value) { this.attributes[name] = String(value); },
    };
}

function shellFixture() {
    const one = {
        '[data-eo-shift-button]': element(),
        '[data-eo-shift-label]': element(),
        '[data-eo-screen="shift"]': element(),
        '[data-eo-screen="trucks"]': element(),
        '[data-eo-screen="events"]': element(),
        '[data-eo-face-settings-available]': element(),
        '[data-eo-apply-settings]': element(),
        '[data-eo-local-shift-fact-value]': null,
        '[data-eo-local-shift-fact-meta]': null,
        '[data-eo-face-horizon]': element(),
        '[data-eo-face-block]': element(),
        '[data-eo-rock-select]': element(),
    };
    const many = {
        '[data-eo-face-horizon], [data-eo-face-block], [data-eo-rock-select], [data-eo-dump-select]': [
            one['[data-eo-face-horizon]'], one['[data-eo-face-block]'], one['[data-eo-rock-select]'],
        ],
        '[data-eo-truck-card]': [element({eoPreparedCanLoad: '1', eoPreparedManualAvailable: '1'})],
        '[data-eo-dump-target], [data-eo-downtime-reason-id]': [element()],
        '[data-eo-free-bucket-open]': [element()],
        '[data-eo-dump-select]': [element({eoDumpSelect: '4'}), element({eoDumpSelect: '5'})],
    };
    many['[data-eo-face-horizon], [data-eo-face-block], [data-eo-rock-select], [data-eo-dump-select]']
        .push(...many['[data-eo-dump-select]']);
    return {
        dataset: {eoCurrentExcavatorId: '7', eoServerRenderedShiftId: '0'},
        one,
        many,
        querySelector(selector) { return one[selector] || null; },
        querySelectorAll(selector) { return many[selector] || []; },
    };
}

const runtime = {console, Promise, JSON, Number, Boolean};
vm.createContext(runtime);
vm.runInContext(extractBraceBlock(template, 'function projectExcavatorLocalShift(shell, ledger)'), runtime);

test('OFF-C1-R1 C7/C8: local opening enables real controls and restores durable face context', async () => {
    const shell = shellFixture();
    const shift = {local_shift_id: 'local-shift', equipment_id: 7, server_shift_id: null, status: 'open'};
    const ledger = {
        currentShift: () => shift,
        workContext: async () => ({
            rock_type_id: 9,
            dump_point_ids: [5],
            loading_horizon: '777',
            loading_block: '8',
        }),
    };

    runtime.projectExcavatorLocalShift(shell, ledger);
    await Promise.resolve();

    assert.equal(shell.many['[data-eo-free-bucket-open]'][0].disabled, false);
    assert.equal(shell.one['[data-eo-apply-settings]'].disabled, false);
    assert.equal(shell.one['[data-eo-face-horizon]'].value, '777');
    assert.equal(shell.one['[data-eo-face-block]'].value, '8');
    assert.equal(shell.one['[data-eo-rock-select]'].value, '9');
    assert.equal(shell.many['[data-eo-dump-select]'][0].attributes['aria-pressed'], 'false');
    assert.equal(shell.many['[data-eo-dump-select]'][1].attributes['aria-pressed'], 'true');
});

test('OFF-C1-R1 C9: a locally closed shift projects the button back to a new opening without deleting history', () => {
    const shell = shellFixture();
    const closed = {local_shift_id: 'closed-local-shift', equipment_id: 7, status: 'closed'};
    const ledger = {currentShift: () => closed};

    runtime.projectExcavatorLocalShift(shell, ledger);

    assert.equal(shell.one['[data-eo-shift-button]'].dataset.eoShiftAction, 'open');
    assert.equal(shell.one['[data-eo-shift-button]'].disabled, false);
    assert.equal(shell.one['[data-eo-shift-label]'].textContent, 'Начать смену');
    assert.equal(shell.many['[data-eo-free-bucket-open]'][0].disabled, true);
    assert.equal(closed.local_shift_id, 'closed-local-shift');
});

test('OFF-C1-R1 C8: apply uses the durable field queue rather than the lossy settings fetch', () => {
    assert.match(template, /queueExcavatorFieldEvent\("excavator\.work_context\.changed"/);
    assert.doesNotMatch(template, /fetch\(settingsUrl,/);
    assert.match(template, /localShiftLedger\.nextSequence\(\)/);
    assert.match(template, /if \(!localShiftLedger\) return Promise\.reject/);
});
