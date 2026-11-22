"use strict";
/* T-036 frontend-4: контракт ручного заселения в комнаты на 2 места.
   Проверяем:
   - поля «Подтверждаю: сотрудник — ИТР» и «Основание» показываются только для
     комнаты вместимостью <= 2 и только когда форма заселения открыта;
   - без подтверждения и основания запрос не уходит;
   - с подтверждением в существующий запрос добавляются confirm_itr=true и
     placement_reason;
   - обычные комнаты не меняются (в тело запроса ничего не добавляется);
   - серверный 409 по коду спецфонда получает понятную подсказку;
   - _room_card.html и settlement-clerk.js не тронуты, v37 не поднята. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const read = (relative) => fs.readFileSync(path.resolve(__dirname, relative), "utf8").replace(/\r\n/g, "\n");
const TEMPLATE = read("../../../templates/settlement/clerk_map.html");
const ROOM_CARD = read("../../../templates/settlement/_room_card.html");
const MAP_JS = read("../settlement-clerk.js");
const SPECIAL_CSS = read("../../css/settlement-special-fund-v1.css");
const SPECIAL_JS = read("../settlement-special-fund-v1.js");

test("special-fund assets are linked with their own version and load last", () => {
    assert.match(TEMPLATE, /css\/settlement-special-fund-v1\.css[\s\S]{0,90}-settlement-special-fund-v1/);
    assert.match(TEMPLATE, /js\/settlement-special-fund-v1\.js[\s\S]{0,90}-settlement-special-fund-v1/);
    assert.ok(
        TEMPLATE.indexOf("js/settlement-special-fund-v1.js") > TEMPLATE.indexOf("js/settlement-clerk.js"),
        "special-fund script loads after the map script"
    );
    assert.equal(TEMPLATE.split("settlement-map-v39").length - 1, 4);
    assert.equal(TEMPLATE.split("settlement-map-v37").length - 1, 0);
    assert.doesNotMatch(ROOM_CARD, /special-fund|specialFund/);
    assert.doesNotMatch(MAP_JS, /special-fund|specialFund/);
});

test("fields are mandatory only for manual-only rooms (capacity <= 2)", () => {
    assert.match(SPECIAL_JS, /var ITR_ROOM_MAX_CAPACITY = 2;/);
    assert.match(SPECIAL_JS, /function isManualOnlyRoom\(room\)/);
    assert.match(SPECIAL_JS, /capacity <= ITR_ROOM_MAX_CAPACITY/);
    assert.match(SPECIAL_JS, /room\.querySelectorAll\("\[data-bed\]"\)\.length/);
    assert.match(SPECIAL_JS, /Подтверждаю: сотрудник — ИТР/);
    assert.match(SPECIAL_JS, /"Основание"/);
});

test("the guard sends confirm_itr and placement_reason in the existing request", () => {
    assert.match(SPECIAL_JS, /body\.confirm_itr = true;/);
    assert.match(SPECIAL_JS, /body\.placement_reason = placementReason\(\);/);
    assert.match(SPECIAL_JS, /root\.dataset\.occupancyCreateUrl/);
    assert.match(SPECIAL_JS, /parsed\.action !== "settle" && parsed\.action !== "relocate"/);
    assert.doesNotMatch(SPECIAL_JS, /\/clerk\/settlement\/occupancies\//);
});

test("server 409 codes of the special fund get a clear next step", () => {
    Object.keys({
        itr_room_requires_itr: 1,
        itr_room_manual_only: 1,
        protected_room_forbidden: 1,
        protected_dispatch_only: 1
    }).forEach((code) => {
        assert.ok(SPECIAL_JS.includes(code), code);
    });
    assert.match(SPECIAL_JS, /response\.status !== 409/);
    assert.match(SPECIAL_JS, /new Response\(JSON\.stringify\(body\)/);
});

test("no innerHTML and only own CSS selectors", () => {
    assert.doesNotMatch(SPECIAL_JS, /innerHTML/);
    assert.doesNotMatch(SPECIAL_JS, /style\.display/);
    assert.match(SPECIAL_CSS, /\.settlement-special-fund \{/);
    assert.match(SPECIAL_CSS, /\.settlement-special-fund-error \{/);
    assert.doesNotMatch(SPECIAL_CSS, /\.settlement-room\b/);
    assert.doesNotMatch(SPECIAL_CSS, /--settlement-room-min-height/);
});

/* --- Исполнение в минимальной DOM-заглушке ------------------------------- */

function attrMatcher(selector) {
    const attribute = selector.slice(1, -1);
    return (node) => node.attributes && Object.prototype.hasOwnProperty.call(node.attributes, attribute);
}

function matches(node, selector) {
    if (selector === "[data-bed]") return Boolean(node.attributes && "data-bed" in node.attributes);
    if (selector === "[data-room-card]") return Boolean(node.attributes && "data-room-card" in node.attributes);
    if (selector === "[data-settle-button]") return Boolean(node.attributes && "data-settle-button" in node.attributes);
    if (selector === "[data-settlement-form]") return Boolean(node.attributes && "data-settlement-form" in node.attributes);
    if (selector === "[data-room-panel]") return Boolean(node.attributes && "data-room-panel" in node.attributes);
    if (selector === "[data-relocation-modal]") return Boolean(node.attributes && "data-relocation-modal" in node.attributes);
    if (selector === "[data-placement-feedback]") return Boolean(node.attributes && "data-placement-feedback" in node.attributes);
    if (selector.startsWith("[")) return attrMatcher(selector)(node);
    return false;
}

function makeNode(tag, attributes) {
    const classes = new Set();
    const node = {
        tagName: String(tag || "div").toUpperCase(),
        className: "",
        textContent: "",
        value: "",
        checked: false,
        hidden: false,
        parent: null,
        children: [],
        attributes: Object.assign({}, attributes || {}),
        listeners: {},
        focus() {},
        appendChild(child) {
            child.parent = this;
            this.children.push(child);
            return child;
        },
        insertBefore(child, reference) {
            child.parent = this;
            const index = this.children.indexOf(reference);
            if (index === -1) this.children.push(child);
            else this.children.splice(index, 0, child);
            return child;
        },
        setAttribute(name, value) {
            this.attributes[name] = String(value);
        },
        getAttribute(name) {
            return Object.prototype.hasOwnProperty.call(this.attributes, name)
                ? this.attributes[name]
                : null;
        },
        addEventListener(name, handler) {
            (this.listeners[name] = this.listeners[name] || []).push(handler);
        },
        querySelectorAll(selector) {
            const found = [];
            (function walk(current) {
                (current.children || []).forEach((child) => {
                    if (matches(child, selector)) found.push(child);
                    walk(child);
                });
            }(this));
            return found;
        },
        querySelector(selector) {
            return this.querySelectorAll(selector)[0] || null;
        },
        closest(selector) {
            let current = this;
            while (current) {
                if (matches(current, selector)) return current;
                current = current.parent;
            }
            return null;
        }
    };
    node.classList = {
        add: (name) => classes.add(name),
        remove: (name) => classes.delete(name),
        contains: (name) => classes.has(name),
        toggle: (name, force) => (force ? classes.add(name) : classes.delete(name))
    };
    return node;
}

function buildPage({ bedsInSpecial = 2, bedsInNormal = 4 } = {}) {
    const root = makeNode("div", {"data-settlement-root": ""});
    const specialRoom = makeNode("article", {"data-room-card": "", "data-room-id": "36", "data-room-number": "36"});
    const specialBeds = [];
    for (let index = 0; index < bedsInSpecial; index += 1) {
        const bed = makeNode("button", {"data-bed": "", "data-bed-id": "SPEC-" + index});
        specialRoom.appendChild(bed);
        specialBeds.push(bed);
    }
    const normalRoom = makeNode("article", {"data-room-card": "", "data-room-id": "12", "data-room-number": "12"});
    const normalBeds = [];
    for (let index = 0; index < bedsInNormal; index += 1) {
        const bed = makeNode("button", {"data-bed": "", "data-bed-id": "NORM-" + index});
        normalRoom.appendChild(bed);
        normalBeds.push(bed);
    }
    root.appendChild(specialRoom);
    root.appendChild(normalRoom);

    const panel = makeNode("aside", {"data-room-panel": ""});
    const placementPanel = makeNode("section");
    const form = makeNode("form", {"data-settlement-form": ""});
    form.hidden = true;
    const submit = makeNode("button", {"data-settle-button": ""});
    form.appendChild(makeNode("div"));
    form.appendChild(submit);
    placementPanel.appendChild(form);
    panel.appendChild(placementPanel);
    const feedback = makeNode("p", {"data-placement-feedback": ""});
    feedback.hidden = true;
    panel.appendChild(feedback);
    root.appendChild(panel);
    const relocationModal = makeNode("section", {"data-relocation-modal": ""});
    relocationModal.hidden = true;
    root.appendChild(relocationModal);

    return {root, form, submit, feedback, specialBeds, normalBeds, specialRoom, normalRoom};
}

function bootPage(page, { respond } = {}) {
    const calls = [];
    const timers = [];
    const submitHandlers = [];
    const documentStub = {
        cookie: "",
        querySelector: (selector) => (
            selector === "[data-settlement-root]" ? page.root : page.root.querySelector(selector)
        ),
        createElement: (tag) => makeNode(tag),
        addEventListener(name, handler) {
            if (name === "submit") submitHandlers.push(handler);
        }
    };
    const sandbox = {
        console,
        Promise,
        Date,
        Number,
        String,
        Boolean,
        Array,
        Object,
        Math,
        JSON,
        Set,
        Error,
        Response,
        document: documentStub,
        window: {
            setTimeout: (callback) => {
                timers.push(callback);
                return timers.length;
            },
            clearTimeout: () => undefined,
            MutationObserver: undefined,
            fetch: (url, init) => {
                calls.push({url, init});
                const reply = respond ? respond(url, init) : {status: 201, body: {ok: true}};
                return Promise.resolve({
                    ok: (reply.status || 200) < 400,
                    status: reply.status || 200,
                    statusText: reply.statusText || "",
                    json: () => Promise.resolve(reply.body || {}),
                    clone() {
                        return {json: () => Promise.resolve(reply.body || {})};
                    }
                });
            }
        }
    };
    page.root.dataset = {occupancyCreateUrl: "/clerk/settlement/occupancies/"};
    vm.runInNewContext(SPECIAL_JS, sandbox, {filename: "settlement-special-fund-v1.js"});
    return {
        calls,
        timers,
        window: sandbox.window,
        flush() {
            while (timers.length) {
                const pending = timers.splice(0, timers.length);
                pending.forEach((callback) => callback());
            }
        },
        click(target) {
            (page.root.listeners.click || []).forEach((handler) => handler({target}));
            this.flush();
        },
        submit() {
            const event = {
                target: page.form,
                defaultPrevented: false,
                stopped: false,
                preventDefault() { this.defaultPrevented = true; },
                stopPropagation() { this.stopped = true; }
            };
            submitHandlers.forEach((handler) => handler(event));
            return event;
        },
        field(name) {
            return page.form.querySelector("[data-special-fund-" + name + "]");
        },
        block() {
            return page.form.querySelector("[data-special-fund]");
        }
    };
}

function selectBed(page, bed) {
    page.specialBeds.concat(page.normalBeds).forEach((item) => {
        item.classList.remove("is-selected");
        item.setAttribute("aria-pressed", "false");
    });
    bed.classList.add("is-selected");
    bed.setAttribute("aria-pressed", "true");
    page.form.hidden = false;
}

test("runtime: fields appear for a 2-bed room and stay hidden for a normal room", () => {
    const page = buildPage();
    const app = bootPage(page);
    assert.equal(app.block().hidden, true, "до выбора койки поля скрыты");

    selectBed(page, page.specialBeds[0]);
    app.click(page.specialBeds[0]);
    assert.equal(app.block().hidden, false, "в комнате на 2 места поля должны появиться");

    selectBed(page, page.normalBeds[0]);
    app.click(page.normalBeds[0]);
    assert.equal(app.block().hidden, true, "в обычной комнате поля не нужны");
});

test("runtime: without confirmation the request is not sent", async () => {
    const page = buildPage();
    const app = bootPage(page);
    selectBed(page, page.specialBeds[0]);
    app.click(page.specialBeds[0]);

    const event = app.submit();
    assert.equal(event.defaultPrevented, true, "submit должен быть погашен");
    assert.equal(event.stopped, true, "submit не должен доходить до карты");
    assert.equal(app.field("error").hidden, false, "ошибка должна быть видна");
    assert.match(app.field("error").textContent, /Подтверждаю: сотрудник — ИТР/);
    assert.equal(app.calls.length, 0, "запрос не должен уйти");

    await assert.rejects(
        Promise.resolve(app.window.fetch("/clerk/settlement/occupancies/", {
            method: "POST",
            body: JSON.stringify({action: "settle", bed_stable_id: "SPEC-0"})
        })),
        /Подтверждаю: сотрудник — ИТР/
    );
    assert.equal(app.calls.length, 0, "обёртка fetch тоже не должна пропустить запрос");
});

test("runtime: confirmation adds confirm_itr and placement_reason to the body", async () => {
    const page = buildPage();
    const app = bootPage(page);
    selectBed(page, page.specialBeds[0]);
    app.click(page.specialBeds[0]);
    app.field("confirm").checked = true;
    app.field("reason").value = "  Приказ № 5 от 20.11.2026  ";

    const event = app.submit();
    assert.equal(event.defaultPrevented, false, "с подтверждением submit проходит");

    await app.window.fetch("/clerk/settlement/occupancies/", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({action: "settle", resident_id: 1, bed_stable_id: "SPEC-0"})
    });
    assert.equal(app.calls.length, 1);
    const body = JSON.parse(app.calls[0].init.body);
    assert.equal(body.confirm_itr, true);
    assert.equal(body.placement_reason, "Приказ № 5 от 20.11.2026");
    assert.equal(body.action, "settle");
});

test("runtime: normal rooms are not modified and relocation is covered too", async () => {
    const page = buildPage();
    const app = bootPage(page);
    selectBed(page, page.normalBeds[0]);
    app.click(page.normalBeds[0]);

    await app.window.fetch("/clerk/settlement/occupancies/", {
        method: "POST",
        body: JSON.stringify({action: "settle", resident_id: 1, bed_stable_id: "NORM-0"})
    });
    const normalBody = JSON.parse(app.calls[0].init.body);
    assert.equal("confirm_itr" in normalBody, false, "обычная комната не должна получать confirm_itr");
    assert.equal("placement_reason" in normalBody, false);

    selectBed(page, page.specialBeds[1]);
    app.click(page.specialBeds[1]);
    app.field("confirm").checked = true;
    app.field("reason").value = "Переселение ИТР";
    await app.window.fetch("/clerk/settlement/occupancies/", {
        method: "POST",
        body: JSON.stringify({action: "relocate", occupancy_id: 9, bed_stable_id: "SPEC-1"})
    });
    const relocationBody = JSON.parse(app.calls[1].init.body);
    assert.equal(relocationBody.confirm_itr, true);
    assert.equal(relocationBody.placement_reason, "Переселение ИТР");
});

test("runtime: 409 special code gets the next-step hint", async () => {
    const page = buildPage();
    const app = bootPage(page, {
        respond: () => ({
            status: 409,
            body: {
                ok: false,
                code: "itr_room_requires_itr",
                error: "Двухместная комната доступна только ИТР по подтверждённому основанию."
            }
        })
    });
    selectBed(page, page.specialBeds[0]);
    app.click(page.specialBeds[0]);
    app.field("confirm").checked = true;
    app.field("reason").value = "Приказ";

    const response = await app.window.fetch("/clerk/settlement/occupancies/", {
        method: "POST",
        body: JSON.stringify({action: "settle", resident_id: 1, bed_stable_id: "SPEC-0"})
    });
    assert.equal(response.status, 409);
    const payload = await response.json();
    assert.match(payload.error, /Двухместная комната доступна только ИТР/);
    assert.match(payload.error, /Отметьте «Подтверждаю: сотрудник — ИТР»/);
});
