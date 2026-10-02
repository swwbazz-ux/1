# E2-R2 — точная граница сохранённой браузерной проверки

Этот файл не объявляет новый браузерный прогон. Он фиксирует команды и фактическую границу проверки E2-R1, сохранённой в [исходном пакете](../ПАКЕТ_E2_R1_2026_10_02/README.md). Исходные raw не изменялись.

## Что действительно выполнено

1. Рабочая PWA-страница машиниста была открыта при работающем локальном Django.
2. Django остановили, а на том же `127.0.0.1:8765` запустили TCP-listener, который принимал соединения и намеренно не отвечал HTTP.
3. На уже открытой странице настоящий touch drag создал `excavator.trip.loaded` с `event_id=excavator-load-333430d9-59c2-4b2a-ad90-d6a58a6469a8` и `occurred_at=2026-10-02T00:17:05.756Z`.
4. Очередь показывала одну локальную запись; в SQLite receipt ещё отсутствовал.
5. Вкладку закрыли и открыли новую. Навигация при продолжающем молчать listener завершилась timeout. Оболочка стала видна лишь после остановки listener.
6. После возврата Django очередь дослала прежнее событие: один receipt, один `TripClientAction`, один рейс; `trip.loaded_at` равен исходному `occurred_at`, а не времени приёма.

Таким образом, доказаны сохранность очереди через restart и доставка после устранения зависшего соединения. Не доказан автономный старт оболочки, пока listener продолжает принимать и удерживать соединения. Это ограничение OFF-C1, не исправление E2.

## Команды подготовки изолированной SQLite QA-БД

Команды выполнялись в `СИСТЕМА_MVP/backend` отдельного worktree E2-R1. Значения телефона/PIN относятся только к локальному QA-сценарию.

```powershell
$env:EXCAVATOR_QA_ENABLED='1'
$env:EXCAVATOR_QA_DATABASE_NAME='<worktree>\СИСТЕМА_MVP\backend\db.sqlite3'
$env:EXCAVATOR_QA_PHONE='79990000001'
$env:EXCAVATOR_QA_PIN='100001'
$env:DRIVER_QA_PHONE='79990000002'
$env:DRIVER_QA_PIN='100002'
$env:ADMIN_QA_PHONE='79990000003'
$env:ADMIN_QA_PIN='100003'

$code="from trips.qa_simulator import load_excavator_qa_scenario; from shifts.services import open_excavator_shift; from shifts.models import EmployeeShift; from assignments.models import ExcavatorPlacement; s=load_excavator_qa_scenario(); open_shift=EmployeeShift.objects.filter(employee=s.operator,equipment=s.excavator,closed_at__isnull=True).first(); result={'shift_id':open_shift.id} if open_shift else open_excavator_shift(employee=s.operator,equipment=s.excavator,shift_type='day',fuel_value='5960',engine_hours_value='100',client_action_id='e2-r1-browser-shift'); p=ExcavatorPlacement.objects.get(excavator=s.excavator); p.zone=ExcavatorPlacement.Zone.ACTIVE; p.save(update_fields=['zone','changed_at']); print(result,p.zone)"
python manage.py shell -c $code
python manage.py run_excavator_qa_simulator --once
```

## Запуск и остановка Django

```powershell
$env:MVP_SERVER_PORT='8765'
$env:EXCAVATOR_QA_ENABLED='1'
$env:EXCAVATOR_QA_DATABASE_NAME='<worktree>\СИСТЕМА_MVP\backend\db.sqlite3'
$env:EXCAVATOR_QA_LABEL='E2-R1 SILENT PORT'
$env:EXCAVATOR_QA_PHONE='79990000001'
$env:EXCAVATOR_QA_PIN='100001'
$env:DRIVER_QA_PHONE='79990000002'
$env:DRIVER_QA_PIN='100002'
$env:ADMIN_QA_PHONE='79990000003'
$env:ADMIN_QA_PIN='100003'
Start-Process -FilePath 'cmd.exe' -ArgumentList @('/c','START_SERVER_MVP.bat') -WorkingDirectory '<worktree>' -WindowStyle Hidden

$env:MVP_SERVER_PORT='8765'
cmd /c STOP_SERVER_MVP.bat
```

## Молчащий порт

```powershell
powershell -ExecutionPolicy Bypass -File `
  .\ПРОГРЕСС_ПРОЕКТА\ПАКЕТ_E2_R1_2026_10_02\tools\silent_http_port.ps1 `
  -Port 8765 `
  -LogPath .\ПРОГРЕСС_ПРОЕКТА\ПАКЕТ_E2_R1_2026_10_02\raw\silent-port.log
```

Listener из исходного пакета принимает TCP-клиенты, но не читает и не пишет HTTP. Это не throttling и не ответ с ошибкой.

## Жест

После получения CDP-capability вкладки были отправлены события:

```javascript
const point = (x, y) => ({ x, y, id: 1, radiusX: 2, radiusY: 2, force: 1 });
await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [point(232, 220)] });
await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [point(330, 300)] });
await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [point(450, 390)] });
await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [point(570, 490)] });
await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [point(660, 550)] });
await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
```

## Restart и снимок БД

Restart страницы выполнялся закрытием управляемой вкладки, созданием новой вкладки и переходом на `http://127.0.0.1:8765/excavator/work/`. Переход при работающем listener получил timeout; это часть результата, а не PASS старта.

После остановки listener и возврата Django снимок снимался так:

```powershell
$env:EXCAVATOR_QA_ENABLED='1'
$env:EXCAVATOR_QA_DATABASE_NAME='<worktree>\СИСТЕМА_MVP\backend\db.sqlite3'
python manage.py shell -c "import json; from django.core.serializers.json import DjangoJSONEncoder; from core.models import OfflineFieldEvent; from trips.models import Trip,TripClientAction; eid='excavator-load-333430d9-59c2-4b2a-ad90-d6a58a6469a8'; q=OfflineFieldEvent.objects.get(event_id=eid); tid=q.result_payload.get('server_ids',{}).get('trip_id') or q.result_payload.get('trip_id'); t=Trip.objects.get(pk=tid); data={'receipt_count':OfflineFieldEvent.objects.filter(event_id=eid).count(),'receipt':{'event_id':q.event_id,'event_type':q.event_type,'status':q.status,'error_code':q.error_code,'sequence':q.sequence,'depends_on':q.depends_on,'occurred_at':q.occurred_at,'received_at':q.received_at,'payload':q.payload,'result_payload':q.result_payload},'trip':{'id':t.id,'status':t.status,'loaded_at':t.loaded_at,'completed_at':t.completed_at,'truck_id':t.truck_id,'excavator_id':t.excavator_id,'dump_point_id':t.dump_point_id},'action_count':TripClientAction.objects.filter(client_action_id=eid).count()}; print(json.dumps(data,cls=DjangoJSONEncoder,ensure_ascii=False,sort_keys=True))"
```

Машинно-читаемые итоговые значения остаются в `ПАКЕТ_E2_R1_2026_10_02/raw/silent-port-browser-qa.json` и `silent-port-db-after.json`.
