const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");


class ClassListStub {
    constructor() {
        this.values = new Set();
    }

    add(...names) {
        names.forEach((name) => this.values.add(name));
    }

    remove(...names) {
        names.forEach((name) => this.values.delete(name));
    }

    contains(name) {
        return this.values.has(name);
    }

    toggle(name, force) {
        const enabled = force === undefined ? !this.contains(name) : Boolean(force);
        if (enabled) {
            this.add(name);
        } else {
            this.remove(name);
        }
        return enabled;
    }
}


class ElementStub {
    constructor({dataset = {}, textContent = ""} = {}) {
        this.attributes = new Map();
        this.children = [];
        this.classList = new ClassListStub();
        this.dataset = {...dataset};
        this.handlers = new Map();
        this.offsetHeight = 52;
        this.offsetWidth = 190;
        this.style = {};
        this.textContent = textContent;
    }

    addEventListener(name, handler) {
        if (!this.handlers.has(name)) {
            this.handlers.set(name, []);
        }
        this.handlers.get(name).push(handler);
    }

    appendChild(child) {
        this.children.push(child);
        return child;
    }

    cloneNode() {
        return new ElementStub({dataset: this.dataset, textContent: this.textContent});
    }

    dispatch(name, event = {}) {
        event.target = event.target || this;
        for (const handler of this.handlers.get(name) || []) {
            handler(event);
        }
    }

    getAttribute(name) {
        return this.attributes.has(name) ? this.attributes.get(name) : null;
    }

    querySelector(selector) {
        if (selector === "span") {
            return this.hint || null;
        }
        if (selector === ".employee-mini-avatar") {
            return null;
        }
        const formMatch = selector.match(/^\[data-dnd-form="([^"]+)"\]$/);
        if (formMatch) {
            return this.forms ? this.forms[formMatch[1]] || null : null;
        }
        return null;
    }

    remove() {
        this.removed = true;
    }

    setAttribute(name, value) {
        this.attributes.set(name, String(value));
    }
}


function dataTransfer(employeeId) {
    const values = new Map();
    if (employeeId) {
        values.set("text/plain", employeeId);
    }
    return {
        dropEffect: "",
        effectAllowed: "",
        getData(type) {
            return values.get(type) || "";
        },
        setData(type, value) {
            values.set(type, value);
        },
        setDragImage() {},
    };
}


function loadRuntime() {
    const templatePath = path.resolve(
        __dirname,
        "../../../templates/users/system_admin_employees.html"
    );
    const template = fs.readFileSync(templatePath, "utf8");
    const scripts = [...template.matchAll(/<script>([\s\S]*?)<\/script>/g)];
    const script = scripts
        .map((match) => match[1])
        .find((source) => source.includes('var zones = document.querySelectorAll("[data-dnd-action]")'));
    assert.ok(script, "admin employees runtime script was not found");

    let activeSubmissions = 0;
    const activeForm = new ElementStub();
    activeForm.requestSubmit = () => {
        activeSubmissions += 1;
    };

    const terminalRows = [
        ["101", "Архивный сотрудник", "В архиве", "archived"],
        ["103", "Уволенный сотрудник", "Уволен", "dismissed"],
        ["104", "Удаленный сотрудник", "Удален", "deleted"],
    ].map(([employeeId, employeeName, employeeStatus, employeeStatusCode]) => {
        const row = new ElementStub({
            dataset: {
                accessStatus: "Заблокирован",
                employeeId,
                employeeName,
                employeeStatus,
                employeeStatusCode,
            },
        });
        row.forms = {};
        return row;
    });
    const activeRow = new ElementStub({
        dataset: {
            accessStatus: "Заблокирован",
            employeeId: "102",
            employeeName: "Активный сотрудник",
            employeeStatus: "Активен",
            employeeStatusCode: "active",
        },
    });
    activeRow.forms = {unblock_access: activeForm};
    const activeWithoutAccessRow = new ElementStub({
        dataset: {
            accessStatus: "Нет доступа",
            employeeId: "105",
            employeeName: "Активный без доступа",
            employeeStatus: "Активен",
            employeeStatusCode: "active",
        },
    });
    activeWithoutAccessRow.forms = {};

    const hint = new ElementStub({textContent: "Вернуть вход"});
    const zone = new ElementStub({
        dataset: {
            dndAction: "unblock_access",
            dndUnavailableHint: "Сначала восстановите сотрудника",
        },
    });
    zone.hint = hint;
    zone.setAttribute("aria-disabled", "false");

    const body = new ElementStub();
    const allRows = [...terminalRows, activeRow, activeWithoutAccessRow];
    const rowsById = new Map(allRows.map((row) => [row.dataset.employeeId, row]));
    let domReady = null;
    const document = {
        body,
        addEventListener(name, handler) {
            if (name === "DOMContentLoaded") {
                domReady = handler;
            }
        },
        createElement() {
            return new ElementStub();
        },
        createTextNode(textContent) {
            return new ElementStub({textContent});
        },
        querySelector(selector) {
            const rowMatch = selector.match(/^\[data-employee-id="([^"]+)"\]$/);
            if (rowMatch) {
                return rowsById.get(rowMatch[1]) || null;
            }
            return null;
        },
        querySelectorAll(selector) {
            if (selector === "[data-employee-row]") {
                return allRows;
            }
            if (selector === "[data-dnd-action]") {
                return [zone];
            }
            return [];
        },
    };
    const context = {
        Boolean,
        document,
        window: {
            innerHeight: 844,
            innerWidth: 390,
            localStorage: {
                getItem() {
                    return null;
                },
                setItem() {},
            },
            location: {href: ""},
        },
    };
    vm.runInNewContext(script, context, {filename: templatePath});
    assert.equal(typeof domReady, "function");
    domReady();

    return {
        activeRow,
        activeWithoutAccessRow,
        get activeSubmissions() {
            return activeSubmissions;
        },
        hint,
        terminalRows,
        zone,
    };
}


test("terminal-status unlock drops are explained and send no request; active blocked drop submits once", () => {
    const runtime = loadRuntime();

    for (const terminalRow of runtime.terminalRows) {
        const terminalTransfer = dataTransfer(terminalRow.dataset.employeeId);
        terminalRow.dispatch("dragstart", {
            clientX: 0,
            clientY: 0,
            dataTransfer: terminalTransfer,
        });
        assert.equal(runtime.zone.getAttribute("aria-disabled"), "true");
        assert.equal(runtime.zone.classList.contains("is-unavailable"), true);
        assert.equal(runtime.hint.textContent, "Сначала восстановите сотрудника");

        let terminalDragOverPrevented = false;
        runtime.zone.dispatch("dragover", {
            clientX: 0,
            clientY: 0,
            dataTransfer: terminalTransfer,
            preventDefault() {
                terminalDragOverPrevented = true;
            },
        });
        assert.equal(terminalDragOverPrevented, false);
        assert.equal(terminalTransfer.dropEffect, "none");
        runtime.zone.dispatch("drop", {
            dataTransfer: terminalTransfer,
            preventDefault() {},
        });

        terminalRow.dispatch("dragend");
        assert.equal(runtime.zone.getAttribute("aria-disabled"), "false");
        assert.equal(runtime.zone.classList.contains("is-unavailable"), false);
        assert.equal(runtime.hint.textContent, "Вернуть вход");
    }

    const noAccessTransfer = dataTransfer("105");
    runtime.activeWithoutAccessRow.dispatch("dragstart", {
        clientX: 0,
        clientY: 0,
        dataTransfer: noAccessTransfer,
    });
    assert.equal(runtime.zone.getAttribute("aria-disabled"), "true");
    assert.equal(runtime.hint.textContent, "Доступ не назначен");
    runtime.zone.dispatch("drop", {
        dataTransfer: noAccessTransfer,
        preventDefault() {},
    });
    runtime.activeWithoutAccessRow.dispatch("dragend");
    assert.equal(runtime.activeSubmissions, 0);

    const activeTransfer = dataTransfer("102");
    runtime.activeRow.dispatch("dragstart", {
        clientX: 0,
        clientY: 0,
        dataTransfer: activeTransfer,
    });
    assert.equal(runtime.zone.getAttribute("aria-disabled"), "false");
    assert.equal(runtime.hint.textContent, "Вернуть вход");

    let activeDragOverPrevented = false;
    runtime.zone.dispatch("dragover", {
        clientX: 0,
        clientY: 0,
        dataTransfer: activeTransfer,
        preventDefault() {
            activeDragOverPrevented = true;
        },
    });
    assert.equal(activeDragOverPrevented, true);
    runtime.zone.dispatch("drop", {
        dataTransfer: activeTransfer,
        preventDefault() {},
    });
    assert.equal(runtime.activeSubmissions, 1);
});
