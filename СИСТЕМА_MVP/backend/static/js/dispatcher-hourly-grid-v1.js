/* ============================================================================
   Часовая сетка смены.

   Сервер отдаёт готовый разрез в JSON (reports/hourly_grid.py), страница только
   рисует. Раскрытия — состояние экрана, а не сервера, поэтому автообновление
   отчёта их не сбрасывает: после подмены фрагмента сетка перерисовывается с тем
   же раскрытым часом и экскаватором.
   ============================================================================ */
(function () {
    "use strict";

    var POINT_VARS = ["--p1", "--p2", "--p3", "--p4", "--p5", "--p6", "--p7", "--p8", "--p9"];
    var state = { hour: null, excavator: null, cellExcavator: null, unit: "trips", expanded: false };
    var data = null;
    var renderedTable = null;
    // Первый показ сам выбирает текущий час. Дальше выбор принадлежит человеку:
    // если он свернул час, автообновление не должно раскрывать его заново.
    var hourChosenByUser = false;

    var lastSignature = "";

    function readData() {
        var node = document.getElementById("hourly-grid-data");
        if (!node) return null;
        try {
            // Данные лежат в data-атрибуте, а не в <script>: сборщик фрагмента
            // для автообновления вырезает из него все теги script и style,
            // поэтому после первого же обновления сетка осталась бы без данных.
            return JSON.parse(node.dataset.hourlyGrid || "null");
        } catch (error) {
            return null;
        }
    }

    function fmt(value) {
        return String(Math.round(Number(value) || 0)).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
    }

    function escapeHtml(value) {
        return String(value === null || value === undefined ? "" : value)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }

    function pointColor(key) {
        var order = Object.keys(data.points).sort();
        var index = order.indexOf(String(key));
        return "var(" + POINT_VARS[(index < 0 ? 0 : index) % POINT_VARS.length] + ")";
    }

    function lastClosedHour() {
        var last = null;
        data.hours.forEach(function (hour) { if (!hour.future) last = hour.index; });
        return last;
    }

    function cellOf(row, hourIndex) {
        var total = { trips: 0, volume: 0, running: 0, points: {} };
        row.fleets.forEach(function (fleet) {
            var cell = fleet.hours[hourIndex];
            if (!cell) return;
            total.trips += cell.trips;
            total.volume += cell.volume;
            total.running += cell.running;
            Object.keys(cell.points).forEach(function (key) {
                total.points[key] = (total.points[key] || 0) + cell.points[key];
            });
        });
        return total;
    }

    function openPoints() {
        if (state.hour === null) return [];
        var hour = data.hours[state.hour];
        if (!hour || hour.future) return [];
        return Object.keys(hour.points).sort(function (a, b) { return hour.points[b] - hour.points[a]; });
    }

    function maxCellValue() {
        var max = 1;
        data.rows.forEach(function (row) {
            row.fleets.forEach(function (fleet) {
                fleet.hours.forEach(function (cell) {
                    max = Math.max(max, state.unit === "volume" ? cell.volume : cell.trips);
                });
            });
        });
        return max;
    }

    function renderStrip() {
        var totals = data.totals;
        var box = document.getElementById("hourly-grid-strip");
        if (!box) return;
        var last = lastClosedHour();
        var previous = last === null ? null : last - 1;
        var delta = "—";
        if (last !== null && previous !== null && previous >= 0) {
            var difference = data.hours[last].volume - data.hours[previous].volume;
            delta = (difference >= 0 ? "+" : "−") + fmt(Math.abs(difference)) + " м³ к прошлому";
        }
        var runningTrips = totals.running;
        var idleCount = 0;
        data.rows.forEach(function (row) { idleCount += Object.keys(row.idle).length; });

        var issues = [];
        if (runningTrips) issues.push('<span class="is-danger">Рейсы в пути — ' + runningTrips + "</span>");
        if (idleCount) issues.push('<span class="is-risk">Простои — ' + idleCount + "</span>");
        if (!issues.length) issues.push('<span class="is-ok">Данные смены заполнены</span>');

        box.innerHTML =
            kpi("Смена", totals.elapsed_hours + " из " + totals.hours_total + " ч", totals.elapsed_hours ? "идёт" : "не началась") +
            kpi("Объём за смену", fmt(totals.volume) + " м³", totals.trips + " рейсов") +
            kpi("Последний час", last === null ? "—" : fmt(data.hours[last].volume) + " м³", delta) +
            kpi("Темп", fmt(totals.rate) + " м³/ч", "среднее по смене") +
            kpi("Прогноз к концу", fmt(totals.forecast) + " м³", "по текущему темпу") +
            '<div class="gd-hgrid-issues"><b>Проверить:</b>' + issues.join("") + "</div>";
    }

    function kpi(label, value, note, modifier) {
        return '<div class="gd-hgrid-kpi ' + (modifier || "") + '"><span>' + escapeHtml(label) +
            "</span><strong>" + escapeHtml(value) + "</strong><em>" + escapeHtml(note) + "</em></div>";
    }

    function renderGrid() {
        var table = document.getElementById("hourly-grid-table");
        if (!table) return;
        var points = openPoints();
        var max = maxCellValue();

        var head = '<thead><tr><th class="is-exc" rowspan="2">Экскаватор</th><th class="is-fleet" rowspan="2">а/с</th>';
        data.hours.forEach(function (hour) {
            var classes = ["is-hour"];
            if (hour.current) classes.push("is-now");
            if (hour.future) classes.push("is-future");
            if (hour.outside) classes.push("is-outside");
            if (hour.index === state.hour) classes.push("is-open");
            var span = (hour.index === state.hour && points.length) ? points.length + 1 : 1;
            head += '<th class="' + classes.join(" ") + '" data-hgrid-hour="' + hour.index + '" colspan="' + span +
                '" rowspan="' + (span > 1 ? 1 : 2) + '" title="' + escapeHtml(hour.range +
                (hour.outside ? " · вне часов смены: рейсы есть, смену открыли или закрыли не по часам" : "")) +
                '">' + escapeHtml(hour.label) + "</th>";
        });
        head += '<th class="is-exc is-total" rowspan="2">За смену</th></tr><tr>';
        if (points.length) {
            points.forEach(function (key) {
                head += '<th class="is-point">' + escapeHtml(data.points[key] || "Без точки") + "</th>";
            });
            head += '<th class="is-point">всего</th>';
        }
        head += "</tr></thead>";

        var body = "<tbody>";
        data.rows.forEach(function (row) {
            row.fleets.forEach(function (fleet, fleetIndex) {
                body += '<tr class="is-' + fleet.key + '">';
                if (fleetIndex === 0) {
                    body += '<th class="is-exc' + (state.excavator === row.id ? " is-open" : "") +
                        '" rowspan="2" data-hgrid-exc="' + row.id + '" title="раскрыть точки разгрузки этого экскаватора">' +
                        (state.excavator === row.id ? "▾ " : "▸ ") + escapeHtml(row.label) + "</th>";
                }
                body += '<th class="is-fleet">' + escapeHtml(fleet.label) + "</th>";
                data.hours.forEach(function (hour) {
                    var cell = fleet.hours[hour.index];
                    if (hour.index === state.hour && points.length) {
                        points.forEach(function (key) {
                            var own = hour.future ? 0 : (cell.points[key] || 0);
                            body += '<td class="is-point' + (own ? "" : " is-zero") + '">' + (own || "·") + "</td>";
                        });
                    }
                    if (hour.future) { body += '<td class="is-cell is-future"></td>'; return; }
                    var idle = row.idle[String(hour.index)];
                    var value = state.unit === "volume" ? cell.volume : cell.trips;
                    var classes = ["is-cell"];
                    if (hour.outside) classes.push("is-outside");
                    if (!value) classes.push("is-zero");
                    if (idle && fleet.key === "belaz") classes.push("is-idle");
                    if (hour.index === state.hour && (state.cellExcavator === null || state.cellExcavator === row.id)) {
                        classes.push("is-selected");
                    }
                    var mix = "";
                    if (cell.trips) {
                        mix = '<span class="gd-hgrid-mix">' + Object.keys(cell.points).map(function (key) {
                            return '<i style="width:' + (cell.points[key] / cell.trips * 100) + "%;background:" + pointColor(key) + '"></i>';
                        }).join("") + "</span>";
                    }
                    var title = row.label + " · " + fleet.label + " · " + hour.range + " · " + cell.trips + " рейсов · " +
                        fmt(cell.volume) + " м³" + (cell.running ? " · в пути " + cell.running : "") +
                        (idle ? " · простой " + idle.minutes + " мин: " + idle.reason : "");
                    body += '<td class="' + classes.join(" ") + '" data-hgrid-cell="' + row.id + '" data-hgrid-hour="' + hour.index +
                        '" title="' + escapeHtml(title) + '">' + (value ? (state.unit === "volume" ? fmt(value) : value) : "·") +
                        mix + (cell.running ? '<span class="gd-hgrid-run"></span>' : "") + "</td>";
                });
                body += '<td class="is-total"><b>' + fmt(fleet.volume) + " м³</b><i>· " + fleet.trips + " р.</i></td></tr>";
            });

            if (state.excavator === row.id) {
                row.points.forEach(function (point) {
                    body += '<tr class="is-sub"><th class="is-sub" colspan="2"><i style="background:' + pointColor(point.key) + '"></i>' +
                        escapeHtml(point.name) + "</th>";
                    data.hours.forEach(function (hour) {
                        if (hour.index === state.hour && points.length) {
                            points.forEach(function (key) {
                                var own = key === point.key ? (point.hours[String(hour.index)] || 0) : 0;
                                body += '<td class="is-point' + (own ? "" : " is-zero") + '">' + (own || "·") + "</td>";
                            });
                        }
                        if (hour.future) { body += '<td class="is-cell is-future"></td>'; return; }
                        var count = point.hours[String(hour.index)] || 0;
                        body += '<td class="is-cell' + (count ? "" : " is-zero") + '">' + (count || "·") + "</td>";
                    });
                    body += '<td class="is-total"><b>' + point.trips + " р.</b><i>· " + fmt(point.volume) + " м³</i></td></tr>";
                });

                body += '<tr class="is-sub is-idle-row"><th class="is-sub" colspan="2"><i style="background:var(--gd-red)"></i>Простои</th>';
                var idleTotal = 0;
                data.hours.forEach(function (hour) {
                    if (hour.index === state.hour && points.length) {
                        points.forEach(function () { body += '<td class="is-point is-zero">·</td>'; });
                    }
                    if (hour.future) { body += '<td class="is-cell is-future"></td>'; return; }
                    var idle = row.idle[String(hour.index)];
                    if (idle) idleTotal += idle.minutes;
                    body += '<td class="is-cell' + (idle ? " is-idle" : " is-zero") + '" title="' + escapeHtml(idle ? idle.reason : "") + '">' +
                        (idle ? idle.minutes + "м" : "·") + "</td>";
                });
                body += '<td class="is-total"><b>' + (idleTotal ? idleTotal + " мин" : "нет") + "</b><i>· " +
                    Object.keys(row.idle).length + " шт</i></td></tr>";
            }
        });
        body += "</tbody>";

        var foot = '<tfoot><tr><th class="is-exc" colspan="2">Итого за час</th>';
        data.hours.forEach(function (hour) {
            if (hour.index === state.hour && points.length) {
                points.forEach(function (key) { foot += "<th>" + (hour.points[key] || "·") + "</th>"; });
            }
            foot += "<th" + (hour.future ? ' style="opacity:.4"' : "") + ">" +
                (hour.future ? "—" : hour.trips + "<small>" + fmt(hour.volume) + "</small>") + "</th>";
        });
        foot += '<th class="is-total">' + data.totals.trips + " рейсов<small>" + fmt(data.totals.volume) + " м³</small></th></tr></tfoot>";

        var box = table.closest(".gd-hgrid-box");
        var keepTop = box ? box.scrollTop : 0;
        var keepLeft = box ? box.scrollLeft : 0;
        table.innerHTML = head + body + foot;
        renderedTable = table;
        if (box) { box.scrollTop = keepTop; box.scrollLeft = keepLeft; }

        var note = document.getElementById("hourly-grid-note");
        if (note) {
            var current = data.totals.current_hour;
            // Часы смены берём из самой смены, а не из первой и последней
            // колонки: крайние колонки могут быть за её пределами.
            var outside = data.totals.hours_outside || 0;
            note.textContent = "смена " + data.meta.time_range +
                (outside ? " · вне смены " + outside + " ч" : "") +
                " · " + (current === null ? "смена закрыта" : "текущий час " + data.hours[current].range) +
                " · раскрыт " + (state.hour === null ? "—" : data.hours[state.hour].range);
        }

        var legend = document.getElementById("hourly-grid-legend");
        if (legend) {
            var used = {};
            data.hours.forEach(function (hour) { Object.keys(hour.points).forEach(function (key) { used[key] = true; }); });
            legend.innerHTML = Object.keys(used).sort().map(function (key) {
                return '<span><i style="background:' + pointColor(key) + '"></i>' + escapeHtml(data.points[key] || "Без точки") + "</span>";
            }).join("") +
                '<span><i class="is-idle"></i>простой экскаватора</span>' +
                '<span><i class="is-run"></i>есть рейсы в пути</span>' +
                "<span>полоса под числом — куда ушли рейсы этого часа</span>";
        }
    }

    function renderSide() {
        var title = document.getElementById("hourly-grid-hour-title");
        var note = document.getElementById("hourly-grid-hour-note");
        var pointsBox = document.getElementById("hourly-grid-points");
        var trucksBox = document.getElementById("hourly-grid-trucks");
        var idleBox = document.getElementById("hourly-grid-idles");
        if (!title || !pointsBox) return;

        if (state.hour === null || data.hours[state.hour].future) {
            title.textContent = "Час";
            note.textContent = "выберите час в таблице";
            pointsBox.innerHTML = trucksBox.innerHTML = idleBox.innerHTML = '<div class="gd-hgrid-empty">Данных нет</div>';
            return;
        }

        var hour = data.hours[state.hour];
        var rows = state.cellExcavator === null
            ? data.rows
            : data.rows.filter(function (row) { return row.id === state.cellExcavator; });
        var selectedLabel = state.cellExcavator === null ? "" : (rows[0] ? rows[0].label : "");

        var trips = 0, volume = 0, running = 0, points = {};
        rows.forEach(function (row) {
            var cell = cellOf(row, state.hour);
            trips += cell.trips; volume += cell.volume; running += cell.running;
            Object.keys(cell.points).forEach(function (key) { points[key] = (points[key] || 0) + cell.points[key]; });
        });

        title.textContent = hour.range + (selectedLabel ? " · экскаватор " + selectedLabel : "");
        note.textContent = fmt(volume) + " м³ · " + trips + " рейсов" + (running ? " · в пути " + running : "");

        var keys = Object.keys(points).sort(function (a, b) { return points[b] - points[a]; });
        var max = keys.length ? points[keys[0]] : 1;
        pointsBox.innerHTML = keys.length ? keys.map(function (key) {
            return '<div class="gd-hgrid-bar"><b>' + escapeHtml(data.points[key] || "Без точки") + "</b><em>" + points[key] +
                ' р.</em><span><i style="width:' + Math.max(6, Math.round(points[key] / max * 100)) + "%;background:" +
                pointColor(key) + '"></i></span></div>';
        }).join("") : '<div class="gd-hgrid-empty">В этом часу рейсов не было</div>';

        var belaz = [], nhl = [];
        rows.forEach(function (row) {
            row.fleets.forEach(function (fleet) {
                var cell = fleet.hours[state.hour];
                if (!cell || !cell.trips) return;
                (fleet.key === "nhl" ? nhl : belaz).push({ label: row.label, trips: cell.trips });
            });
        });
        function chips(list, modifier) {
            if (!list.length) return '<div class="gd-hgrid-empty">нет</div>';
            return '<div class="gd-hgrid-chips">' + list.map(function (item) {
                return '<span class="gd-hgrid-chip ' + modifier + '">Э-' + escapeHtml(item.label) + " <small>· " + item.trips + "</small></span>";
            }).join("") + "</div>";
        }
        trucksBox.innerHTML = "<h4>БелАЗ по экскаваторам</h4>" + chips(belaz, "") +
            '<h4 style="margin-top:6px">NHL по экскаваторам</h4>' + chips(nhl, "is-nhl");

        var idleRows = [];
        rows.forEach(function (row) {
            var idle = row.idle[String(state.hour)];
            if (idle) {
                idleRows.push('<div class="gd-hgrid-idle"><i></i><b>Э-' + escapeHtml(row.label) + "</b><span>" +
                    escapeHtml(idle.reason) + "</span><span>" + idle.minutes + " мин</span></div>");
            }
        });
        idleBox.innerHTML = "<h4>Простои часа</h4>" +
            (idleRows.length ? idleRows.join("") : '<div class="gd-hgrid-empty">Простоев не было</div>');
    }

    // Обработчик один на документ и вешается ОДИН раз. Раньше bind() вызывался
    // при каждой перерисовке и вешал ещё один слушатель на те же кнопки:
    // клик срабатывал дважды, разворот тут же схлопывался обратно, а час
    // раскрывался и закрывался в одно нажатие.
    function handleClick(event) {
        var expand = event.target.closest("[data-hgrid-expand]");
        if (expand) {
            state.expanded = !state.expanded;
            applyExpanded();
            expand.blur();
            return;
        }

        var unit = event.target.closest("[data-hgrid-unit]");
        if (unit) {
            document.querySelectorAll("[data-hgrid-unit]").forEach(function (other) {
                other.classList.remove("is-active");
            });
            unit.classList.add("is-active");
            state.unit = unit.dataset.hgridUnit;
            renderGrid();
            unit.blur();
            return;
        }

        var table = event.target.closest("#hourly-grid-table");
        if (!table) return;

        var hourHead = event.target.closest("th[data-hgrid-hour]");
        if (hourHead && !hourHead.classList.contains("is-future")) {
            var index = Number(hourHead.dataset.hgridHour);
            state.hour = state.hour === index ? null : index;
            state.cellExcavator = null;
            hourChosenByUser = true;
            render();
            return;
        }

        var cell = event.target.closest("td[data-hgrid-cell]");
        if (cell) {
            state.hour = Number(cell.dataset.hgridHour);
            state.cellExcavator = Number(cell.dataset.hgridCell);
            hourChosenByUser = true;
            render();
            return;
        }

        var exc = event.target.closest("th[data-hgrid-exc]");
        if (exc) {
            var id = Number(exc.dataset.hgridExc);
            state.excavator = state.excavator === id ? null : id;
            state.cellExcavator = state.excavator;
            render();
        }
    }

    document.addEventListener("click", handleClick);

    function applyExpanded() {
        // Разворот живёт в состоянии экрана: карточку подменяет автообновление
        // отчёта, и класс на ней теряется вместе с узлом. Блокировку прокрутки
        // страницы снимаем всегда, когда развёрнутой карточки нет, иначе
        // страница осталась бы заблокированной навсегда.
        var card = document.querySelector(".gd-hgrid-card");
        var button = document.querySelector("[data-hgrid-expand]");
        var expanded = !!(state.expanded && card);
        if (card) card.classList.toggle("is-expanded", expanded);
        document.body.classList.toggle("gd-hgrid-locked", expanded);
        if (button) button.textContent = expanded ? "Свернуть" : "Развернуть";
        // В развёрнутом виде прокручивается сама таблица — значит, стрелки
        // должны попадать в неё, а не в блок отчёта под ней.
        var box = card ? card.querySelector(".gd-hgrid-box") : null;
        if (box && expanded) {
            box.tabIndex = 0;
            var active = document.activeElement;
            if (!active || active === document.body || !box.contains(active)) {
                try { box.focus({ preventScroll: true }); } catch (err) { box.focus(); }
            }
        } else if (box) {
            box.removeAttribute("tabindex");
            // Свернули — фокус, а вместе с ним стрелки, возвращаются в отчёт.
            focusScroller();
        }
    }

    function focusScroller() {
        // Стрелки и PageDown прокручивают тот блок, в котором стоит фокус.
        // Страница прокрутки не имеет — весь отчёт лежит в своём блоке, поэтому
        // фокус ставим на него, пока пользователь не выбрал что-то другое.
        var live = document.querySelector("[data-dispatcher-shift-report-live]");
        if (!live) return;
        var active = document.activeElement;
        if (active && active !== document.body && active !== document.documentElement) return;
        try { live.focus({ preventScroll: true }); } catch (err) { live.focus(); }
    }

    function assetRelease(url) {
        // Номер выпуска берём из адреса самого файла (?v=…): его подставляет
        // страница, значит цифра совпадает с выпуском страницы всегда, когда
        // загрузилась свежая страница, и отстаёт — когда приложение показало
        // старую. Руками номер в файле держать не нужно.
        var match = /[?&]v=([^&#]+)/.exec(url || "");
        return match ? decodeURIComponent(match[1]).replace(/^\D+/, "") : "?";
    }

    var JS_BUILD = assetRelease(document.currentScript && document.currentScript.src);

    function showBuild() {
        // Подпись версий: страница, скрипт и стили приходят из трёх разных
        // файлов, и приложение умеет отдать их вразнобой из своего кэша. Пока
        // на экране не видно, что именно загрузилось, любая правка проверяется
        // вслепую.
        var el = document.getElementById("hourly-grid-build");
        if (!el) return;
        var page = (el.dataset.pageRelease || "?").replace(/^\D+/, "");
        var link = document.querySelector('link[href*="dispatcher-hourly-grid-v1.css"]');
        var css = link ? assetRelease(link.getAttribute("href")) : "?";
        var live = document.querySelector("[data-dispatcher-shift-report-live]");
        var size = "";
        if (live) {
            var rect = live.getBoundingClientRect();
            size = " · окно " + window.innerHeight
                + " · холст " + canvasScale().toFixed(2)
                + " · блок " + Math.round(rect.top) + "→" + Math.round(rect.bottom)
                + " · низ " + Math.round(window.innerHeight - rect.bottom)
                + " · стр " + (document.documentElement.scrollHeight - window.innerHeight);
        }
        // Три числа совпадают, когда страница и её файлы одного выпуска; разные
        // цифры значат, что приложение отдало старую страницу или старый файл.
        var same = page === JS_BUILD && page === css;
        el.textContent = (same ? "сборка " + page : "сборка: стр " + page + " · скрипт " + JS_BUILD + " · стили " + css) + size;
    }

    function restoreScroll() {
        // Позицию прокрутки кладёт на новый блок скрипт автообновления отчёта
        // (шаблон, обработчик подмены): сам блок прокручиваемый, и без этого
        // диспетчера выбрасывало в начало отчёта при каждом обновлении.
        // Применяем после отрисовки — в момент подмены таблица ещё пустая, и
        // позиция обрезалась бы по короткому содержимому.
        var live = document.querySelector("[data-dispatcher-shift-report-live]");
        if (!live) return;
        var want = Number(live.dataset.restoreScroll || 0);
        if (want > 0) {
            live.scrollTop = want;
            delete live.dataset.restoreScroll;
            // Повтор на следующем кадре: к этому моменту раскладка досчитана,
            // и если что-то ещё сдвинуло блок, позиция встаёт ровно.
            window.requestAnimationFrame(function () {
                if (live.isConnected && Math.abs(live.scrollTop - want) > 1) {
                    live.scrollTop = want;
                }
            });
        }
        var box = document.querySelector(".gd-hgrid-card.is-expanded .gd-hgrid-box");
        var boxWant = (live.dataset.restoreGridBoxScroll || "").split(":");
        if (box && boxWant.length === 2) {
            box.scrollTop = Number(boxWant[0]) || 0;
            box.scrollLeft = Number(boxWant[1]) || 0;
            delete live.dataset.restoreGridBoxScroll;
        }
    }

    function canvasScale() {
        // Экран живёт внутри холста: тот масштабирует всю страницу целиком,
        // поэтому замеры в точках окна и размеры в стилях — разные единицы.
        var canvas = document.querySelector('.dispatcher-canvas[data-dispatcher-canvas="on"]');
        if (!canvas) return 1;
        var value = parseFloat(getComputedStyle(canvas).getPropertyValue("--dispatcher-canvas-scale"));
        return value > 0 ? value : 1;
    }

    function fitScroller() {
        // Страховка на случай, если внешняя раскладка отчётов пересилит наши
        // правила: блок отчёта должен доходить до низа окна. Если вёрстка уже
        // справилась, разница меньше зазора и ничего не трогаем.
        var live = document.querySelector("[data-dispatcher-shift-report-live]");
        if (!live) return;
        live.style.removeProperty("height");
        var rect = live.getBoundingClientRect();
        if (rect.top < 0) return;
        var scale = canvasScale();
        var free = Math.round((window.innerHeight - rect.top - 8 * scale) / scale);
        if (free < 200) return;
        if (Math.abs(rect.height / scale - free) > 8) live.style.height = free + "px";
    }

    window.addEventListener("resize", function () {
        fitScroller();
        showBuild();
    });

    function render() {
        if (!data) return;
        renderStrip();
        renderGrid();
        renderSide();
        applyExpanded();
        focusScroller();
        fitScroller();
        restoreScroll();
        showBuild();
    }

    function init() {
        var fresh = readData();
        if (!fresh) return;
        var node = document.getElementById("hourly-grid-data");
        var signature = (node && node.dataset.hourlyGrid ? node.dataset.hourlyGrid.length : 0) + ":" +
            (node && node.dataset.hourlyGrid ? node.dataset.hourlyGrid.slice(0, 400) : "");
        var sameData = signature === lastSignature;
        lastSignature = signature;
        data = fresh;
        if (sameData && renderedTable && document.getElementById("hourly-grid-table") === renderedTable) {
            // Данные те же и таблица на месте — перерисовывать нечего.
            return;
        }
        var exists = state.hour !== null && data.hours[state.hour] && !data.hours[state.hour].future;
        if (!exists && !hourChosenByUser) {
            state.hour = data.totals.current_hour !== null ? data.totals.current_hour : lastClosedHour();
        } else if (!exists && state.hour !== null) {
            state.hour = null;
        }
        if (state.excavator !== null && !data.rows.some(function (row) { return row.id === state.excavator; })) {
            state.excavator = null;
        }
        render();
    }

    document.addEventListener("keydown", function (event) {
        if (event.key !== "Escape") return;
        var card = document.querySelector(".gd-hgrid-card.is-expanded");
        if (!card) return;
        state.expanded = false;
        applyExpanded();
    });

    document.addEventListener("DOMContentLoaded", init);

    // Отчёт подменяет весь свой блок при автообновлении, вместе с таблицей и
    // данными. Ловим появление новой таблицы и рисуем в неё, сохраняя
    // раскрытый час и экскаватор.
    var observer = new MutationObserver(function () {
        var table = document.getElementById("hourly-grid-table");
        if (table && table !== renderedTable) init();
    });

    document.addEventListener("DOMContentLoaded", function () {
        observer.observe(document.body, { childList: true, subtree: true });
    });
}());
