function storage() {
    const map = new Map();
    return {
        getItem: key => (map.has(key) ? map.get(key) : null),
        setItem: (key, value) => map.set(key, String(value)),
        removeItem: key => map.delete(key),
    };
}

function node(attrs) {
    const el = {
        dataset: Object.assign({}, attrs && attrs.dataset),
        hidden: !!(attrs && attrs.hidden),
        disabled: false,
        value: attrs && attrs.value !== undefined ? attrs.value : "",
        textContent: "",
        children: (attrs && attrs.children) || [],
        classList: {
            items: new Set(),
            add(name) { this.items.add(name); },
            remove(...names) { names.forEach(name => this.items.delete(name)); },
            toggle(name, on) { if (on) this.items.add(name); else this.items.delete(name); },
            contains(name) { return this.items.has(name); },
        },
        attributes: {},
        setAttribute(name, value) { this.attributes[name] = String(value); },
        removeAttribute(name) { delete this.attributes[name]; },
        matches: attrs && attrs.matches || (() => false),
        checkValidity: () => true,
        querySelectorAll(selector) {
            const found = [];
            (function walk(list) {
                list.forEach(child => {
                    if (child.matches(selector)) found.push(child);
                    walk(child.children);
                });
            })(this.children);
            return found;
        },
        querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
        closest(selector) { return selector === "[data-driver-shell]" ? el.shell || null : null; },
    };
    return el;
}

function matcher(test) { return selector => test(selector); }

function input(name, value) {
    return node({value, matches: matcher(selector => selector === `[name="${name}"]` || selector === "input[type='number']")});
}

function page({serverShiftId = "", preparedTruckId = "58", version = 10} = {}) {
    const openForm = node({
        dataset: {driverLocalShiftForm: "open"},
        matches: matcher(selector => selector === '[data-driver-local-shift-form="open"]'),
        children: [input("start_fuel", "410"), input("start_mileage", "12000"), input("start_engine_hours", "3000")],
    });
    const shiftIdInput = node({matches: matcher(selector => selector === 'input[name="shift_id"]')});
    const startFuel = node({dataset: {driverShiftStart: "start_fuel"}, matches: matcher(selector => selector === "[data-driver-shift-start]")});
    const closeForm = node({
        dataset: {driverLocalShiftForm: "close"},
        hidden: !serverShiftId,
        matches: matcher(selector => selector === '[data-driver-local-shift-form="close"]'),
        children: [shiftIdInput, startFuel, input("end_fuel", "300"), input("end_mileage", "12040"), input("end_engine_hours", "3008")],
    });
    openForm.hidden = !!serverShiftId;
    const gated = node({matches: matcher(selector => selector === "[data-driver-shift-gated]")});
    const shell = node({
        dataset: {
            driverAccessId: "7",
            driverShiftId: serverShiftId,
            driverServerShiftId: serverShiftId,
            driverCurrentTruckId: preparedTruckId,
            driverPreparedTruckId: serverShiftId ? "" : preparedTruckId,
            driverShiftType: "day",
        },
        children: [openForm, closeForm, gated],
    });
    [openForm, closeForm].forEach(form => { form.shell = shell; });
    const document = {
        body: {dataset: {operationalStateVersion: String(version)}},
        querySelector: selector => (selector === "[data-driver-shell]" ? shell : null),
    };
    return {shell, openForm, closeForm, gated, shiftIdInput, startFuel, document};
}


module.exports = {storage, node, page, matcher};
