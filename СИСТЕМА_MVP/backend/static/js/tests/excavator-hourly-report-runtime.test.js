const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(
    path.resolve(__dirname, '..', 'excavator-hourly-report-v1.js'),
    'utf8',
);

class FakeClassList {
    add() {}
    remove() {}
    toggle() {}
}

class FakeElement {
    constructor(tag = 'div') {
        this.tagName = tag.toUpperCase();
        this.children = [];
        this.dataset = {};
        this.hidden = false;
        this.classList = new FakeClassList();
        this.attributes = new Map();
        this.textContent = '';
        this.firstChild = null;
    }
    appendChild(child) {
        this.children.push(child);
        this.firstChild = this.children[0] || null;
        return child;
    }
    removeChild(child) {
        this.children = this.children.filter(item => item !== child);
        this.firstChild = this.children[0] || null;
        return child;
    }
    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    removeAttribute(name) { this.attributes.delete(name); }
    focus() {}
}

test('OFF-C1-R1 C5: a hung hourly GET cannot hide the durable local report', async () => {
    const listeners = {};
    const windowListeners = {};
    const modal = new FakeElement();
    modal.hidden = true;
    modal.dataset.eoHourlyReportUrl = '/excavator/hourly-report/';
    const content = new FakeElement();
    const subtitle = new FakeElement();
    const close = new FakeElement('button');
    const back = new FakeElement('button');
    modal.querySelector = selector => ({
        '[data-eo-hourly-report-content]': content,
        '[data-eo-hourly-report-subtitle]': subtitle,
        '[data-eo-hourly-report-close]': close,
        '.eo-hourly-report__return': back,
    })[selector] || null;

    const shell = new FakeElement('main');
    shell.dataset.eoCurrentExcavatorId = '12';
    const opener = new FakeElement('button');
    const document = {
        readyState: 'complete',
        body: new FakeElement('body'),
        activeElement: null,
        createElement: tag => new FakeElement(tag),
        querySelector: selector => {
            if (selector === '[data-eo-hourly-report-modal]') return modal;
            if (selector === '[data-eo-shell]') return shell;
            if (selector === '[data-eo-hourly-report-open]') return opener;
            return null;
        },
        addEventListener: (name, callback) => { listeners[name] = callback; },
    };
    const localPayload = {
        ok: true,
        schema_version: 2,
        excavator: {name: 'Экскаватор 12'},
        work_date: '2026-09-30',
        generated_at: '2026-09-30T00:15:00.000Z',
        freshness_label: 'Местный журнал',
        hours: [{
            code: 'current',
            title: 'Текущий час',
            period: {
                label: '10:00–11:00',
                start: '2026-09-30T00:00:00.000Z',
                end: '2026-09-30T01:00:00.000Z',
            },
            rows: [],
            totals: {trip_count: 1, belaz: 1, nhl: 0, volume_m3: 22},
            is_empty: false,
            unclassified_trip_count: 0,
        }],
    };
    let fetchSettled = false;
    const neverSettles = new Promise(() => {});
    const context = {
        console,
        document,
        navigator: {onLine: true},
        location: {href: 'https://excavator.test/excavator/work/'},
        history: {
            state: null,
            pushState(state) { this.state = state; },
            back() {},
        },
        localStorage: {getItem: () => null, setItem() {}},
        fetch: () => neverSettles.then(value => { fetchSettled = true; return value; }),
        setTimeout: () => 1,
        clearTimeout() {},
        window: {
            eoExcavatorLocalShiftLedger: {
                hourlyReport: async () => localPayload,
            },
            addEventListener: (name, callback) => { windowListeners[name] = callback; },
            removeEventListener() {},
            setTimeout: () => 1,
            clearTimeout() {},
        },
        URL,
        Date,
        Number,
        Promise,
        Object,
        Array,
        JSON,
        Intl,
    };
    context.window.window = context.window;
    vm.createContext(context);
    vm.runInContext(source, context);

    listeners.click({
        target: {closest: selector => selector === '[data-eo-hourly-report-open]' ? opener : null},
        preventDefault() {},
    });
    await Promise.resolve();
    await Promise.resolve();

    assert.equal(modal.hidden, false);
    assert.equal(fetchSettled, false, 'the HTTP request must still be pending');
    assert.match(subtitle.textContent, /Экскаватор 12/);
    assert.ok(
        content.children.some(child => String(child.className || '').includes('eo-hourly-report__hour')),
        'the durable local hour must be rendered before the GET settles',
    );
});
