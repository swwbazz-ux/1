/* Dispatcher detail-card work settings.
   Owns horizon, block, rock type, dump destinations and their save request. */
(function (global, document) {
    "use strict";

    function createDispatcherDetailSettings(options) {
        options = options || {};
        var detailLayer = options.detailLayer || null;
        var getCsrfToken = options.getCsrfToken || function () { return ""; };
        var dispatcherRoleIsReadonly = options.roleIsReadonly || function () { return false; };
        var dispatcherShiftIsOpen = options.getShiftOpen || function () { return false; };
        var currentDispatcherBoardVersion = options.getBoardVersion || function () { return 0; };
        var closeEquipmentCard = options.closeEquipmentCard || function () {};

        var detailSettings = document.querySelector("[data-gd-detail-settings]");
        var detailSettingsTitle = document.querySelector("[data-gd-detail-settings-title]");
        var detailSettingsHint = document.querySelector("[data-gd-detail-settings-hint]");
        var detailSettingsStatus = document.querySelector("[data-gd-detail-settings-status]");
        var detailSettingHorizon = document.querySelector("[data-gd-setting-horizon]");
        var detailSettingBlock = document.querySelector("[data-gd-setting-block]");
        var detailSettingRock = document.querySelector("[data-gd-setting-rock]");
        var detailDestinationList = document.querySelector("[data-gd-destination-list]");
        var detailDestinationAdd = document.querySelector("[data-gd-destination-add]");
        var detailDestinationCount = document.querySelector("[data-gd-destination-count]");
        var detailSettingSave = document.querySelector("[data-gd-setting-save]");
        var detailDumpPointOptions = [];

        function fillDetailSettingSelect(select, items, selectedId) {
            if (!select) return;
            select.innerHTML = "";
            var placeholder = document.createElement("option");
            placeholder.value = "";
            placeholder.textContent = "Выберите";
            select.appendChild(placeholder);
            (items || []).forEach(function (item) {
                var option = document.createElement("option");
                option.value = String(item.id || "");
                option.textContent = item.name || "";
                option.selected = String(item.id || "") === String(selectedId || "");
                select.appendChild(option);
            });
        }

        function detailDestinationRows() {
            return detailDestinationList
                ? Array.prototype.slice.call(detailDestinationList.querySelectorAll("[data-gd-destination-row]"))
                : [];
        }

        function refreshDetailDestinationRows() {
            var rows = detailDestinationRows();
            var selectedIds = rows.map(function (row) {
                var select = row.querySelector("[data-gd-destination-select]");
                return select ? String(select.value || "") : "";
            }).filter(Boolean);
            rows.forEach(function (row) {
                var select = row.querySelector("[data-gd-destination-select]");
                var remove = row.querySelector("[data-gd-destination-remove]");
                if (select) {
                    Array.prototype.forEach.call(select.options, function (option) {
                        option.disabled = !!option.value
                            && option.value !== select.value
                            && selectedIds.indexOf(String(option.value)) !== -1;
                    });
                }
                if (remove) remove.disabled = rows.length <= 1;
            });
            if (detailDestinationCount) {
                detailDestinationCount.textContent = rows.length
                    ? rows.length + " " + (rows.length === 1 ? "точка" : rows.length < 5 ? "точки" : "точек")
                    : "не назначены";
            }
            if (detailDestinationAdd) {
                detailDestinationAdd.disabled = dispatcherRoleIsReadonly()
                    || !dispatcherShiftIsOpen()
                    || rows.length >= detailDumpPointOptions.length;
            }
        }

        function addDetailDestinationRow(destination) {
            if (!detailDestinationList) return;
            var row = document.createElement("div");
            row.className = "gd-detail-destination-row";
            row.setAttribute("data-gd-destination-row", "");

            var selectLabel = document.createElement("label");
            var selectCaption = document.createElement("span");
            selectCaption.textContent = "Точка";
            var select = document.createElement("select");
            select.setAttribute("data-gd-destination-select", "");
            fillDetailSettingSelect(select, detailDumpPointOptions, destination && destination.dump_point_id);
            selectLabel.appendChild(selectCaption);
            selectLabel.appendChild(select);

            var distanceLabel = document.createElement("label");
            distanceLabel.className = "gd-detail-destination-distance";
            var distanceCaption = document.createElement("span");
            distanceCaption.textContent = "Плечо, км";
            var distance = document.createElement("input");
            distance.type = "text";
            distance.inputMode = "decimal";
            distance.maxLength = 12;
            distance.placeholder = "—";
            distance.value = String(destination && destination.transport_distance_km || "").replace(".", ",");
            distance.setAttribute("data-gd-destination-distance", "");
            distanceLabel.appendChild(distanceCaption);
            distanceLabel.appendChild(distance);

            var remove = document.createElement("button");
            remove.type = "button";
            remove.className = "gd-detail-destination-remove";
            remove.setAttribute("data-gd-destination-remove", "");
            remove.setAttribute("aria-label", "Убрать точку разгрузки");
            remove.textContent = "×";

            select.addEventListener("change", refreshDetailDestinationRows);
            remove.addEventListener("click", function () {
                row.remove();
                refreshDetailDestinationRows();
            });
            row.appendChild(selectLabel);
            row.appendChild(distanceLabel);
            row.appendChild(remove);
            detailDestinationList.appendChild(row);
            refreshDetailDestinationRows();
        }

        function collectDetailDestinations() {
            var seen = Object.create(null);
            var destinations = [];
            detailDestinationRows().forEach(function (row) {
                var select = row.querySelector("[data-gd-destination-select]");
                var distance = row.querySelector("[data-gd-destination-distance]");
                var id = select ? String(select.value || "") : "";
                if (!id || seen[id]) return;
                seen[id] = true;
                destinations.push({
                    dump_point_id: id,
                    transport_distance_km: distance ? distance.value : ""
                });
            });
            return destinations;
        }

        function renderDetailSettings(settings) {
            if (!detailSettings) return;
            if (!settings || !settings.editable) {
                detailSettings.hidden = true;
                return;
            }
            detailSettings.hidden = false;
            if (detailSettingsTitle) detailSettingsTitle.textContent = settings.title || "Рабочие параметры комплекса";
            if (detailSettingsHint) detailSettingsHint.textContent = settings.hint || "";
            if (detailSettingsStatus) detailSettingsStatus.textContent = "";
            if (detailSettingHorizon) detailSettingHorizon.value = settings.loading_horizon || "";
            if (detailSettingBlock) detailSettingBlock.value = settings.loading_block || "";
            fillDetailSettingSelect(detailSettingRock, settings.rock_types, settings.rock_type_id);
            detailDumpPointOptions = settings.dump_points || [];
            if (detailDestinationList) detailDestinationList.innerHTML = "";
            var destinations = Array.isArray(settings.destinations) ? settings.destinations : [];
            if (!destinations.length && settings.dump_point_id) {
                destinations = [{
                    dump_point_id: settings.dump_point_id,
                    transport_distance_km: settings.transport_distance_km || ""
                }];
            }
            destinations.forEach(addDetailDestinationRow);
            if (!destinations.length && detailDumpPointOptions.length) {
                addDetailDestinationRow({dump_point_id: detailDumpPointOptions[0].id});
            }
            refreshDetailDestinationRows();
            if (detailSettingSave) detailSettingSave.disabled = dispatcherRoleIsReadonly() || !dispatcherShiftIsOpen();
        }

        function detailSettingsErrorMessage(code) {
            if (code === "stale_board") return "Данные пульта уже изменились. Закройте карточку и откройте снова.";
            if (code === "dispatcher_shift_required") return "Сначала откройте смену Горного диспетчера.";
            if (code === "invalid_transport_distance") return "Плечо должно быть числом не меньше нуля.";
            if (code === "invalid_work_settings") return "Выберите действующие породу и точку разгрузки.";
            if (code === "inactive_role") return "Роль неактивна — доступен только просмотр.";
            return "Не удалось сохранить параметры.";
        }

        function saveDetailSettings() {
            if (!detailLayer || !detailSettingSave) return;
            var url = detailLayer.dataset.gdSettingsUrl || "";
            if (!url) return;
            var destinations = collectDetailDestinations();
            if (!detailSettingRock || !detailSettingRock.value || !destinations.length) {
                if (detailSettingsStatus) detailSettingsStatus.textContent = "Выберите породу и хотя бы одну точку.";
                return;
            }
            detailSettingSave.disabled = true;
            if (detailSettingsStatus) detailSettingsStatus.textContent = "Сохраняю…";
            fetch(url, {
                method: "POST",
                credentials: "same-origin",
                cache: "no-store",
                headers: {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-CSRFToken": getCsrfToken(),
                    "X-Requested-With": "XMLHttpRequest"
                },
                body: JSON.stringify({
                    state_version: currentDispatcherBoardVersion(),
                    loading_horizon: detailSettingHorizon ? detailSettingHorizon.value : "",
                    loading_block: detailSettingBlock ? detailSettingBlock.value : "",
                    rock_type_id: detailSettingRock.value,
                    dump_point_ids: destinations.map(function (row) { return row.dump_point_id; }),
                    destinations: destinations
                })
            }).then(function (response) {
                return response.json().catch(function () { return {}; }).then(function (payload) {
                    if (!response.ok) {
                        var error = new Error("settings_request_failed");
                        error.code = payload.error || "";
                        throw error;
                    }
                    return payload;
                });
            }).then(function (payload) {
                if (detailSettingsStatus) detailSettingsStatus.textContent = "Настройки сохранены";
                if (payload && payload.settings) renderDetailSettings(payload.settings);
                if (global.AppRealtime && typeof global.AppRealtime.wake === "function") {
                    global.AppRealtime.wake("dispatcher_settings_saved");
                }
                global.setTimeout(closeEquipmentCard, 650);
            }).catch(function (error) {
                if (detailSettingsStatus) detailSettingsStatus.textContent = detailSettingsErrorMessage(error && error.code);
                detailSettingSave.disabled = dispatcherRoleIsReadonly() || !dispatcherShiftIsOpen();
            });
        }

        function resetDetailSettings() {
            if (detailSettings) detailSettings.hidden = true;
            if (detailSettingsStatus) detailSettingsStatus.textContent = "";
        }

        if (detailSettingSave) {
            detailSettingSave.addEventListener("click", saveDetailSettings);
        }
        if (detailDestinationAdd) {
            detailDestinationAdd.addEventListener("click", function () {
                var usedIds = collectDetailDestinations().map(function (row) {
                    return String(row.dump_point_id);
                });
                var nextPoint = detailDumpPointOptions.find(function (option) {
                    return usedIds.indexOf(String(option.id)) === -1;
                });
                if (nextPoint) addDetailDestinationRow({dump_point_id: nextPoint.id});
            });
        }

        return {
            collectDestinations: collectDetailDestinations,
            render: renderDetailSettings,
            reset: resetDetailSettings
        };
    }

    global.createDispatcherDetailSettings = createDispatcherDetailSettings;
})(window, document);
