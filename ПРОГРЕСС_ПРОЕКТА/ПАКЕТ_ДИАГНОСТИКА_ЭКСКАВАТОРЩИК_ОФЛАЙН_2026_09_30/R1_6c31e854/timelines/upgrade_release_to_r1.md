
## Обновление release fc227986 (v262) → R1 6c31e854 (v263), одна база; время UTC

| # | устройство | seq | событие | статус | код | смена | время телефона | получено |
|---|---|---|---|---|---|---|---|---|
| 61 | 8b294c8e | 1 | excavator.trip.loaded | accepted | — | 4 | 22:08:14.964 | 22:08:53.212 |
| 62 | 8b294c8e | 2 | excavator.downtime.started | accepted | — | 4 | 22:08:16.846 | 22:08:53.212 |
| 63 | 8b294c8e | 3 | excavator.downtime.started | accepted | — | 4 | 22:08:27.711 | 22:08:53.212 |
| 64 | 8b294c8e | 4 | excavator.downtime.ended | conflict | downtime_end_before_start | 4 | 22:08:38.060 | 22:08:53.212 |
| 65 | 8b294c8e | 5 | excavator.shift.closed | conflict | dependency_rejected | 4 | 22:08:40.294 | 22:08:53.212 |
| 66 | 90d6847e | 1 | excavator.trip.loaded | accepted | — | 5 | 22:12:37.305 | 22:13:15.563 |
| 67 | 90d6847e | 2 | excavator.downtime.started | accepted | — | 5 | 22:12:39.203 | 22:13:15.563 |
| 68 | 90d6847e | 3 | excavator.downtime.started | accepted | — | 5 | 22:12:50.036 | 22:13:15.563 |
| 69 | 90d6847e | 4 | excavator.downtime.ended | accepted | — | 5 | 22:13:00.384 | 22:13:15.563 |
| 70 | 90d6847e | 5 | excavator.shift.closed | accepted | — | 5 | 22:13:02.634 | 22:13:15.563 |
| 71 | f46e2b6d | 1 | excavator.shift.opened | conflict | employee_shift_already_open | — | 22:16:21.108 | 22:16:43.191 |
| 72 | f46e2b6d | 2 | excavator.downtime.started | retry | shift_reference_pending | — | 22:16:23.444 | 22:16:43.191 |
| 73 | f46e2b6d | 3 | excavator.downtime.ended | retry | dependency_pending | — | 22:16:31.777 | 22:16:43.191 |

| смена | открыта | закрыта |
|---|---|---|
| 4 | 22:07:55.495 | 22:11:49.555 |
| 5 | 22:12:16.485 | 22:13:02.634 |
| 6 | 22:14:10.000 | 22:15:56.589 |
| 7 | 22:16:17.664 | 22:18:49.733 |

| простой | причина | начало | конец |
|---|---|---|---|
| 32 | 20 | 22:08:53.244 | 22:08:53.244 |
| 33 | 17 | 22:08:53.244 | 22:08:53.244 |
| 34 | 18 | 22:08:53.244 | 22:11:49.555 |
| 35 | 20 | 22:12:37.305 | 22:12:39.203 |
| 36 | 17 | 22:12:39.203 | 22:12:50.036 |
| 37 | 18 | 22:12:50.036 | 22:13:00.384 |
| 38 | 20 | 22:14:42.044 | 22:18:49.733 |
