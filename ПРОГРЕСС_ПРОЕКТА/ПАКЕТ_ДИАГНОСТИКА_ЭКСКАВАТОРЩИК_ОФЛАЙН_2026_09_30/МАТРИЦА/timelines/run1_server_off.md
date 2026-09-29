## c05a5925, прогон 1 (сервер выключен) (время UTC, прогон с 21:10:00.000)

### Квитанции очереди (OfflineFieldEvent, сотрудник 3)

| # | событие | статус | код | смена | local_shift_id | время телефона | получено сервером | задержка |
|---|---|---|---|---|---|---|---|---|
| 58 | excavator.shift.opened | accepted | — | 4 | 6is152 | 21:10:58.340 | 21:16:35.305 | +337.0 с |
| 59 | excavator.trip.loaded | accepted | — | 4 | 6is152 | 21:11:15.025 | 21:16:35.305 | +320.3 с |
| 60 | excavator.downtime.started | accepted | — | 4 | 6is152 | 21:11:26.621 | 21:16:35.305 | +308.7 с |
| 61 | excavator.downtime.started | accepted | — | 4 | 6is152 | 21:11:38.938 | 21:16:35.305 | +296.4 с |
| 62 | excavator.downtime.ended | conflict | downtime_end_before_start | — | 6is152 | 21:11:51.387 | 21:16:35.305 | +283.9 с |
| 63 | excavator.trip.loaded.cancelled | accepted | — | 4 | 6is152 | 21:12:06.007 | 21:16:35.305 | +269.3 с |
| 64 | excavator.shift.closed | conflict | dependency_rejected | — | 6is152 | 21:14:12.851 | 21:16:35.305 | +142.5 с |
| 65 | excavator.shift.opened | conflict | dependency_rejected | — | tha34d | 21:14:42.216 | 21:16:35.305 | +113.1 с |
| 66 | excavator.trip.loaded | retry | shift_reference_pending | — | tha34d | 21:15:06.890 | 21:16:35.305 | +88.4 с |
| 67 | excavator.shift.closed | retry | dependency_pending | — | tha34d | 21:15:10.266 | 21:16:35.305 | +85.0 с |
| 68 | excavator.shift.opened | retry | dependency_pending | — | rd736l | 21:16:00.464 | 21:16:35.305 | +34.8 с |

### Смены сотрудника 3

| # | открыта | закрыта | топливо/моточасы начала | конца |
|---|---|---|---|---|
| 4 | 21:10:58.340 | ОТКРЫТА | ?/12000.00 | ?/None |

### Простои экскаватора 54

| # | причина | начало | конец | длительность | создан |
|---|---|---|---|---|---|
| 31 | 20 Ожидание самосвалов | 21:16:35.396 | 21:16:35.396 | 0.0 с | — |
| 32 | 17 БВР | 21:16:35.396 | 21:16:35.396 | 0.0 с | — |
| 33 | 18 Обед | 21:16:35.396 | — | идёт | — |

### Рейсы самосвала 1

| # | статус | смена погрузки | погрузка | отмена |
|---|---|---|---|---|
| 9 | cancelled | 4 | 21:11:15.025 | 21:12:06.007 |
128 objects imported automatically (use -v 2 for details).

