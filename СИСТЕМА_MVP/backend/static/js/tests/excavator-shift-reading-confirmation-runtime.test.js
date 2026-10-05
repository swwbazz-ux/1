"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const createLedger = require('../excavator-local-shift-v1.js');
const createController = require('../excavator-autonomous-shift-v1.js');

const backendRoot = path.resolve(__dirname, "..", "..", "..");
const templateSource = fs.readFileSync(
    path.join(backendRoot, "templates", "trips", "excavator_work.html"),
    "utf8"
).replace(/\r\n?/g, "\n");
const shiftCss = fs.readFileSync(
    path.join(backendRoot, "static", "css", "excavator-work-v55-shift.css"),
    "utf8"
).replace(/\r\n?/g, "\n");
const shiftScreenSource = fs.readFileSync(
    path.join(backendRoot, "templates", "includes", "mobile_shift_screen.html"),
    "utf8"
).replace(/\r\n?/g, "\n");

function extractBraceBlock(source, signature, label) {
    const start = source.indexOf(signature);
    assert.notEqual(start, -1, `Missing ${label}`);
    const braceStart = source.indexOf("{", start);
    let depth = 0;
    let quote = "";
    let escaped = false;
    for (let index = braceStart; index < source.length; index += 1) {
        const character = source[index];
        if (quote) {
            if (escaped) escaped = false;
            else if (character === "\\") escaped = true;
            else if (character === quote) quote = "";
            continue;
        }
        if (character === '"' || character === "'" || character === "`") {
            quote = character;
            continue;
        }
        if (character === "{") depth += 1;
        if (character === "}") {
            depth -= 1;
            if (depth === 0) return source.slice(start, index + 1);
        }
    }
    throw new Error(`Unclosed ${label}`);
}

function readingValidator({fuel, hours, startHours, closing}) {
    const context = {
        Number,
        Object,
        shiftFuelInput: {value: String(fuel), dataset: {eoFuelCapacity: "7000"}},
        shiftHoursInput: {
            value: String(hours),
            dataset: {eoStartEngineHours: String(startHours)},
        },
        isShiftCloseAction() { return closing; },
        showShiftErrors() {},
        clearShiftErrors() {},
    };
    const sources = [
        extractBraceBlock(templateSource, "function parseShiftNumber", "number parser"),
        extractBraceBlock(templateSource, "function calculatedShiftFuelLiters", "fuel calculator"),
        extractBraceBlock(templateSource, "function validateShiftReadings", "reading validator"),
    ];
    vm.runInNewContext(`${sources.join("\n")}; this.validate = validateShiftReadings;`, context);
    return context.validate(false);
}

test("closing accepts anomalous whole readings for explicit review before storage", () => {
    assert.equal(readingValidator({fuel: 80, hours: 1199, startHours: 1200, closing: true}).valid, true);
    assert.equal(readingValidator({fuel: 80, hours: 1213, startHours: 1200, closing: true}).valid, true);
    assert.equal(readingValidator({fuel: 120, hours: 1201, startHours: 1200, closing: true}).valid, true);
});

function element(value = '', dataset = {}) {
    const classes = new Set(), listeners = {};
    return {value, dataset, hidden: true, disabled: false, textContent: '', focus() {},
        classList: {add: (...keys) => keys.forEach(k => classes.add(k)), remove: (...keys) => keys.forEach(k => classes.delete(k)), contains: k => classes.has(k)},
        addEventListener: (key, fn) => { listeners[key] = fn; },
        fire: key => listeners[key](), setAttribute() {}};
}
async function closeRuntime(adapter, restored = false) {
    adapter ||= {state: null, fail: false, async read() {return this.state;}, async write(state) {
        if (this.fail) throw new Error('quota');
        this.state = JSON.parse(JSON.stringify(state));
    }};
    const identity = {actor_id: 12, access_id: 7, role_code: 'excavator_operator', device_id: 'phone'};
    const transport = {queue: () => new Promise(() => {})};
    const ledger = createLedger({adapter, accessId: 7, actorId: 12, deviceId: 'phone', outbox: transport,
        locks: {request: (name, options, callback) => Promise.resolve().then(() => callback({name}))}});
    const api = createController({ledger, transport, identity});
    await api.ready();
    if (!restored) await api.open({excavator_id: 7, fuel: '100', engine_hours: '1200'}, 'open-1');
    const fuel = element('120', {eoFuelCapacity: '7000'}), hours = element('1213', {eoStartEngineHours: '900'});
    const button = element('', {eoShiftAction: 'close'}), modal = element(), back = element(), accept = element();
    const shell = {dataset: {eoLocalShiftId: 'open-1', nativeShiftId: '', eoCurrentExcavatorId: '7'}, querySelector: selector => ({
        '[data-eo-shift-fuel]': fuel, '[data-eo-shift-engine-hours]': hours
    }[selector] || null)};
    const errors = [], voices = [], notices = [];
    let eventNumber = 0;
    const context = vm.createContext({shell, autonomousShift: api, shiftButton: button, shiftUrl: '/shift/', shiftServerBlocked: false,
        shiftFuelInput: fuel, shiftHoursInput: hours, shiftLabel: element(), shiftScreen: element(),
        shiftConfirmationModal: modal, shiftConfirmationBack: back, shiftConfirmationAccept: accept,
        shiftConfirmation: null, shiftConfirmationRequesting: false, shiftOtherRoleConfirmed: false,
        shiftPendingActionKey: '', shiftPendingActionId: '',
        document: {body: element()}, window: {reviewExcavatorShiftClose: createController.reviewClose, requestAnimationFrame: fn => fn()},
        isShiftCloseAction: () => true, clearShiftErrors() {}, showShiftErrors: text => errors.push(text),
        updateShiftActionState() {}, resetShiftHold() {}, renderShiftConfirmationWarnings() {},
        generateClientActionId: () => 'close-action', playExcavatorVoice: (...args) => voices.push(args),
        showExcavatorNotice: text => notices.push(text), applyLocalShiftClose: () => {shell.dataset.closed = 'true';},
        projectExcavatorLocalShift() {},
        queueExcavatorFieldEvent: (type, payload) => api.outbox.queue({...identity, event_id: 'close-' + (++eventNumber),
            event_type: type, equipment_id: 7, shift_id: 0, sequence: 1, occurred_at: new Date().toISOString(), depends_on: [], payload}),
        fetch: () => {throw new Error('HTTP must not be used');}});
    for (const name of ['parseShiftNumber', 'calculatedShiftFuelLiters', 'validateShiftReadings', 'hideShiftConfirmation',
        'showShiftConfirmation', 'resetShiftSubmissionUi', 'submitExcavatorShiftAction']) {
        vm.runInContext(extractBraceBlock(templateSource, 'function ' + name + '(', name), context);
    }
    const bindStart = templateSource.indexOf('    if (shiftConfirmationBack) {');
    const bindEnd = templateSource.indexOf('    if (shiftButton) {', bindStart);
    vm.runInContext(templateSource.slice(bindStart, bindEnd), context);
    return {api, ledger, adapter, context, fuel, hours, button, modal, back, accept, shell, errors, voices, notices,
        submit: () => context.submitExcavatorShiftAction()};
}
async function settleClose() { for (let i = 0; i < 15; i += 1) await new Promise(setImmediate); }

test('local warning rules match missing, decreasing and high hours and the exact 12-hour boundary', () => {
    const basis = {shift_ref: 'server:99', equipment_id: 7, start_engine_hours: '1200', fuel_capacity_l: '7000'};
    const review = (hours, extra = {}) => createController.reviewClose({fuel: '7000', fuel_percent: '100', engine_hours: hours}, {...basis, ...extra});
    assert.deepEqual(review('1212').warnings, []);
    assert.equal(review('1199').warnings[0].code, 'engine_hours_decreased');
    assert.equal(review('1213').warnings[0].code, 'engine_hours_delta_high');
    assert.equal(review('1212', {start_engine_hours: null}).warnings[0].code, 'engine_hours_start_missing');
    for (const bad of ['NaN', '-1', '1200.5', '100000000']) assert.throws(() => review(bad), /проверьте показание/);
});

test('normal local close skips confirmation and saves without HTTP', async () => {
    const ui = await closeRuntime();
    ui.fuel.value = '100'; ui.hours.value = '1212';
    assert.equal(await ui.submit(), true);
    assert.equal(ui.ledger.currentShift().status, 'closed');
    assert.equal(ui.modal.hidden, true);
    assert.equal(ui.voices.length, 1);
});

test('startup restoration cannot turn an early Close tap into opening a different shift', async () => {
    const ui = await closeRuntime();
    ui.api.ready = async () => { ui.button.dataset.eoShiftAction = 'open'; };
    assert.equal(await ui.submit(), false);
    assert.equal((await ui.ledger.events()).length, 1);
    assert.equal(ui.voices.length, 0);
    assert.match(ui.errors.at(-1), /Состояние смены изменилось/);
});

test('actual close handler asks first, then durably retains confirmed readings across restart and next opening', async () => {
    const ui = await closeRuntime();
    assert.equal(await ui.submit(), false);
    assert.equal(ui.ledger.currentShift().status, 'open');
    assert.equal((await ui.ledger.events()).length, 1);
    assert.equal(ui.modal.hidden, false);
    assert.equal(ui.voices.length, 0);
    // The initial hours come from the local opening (1200), not stale HTML (900).
    assert.equal(ui.context.shiftConfirmation.readingConfirmation.start_engine_hours, '1200');
    ui.accept.fire('click'); ui.accept.fire('click');
    await settleClose();
    assert.equal(ui.ledger.currentShift().status, 'closed');
    const events = await ui.ledger.events();
    assert.equal(events.length, 2);
    assert.equal(events[1].payload.reading_confirmation.accepted, true);
    assert.equal(ui.voices.length, 1);
    const restarted = await closeRuntime(ui.adapter, true);
    assert.equal(restarted.ledger.currentShift().status, 'closed');
    assert.deepEqual(await restarted.ledger.getEvent(events[1].event_id), events[1]);
    const next = await restarted.api.open({excavator_id: 7, fuel: '100', engine_hours: '1213'}, 'open-2');
    assert.ok(next.depends_on.includes(events[1].event_id));
});

test('Back and changed readings cannot reuse the displayed confirmation', async () => {
    const ui = await closeRuntime();
    await ui.submit(); ui.back.fire('click');
    assert.equal(ui.context.shiftConfirmation, null);
    assert.equal((await ui.ledger.events()).length, 1);
    await ui.submit();
    ui.hours.value = '1214';
    ui.accept.fire('click'); await settleClose();
    assert.equal(ui.ledger.currentShift().status, 'open');
    assert.equal(ui.voices.length, 0);
    assert.match(ui.errors.at(-1), /Показания изменились/);
});

test('failed durable write never closes the screen and retry requires explicit confirmation again', async () => {
    const ui = await closeRuntime();
    await ui.submit(); ui.adapter.fail = true;
    ui.accept.fire('click'); await settleClose();
    assert.equal(ui.ledger.currentShift().status, 'open');
    assert.equal(ui.shell.dataset.closed, undefined);
    assert.equal(ui.button.disabled, false);
    assert.equal(ui.voices.length, 0);
    assert.match(ui.errors.at(-1), /quota/);
    ui.adapter.fail = false;
    assert.equal(await ui.submit(), false);
    ui.accept.fire('click'); await settleClose();
    assert.equal(ui.ledger.currentShift().status, 'closed');
    assert.equal((await ui.ledger.events()).length, 2);
});

test('the ledger rejects a confirmation copied from another local shift before saving', async () => {
    const ui = await closeRuntime();
    await ui.submit();
    const confirmation = ui.context.shiftConfirmation.readingConfirmation;
    confirmation.shift_ref = 'local:another-opening';
    ui.accept.fire('click'); await settleClose();
    assert.equal(ui.ledger.currentShift().status, 'open');
    assert.equal((await ui.ledger.events()).length, 1);
    assert.match(ui.errors.at(-1), /Подтвердите закрытие ещё раз/);
    const original = await ui.ledger.getEvent('open-1');
    await assert.rejects(ui.api.outbox.queue({...original,
        event_id: 'direct-invalid-close', event_type: 'excavator.shift.closed', sequence: original.sequence + 1,
        payload: {fuel: '8400', fuel_percent: '120', engine_hours: '1213', fuel_capacity_l: '7000', reading_confirmation: confirmation}
    }), /Подтвердите закрытие ещё раз/);
    assert.equal((await ui.ledger.events()).length, 1);
});

test("opening remains strict and malformed close values never reach confirmation", () => {
    assert.equal(readingValidator({fuel: 120, hours: 1201, startHours: 1200, closing: false}).valid, false);
    assert.equal(readingValidator({fuel: -1, hours: 1201, startHours: 1200, closing: true}).valid, false);
    assert.equal(readingValidator({fuel: 80.5, hours: 1201, startHours: 1200, closing: true}).valid, false);
    assert.equal(readingValidator({fuel: 80, hours: -1, startHours: 1200, closing: true}).valid, false);
    assert.equal(readingValidator({fuel: 80, hours: 1200.5, startHours: 1200, closing: true}).valid, false);
});

test("the native number input keeps the 100 percent ceiling only while opening", () => {
    assert.match(
        shiftScreenSource,
        /min="0" \{% if mobile_shift_state != "open" %\}max="100" \{% endif %\}name="shift_fuel"/
    );
});

test("confirmation rendering uses text nodes and keeps warnings scrollable", () => {
    assert.match(templateSource, /title\.textContent = String\(warning\.title/);
    assert.match(templateSource, /message\.textContent = String\(warning\.message/);
    assert.doesNotMatch(
        extractBraceBlock(templateSource, "function renderShiftConfirmationWarnings", "warning renderer"),
        /innerHTML/
    );
    assert.match(shiftCss, /\.eo-reading-confirmation__warnings[\s\S]*overflow-y: auto/);
    assert.match(shiftCss, /\.eo-reading-confirmation__actions button[\s\S]*min-height: 50px/);
});

test("warning and confirmed requests keep one shift snapshot and suppress premature success", () => {
    const submitSource = extractBraceBlock(
        templateSource,
        "function submitExcavatorShiftAction",
        "shift submitter"
    );
    assert.match(submitSource, /shift_id: shell\.dataset\.nativeShiftId/);
    assert.match(submitSource, /payload\.confirmation_token = String\(options\.confirmationToken\)/);
    assert.match(submitSource, /options\.expectedActionKey !== shiftActionKey/);
    assert.match(
        submitSource,
        /error\.confirmation_required === true[\s\S]*showShiftConfirmation\(error, shiftActionKey\)[\s\S]*return false/
    );
    assert.match(
        templateSource,
        /shiftConfirmationRequesting = true;[\s\S]*shiftConfirmationAccept\.disabled = true;[\s\S]*confirmationToken: confirmation\.token/
    );
    assert.match(
        templateSource,
        /\.eo-reading-confirmation:not\(\[hidden\]\)/
    );
});

test("editing readings invalidates a previously issued confirmation", () => {
    assert.match(
        templateSource,
        /\[shiftFuelInput, shiftHoursInput\]\.forEach[\s\S]*input\.addEventListener\("input"[\s\S]*hideShiftConfirmation\(\)/
    );
    assert.match(templateSource, /data-eo-reading-confirmation-back/);
    assert.match(templateSource, /data-eo-reading-confirmation-accept/);
});
