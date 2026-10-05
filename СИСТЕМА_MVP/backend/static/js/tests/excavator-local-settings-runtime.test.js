const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const createLedger = require('../excavator-local-shift-v1.js');
const createController = require('../excavator-autonomous-shift-v1.js');
const template = fs.readFileSync(require.resolve('../../../templates/trips/excavator_work.html'), 'utf8');
const identity = {actor_id: 12, access_id: 7, role_code: 'excavator_operator', device_id: 'phone'};
const face = {rock_type_id: '8', dump_point_ids: ['11'], loading_horizon: '075', loading_block: '52'};
const copy = value => JSON.parse(JSON.stringify(value));
function disk() {
    let state;
    return {fail: false, read: async () => state && copy(state), async write(next) {
        if (this.fail) throw new Error('quota');
        state = copy(next);
    }};
}
async function controller(adapter = disk(), open = true) {
    const transport = {queue: () => new Promise(() => {})};
    const ledger = createLedger({adapter, accessId: 7, actorId: 12, deviceId: 'phone', outbox: transport,
        locks: {request: (name, options, callback) => Promise.resolve().then(() => callback({name}))}});
    const api = createController({ledger, transport, identity});
    await api.ready();
    if (open && !ledger.currentShift()) await api.open({excavator_id: 7, fuel: '100', engine_hours: '1200'}, 'open-1');
    return api;
}
function node(dataset = {}, classes = []) {
    const values = new Set(classes);
    const listeners = {};
    return {dataset, value: '', hidden: false, disabled: false, textContent: '',
        classList: {add: (...names) => names.forEach(name => values.add(name)),
            remove: (...names) => names.forEach(name => values.delete(name)), contains: name => values.has(name),
            toggle(name, on) { on ??= !values.has(name); if (on) values.add(name); else values.delete(name); },
            [Symbol.iterator]: () => values.values()},
        setAttribute(name, value) { this[name] = value; }, getAttribute(name) { return this[name]; },
        addEventListener(name, fn) { listeners[name] = fn; },
        fire(name) { return listeners[name]({preventDefault() {}}); }, querySelector: () => null};
}
function screen(api) {
    const rock = node(), horizon = node(), block = node();
    rock.value = '1'; horizon.value = '1'; block.value = '1';
    const choices = [node({eoDumpSelect: '10', eoDumpPersisted: 'true'}, ['is-selected']), node({eoDumpSelect: '11'})];
    const targets = [node({eoDumpTarget: '10'}), node({eoDumpTarget: '11'})];
    targets[1].hidden = true; targets[1].disabled = true;
    const apply = node({eoSettingsAvailable: 'true', eoSettingsApplied: 'false'});
    const grid = node({}, ['is-count-1', 'is-single']);
    const faceScreen = node(), empty = node(), faceContent = node({eoFaceSettingsAvailable: 'true'});
    const selectors = {'[data-eo-rock-select]': rock, '[data-eo-face-horizon]': horizon, '[data-eo-face-block]': block,
        '[data-eo-apply-settings]': apply, '.eo-dashboard-unload-grid': grid, '[data-eo-empty-dumps]': empty};
    const shell = {dataset: {}, querySelector: key => selectors[key] || null, querySelectorAll: key => {
        if (key === '[data-eo-dump-select]') return choices;
        if (key === '[data-eo-dump-target]') return targets;
        if (key === '[data-eo-face-horizon], [data-eo-face-block]') return [horizon, block];
        if (key === '[data-eo-face-horizon], [data-eo-face-block], [data-eo-rock-select]') return [horizon, block, rock];
        return [];
    }};
    const notices = [], voices = [];
    const context = vm.createContext({shell, autonomousShift: api, faceScreen, faceContent,
        window: {eoExcavatorAutonomousShift: api}, document: {querySelector: () => shell},
        dumpInput: node(), dumpPointsInput: node(), settingsUrl: '/settings/', csrfToken: 'test',
        playExcavatorSound() {}, playExcavatorVoice: (...args) => voices.push(args),
        showExcavatorNotice: text => notices.push(text), invalidateExcavatorWorkRefresh() {},
        activateTab: tab => { shell.dataset.eoActiveTab = tab; },
        generateClientActionId: () => 'client-settings', newExcavatorFieldId: () => 'saved-settings',
        fetchCalls: 0, refreshCalls: 0});
    context.fetch = async () => { context.fetchCalls += 1; return {ok: true, json: async () => ({ok: true, dump_point_ids: ['11']})}; };
    context.refreshExcavatorWorkFromServer = async () => { context.refreshCalls += 1; return true; };
    vm.runInContext(template.slice(template.indexOf('    var dumpChoiceButtons ='), template.indexOf('    function snapshotTruckCard(')), context);
    vm.runInContext(template.slice(template.indexOf('    var applySettings ='), template.indexOf('    function parseShiftNumber(')), context);
    return {context, shell, rock, horizon, block, choices, targets, apply, grid, notices, voices,
        restore: () => shell.__eoRestoreLocalWorkContext(),
        draft() {
            rock.value = '8'; horizon.value = '075'; block.value = '52';
            context.toggleDumpChoice(choices[1]); context.toggleDumpChoice(choices[0]);
        }};
}
async function settle() { for (let i = 0; i < 12; i += 1) await new Promise(setImmediate); }

test('actual Apply handler saves locally and reveals the prepared destination without waiting for HTTP', async () => {
    const api = await controller();
    const ui = screen(api);
    ui.draft();
    ui.apply.fire('click');
    await settle();
    assert.equal(api.workContext().event_id, 'saved-settings');
    assert.equal(ui.context.fetchCalls, 0);
    assert.equal(ui.context.refreshCalls, 0);
    assert.equal(ui.targets[0].hidden, true);
    assert.equal(ui.targets[1].hidden, false);
    assert.equal(ui.targets[1].disabled, false);
    assert.equal(ui.apply.disabled, true);
    assert.equal(ui.shell.dataset.eoActiveTab, 'trucks');
    assert.equal(ui.notices.at(-1), 'Настройки сохранены на телефоне');
});

test('restart restores fields and destination cards; receipts and newer settings preserve an unsaved draft', async () => {
    const adapter = disk();
    const first = await controller(adapter);
    const source = await first.saveWorkContext(face, 'settings-1');
    const api = await controller(adapter);
    const ui = screen(api);
    ui.restore();
    assert.deepEqual([ui.rock.value, ui.horizon.value, ui.block.value], ['8', '075', '52']);
    assert.equal(ui.targets[1].hidden, false);
    ui.horizon.value = '999';
    ui.horizon.fire('input');
    await api.confirm(source, {server_ids: {shift_id: 99}});
    ui.restore();
    assert.equal(ui.horizon.value, '999');
    await api.saveWorkContext({...face, dump_point_ids: ['10'], loading_horizon: '80'}, 'settings-2');
    ui.restore();
    assert.equal(ui.horizon.value, '999');
    assert.equal(ui.targets[0].hidden, false);
    assert.equal(ui.targets[1].hidden, true);
    assert.equal(ui.context.faceScreen.dataset.eoFaceDirty, 'true');
});

test('quota failure leaves Apply retryable, draft visible and destination selection unapplied', async () => {
    const adapter = disk();
    const api = await controller(adapter);
    const ui = screen(api);
    ui.draft(); adapter.fail = true;
    ui.apply.fire('click');
    await settle();
    assert.equal(api.workContext(), null);
    assert.equal(ui.targets[1].hidden, true);
    assert.equal(ui.apply.disabled, false);
    assert.equal(ui.context.faceSettingsPending, false);
    assert.equal(ui.horizon.value, '075');
    assert.equal(ui.voices.some(args => args[0] === 'action_ok'), false);
    assert.match(ui.notices.at(-1), /quota/);
    adapter.fail = false;
    ui.apply.fire('click');
    await settle();
    assert.equal(api.workContext().event_id, 'saved-settings');
});

test('the existing server-shift Apply path still performs its original POST and refresh', async () => {
    const api = await controller(disk(), false);
    const ui = screen(api);
    ui.draft(); ui.apply.fire('click');
    await settle();
    assert.equal(ui.context.fetchCalls, 1);
    assert.equal(ui.context.refreshCalls, 1);
    assert.equal(api.workContext(), null);
});

test('an early Apply waits for journal discovery before choosing local versus server storage', async () => {
    const api = await controller();
    let release;
    let loaded = false;
    const startup = new Promise(resolve => { release = () => { loaded = true; resolve(); }; });
    const delayed = {...api, ready: () => startup, currentShift: () => loaded ? api.currentShift() : null};
    const ui = screen(delayed);
    ui.draft(); ui.apply.fire('click');
    await settle();
    assert.equal(ui.context.fetchCalls, 0);
    assert.equal(api.workContext(), null);
    assert.equal(ui.context.faceSettingsPending, true);
    release();
    await settle();
    assert.equal(api.workContext().event_id, 'saved-settings');
    assert.equal(ui.context.fetchCalls, 0);
});

test('unreadable journal cannot silently switch Apply to the server path', async () => {
    const api = await controller();
    const ui = screen({...api, ready: async () => { throw new Error('storage unavailable'); }, currentShift: () => null});
    ui.draft(); ui.apply.fire('click');
    await settle();
    assert.equal(ui.context.fetchCalls, 0);
    assert.equal(api.workContext(), null);
    assert.equal(ui.context.faceSettingsPending, false);
    assert.equal(ui.apply.disabled, false);
    assert.match(ui.notices.at(-1), /storage unavailable/);
});

test('detached and read-only screens cannot project or apply local settings', async () => {
    const api = await controller();
    await api.saveWorkContext(face, 'settings-1');
    const ui = screen(api);
    ui.context.document.querySelector = () => null;
    ui.restore();
    assert.equal(ui.rock.value, '1');
    ui.context.document.querySelector = () => ui.shell;
    ui.context.window.isAppRoleReadonly = () => true;
    ui.restore(); ui.draft(); ui.apply.fire('click');
    await settle();
    assert.equal(api.workContext().event_id, 'settings-1');
    assert.equal(ui.context.fetchCalls, 0);
});
