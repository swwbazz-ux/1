(function () {
    "use strict";

    var root = document.querySelector("[data-settlement-root]");
    if (!root) return;

    var state = {
        dormitory: "all",
        floor: "all",
        status: "all",
        search: ""
    };
    var selectedBed = null;
    var rooms = Array.from(root.querySelectorAll("[data-room-card]"));
    var visibleRoomCount = root.querySelector("[data-visible-room-count]");
    var selectionPlace = root.querySelector("[data-selection-place]");
    var selectionTitle = root.querySelector("[data-selection-title]");
    var selectionMeta = root.querySelector("[data-selection-meta]");
    var selectionCode = root.querySelector("[data-selection-code]");
    var searchInput = root.querySelector("[data-settlement-search]");

    function selectFilter(buttons, activeButton, key, value) {
        buttons.forEach(function (button) {
            var active = button === activeButton;
            button.classList.toggle("is-active", active);
            button.setAttribute("aria-pressed", String(active));
        });
        state[key] = value;
        applyFilters();
    }

    function matchesRoom(room) {
        var dormitoryMatch = (
            state.dormitory === "all"
            || room.dataset.dormitory === state.dormitory
        );
        var floorMatch = (
            state.floor === "all"
            || room.dataset.floor === state.floor
        );
        var statusMatch = (
            state.status === "all"
            || room.dataset.transferStatus === state.status
        );
        var searchMatch = (
            !state.search
            || String(room.dataset.search || "").includes(state.search)
        );
        return dormitoryMatch && floorMatch && statusMatch && searchMatch;
    }

    function applyFilters() {
        var matches = 0;
        rooms.forEach(function (room) {
            var match = matchesRoom(room);
            room.classList.toggle("is-filter-muted", !match);
            room.setAttribute("data-filter-match", String(match));
            if (match) matches += 1;
        });
        if (visibleRoomCount) visibleRoomCount.textContent = String(matches);
    }

    function showBed(bed) {
        if (bed.disabled) return;
        if (selectedBed) {
            selectedBed.classList.remove("is-selected");
            selectedBed.setAttribute("aria-pressed", "false");
        }
        selectedBed = bed;
        selectedBed.classList.add("is-selected");
        selectedBed.setAttribute("aria-pressed", "true");

        selectionPlace.textContent = [
            bed.dataset.dormitoryLabel,
            bed.dataset.floorLabel,
            bed.dataset.roomLabel
        ].join(" · ");
        selectionTitle.textContent = (
            "Блок " + bed.dataset.blockLabel + " · " + bed.dataset.positionLabel
        );
        selectionMeta.textContent = "Переданная комната · физическое койко-место свободно";
        selectionCode.textContent = bed.dataset.bedId;
    }

    [
        ["[data-dorm-filter]", "dormitory", "dormFilter"],
        ["[data-floor-filter]", "floor", "floorFilter"],
        ["[data-status-filter]", "status", "statusFilter"]
    ].forEach(function (definition) {
        var selector = definition[0];
        var stateKey = definition[1];
        var datasetKey = definition[2];
        var buttons = Array.from(root.querySelectorAll(selector));
        buttons.forEach(function (button) {
            button.addEventListener("click", function () {
                selectFilter(
                    buttons,
                    button,
                    stateKey,
                    button.dataset[datasetKey]
                );
            });
        });
    });

    if (searchInput) {
        searchInput.addEventListener("input", function () {
            state.search = searchInput.value.trim().toLowerCase();
            applyFilters();
        });
    }

    root.addEventListener("click", function (event) {
        var bed = event.target.closest("[data-bed]");
        if (bed) showBed(bed);
    });

    root.querySelectorAll("[data-bed]:not(:disabled)").forEach(function (bed) {
        bed.setAttribute("aria-pressed", "false");
    });

    applyFilters();
}());
