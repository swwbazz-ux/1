/* Dispatcher desktop equipment search.
   Owns keyboard capture, search highlighting and rebinding after board fragments. */
(function (global, document) {
    "use strict";

    function createDispatcherEquipmentSearch() {
        var input = null;
        var box = null;
        var count = null;
        var query = "";
        var globalHandlersBound = false;
        var observer = null;
        var pending = null;

        function normalizeSearchText(value) {
            return String(value || "").trim().toLowerCase().replace(/k/g, "к").replace(/\s+/g, "");
        }

        function matchesSearch(node, needle) {
            var name = normalizeSearchText(node.getAttribute("data-equipment-name"));
            if (name && name.indexOf(needle) === 0) return true;
            var zone = normalizeSearchText(node.getAttribute("data-zone-label"));
            return !!zone && zone.indexOf(needle) === 0;
        }

        function applyEquipmentSearch() {
            var needle = normalizeSearchText(query);
            var hits = 0;
            var first = null;
            document.querySelectorAll(".dispatcher-shell [data-equipment-name]").forEach(function (node) {
                var hit = needle !== "" && matchesSearch(node, needle);
                node.classList.toggle("is-search-hit", hit);
                if (hit) {
                    hits += 1;
                    if (!first) first = node;
                }
            });
            document.body.classList.toggle("is-equipment-search", needle !== "");
            if (box) box.classList.toggle("has-query", needle !== "");
            if (count) {
                count.hidden = needle === "";
                count.textContent = String(hits);
                count.classList.toggle("is-none", hits === 0);
            }
            if (first && typeof first.scrollIntoView === "function") {
                first.scrollIntoView({ block: "nearest", inline: "nearest" });
            }
        }

        function clearEquipmentSearch() {
            if (input) input.value = "";
            query = "";
            applyEquipmentSearch();
            if (input && document.activeElement === input) input.blur();
        }

        function bindInput(nextInput) {
            input = nextInput;
            box = input.closest("[data-dispatcher-equipment-search-box]") || input.parentElement;
            count = document.querySelector("[data-dispatcher-equipment-search-count]");
            input.value = query;
            if (input.dataset.dispatcherEquipmentSearchBound !== "true") {
                input.dataset.dispatcherEquipmentSearchBound = "true";
                input.addEventListener("input", function () {
                    query = input.value;
                    applyEquipmentSearch();
                });
                input.addEventListener("keydown", function (event) {
                    if (event.key === "Escape") clearEquipmentSearch();
                });
            }
            applyEquipmentSearch();
        }

        function currentInputIsMissing() {
            return !input || input.isConnected === false;
        }

        function isTypingElsewhere() {
            var active = document.activeElement;
            if (!active || active === document.body || active === input) return false;
            var tag = active.tagName;
            return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || active.isContentEditable;
        }

        function isDialogOpen() {
            if (document.querySelector("dialog[open]")) return true;
            var modal = document.getElementById("app-confirm-modal");
            return !!(modal && !modal.hidden && getComputedStyle(modal).display !== "none");
        }

        function bindGlobalHandlers() {
            if (globalHandlersBound) return;
            globalHandlersBound = true;
            document.addEventListener("keydown", function (event) {
                if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
                if (currentInputIsMissing() && !bindEquipmentSearch()) return;
                if (document.activeElement === input || isTypingElsewhere() || isDialogOpen()) return;
                var key = event.key;
                if (key.length === 1 && /[0-9a-zа-яё\-]/i.test(key)) {
                    if (input.maxLength > 0 && input.value.length >= input.maxLength) return;
                    input.value += key;
                } else if (key === "Backspace" && input.value) {
                    input.value = input.value.slice(0, -1);
                } else {
                    return;
                }
                event.preventDefault();
                query = input.value;
                applyEquipmentSearch();
                input.focus({ preventScroll: true });
                input.setSelectionRange(input.value.length, input.value.length);
            });
            document.addEventListener("pointerdown", function (event) {
                if (query === "" || (box && box.contains(event.target))) return;
                clearEquipmentSearch();
            }, true);
        }

        function watchBoardFragments() {
            if (observer || typeof MutationObserver === "undefined") return;
            observer = new MutationObserver(function (records) {
                var nextInput = document.querySelector("[data-dispatcher-equipment-search]");
                var inputWasReplaced = currentInputIsMissing() || nextInput !== input;
                var boardChangedWhileSearching = query !== "" && records.some(function (record) {
                    return !(box && box.contains(record.target));
                });
                /* applyEquipmentSearch обновляет счётчик внутри box. Такая запись сама
                   создаёт childList mutation, но не должна запускать вечный таймер. */
                if (pending || (!inputWasReplaced && !boardChangedWhileSearching)) return;
                pending = setTimeout(function () {
                    pending = null;
                    bindEquipmentSearch();
                }, 60);
            });
            observer.observe(document.querySelector(".dispatcher-shell") || document.body, {
                childList: true,
                subtree: true
            });
        }

        function bindEquipmentSearch() {
            if (document.body.classList.contains("mining-master-mobile-screen")) return false;
            var nextInput = document.querySelector("[data-dispatcher-equipment-search]");
            if (!nextInput) return false;
            bindInput(nextInput);
            bindGlobalHandlers();
            watchBoardFragments();
            return true;
        }

        return {
            bind: bindEquipmentSearch,
            clear: clearEquipmentSearch
        };
    }

    global.createDispatcherEquipmentSearch = createDispatcherEquipmentSearch;
})(window, document);
