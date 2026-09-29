# Выдержки CDP (ответы Runtime.evaluate, дословно)

Порт 9241 — headless Chrome стенда релиза (:8041), 9242 — стенда c05a5925 (:8042). Время — местное 30.09.2026 (UTC+10).
Команды — `node scripts/cdpx.js <cdp-порт> <часть URL> <eval|tap|hold|drag|shot|...>`.

## Исходное состояние (сервер работает, смена закрыта, SW установлен) — 02:28
```
9241 {"sw":"http://localhost:8041/excavator-sw.js activated","caches":{"excavator-mobile-shell-v262":"66+shell"},"shift":"closed","ver":"excavator-mobile-shell-v262","lock":"false","trucks":0,"dumps":1,"btn":"false/open"}
9242 {"sw":"http://localhost:8042/excavator-sw.js activated","caches":{"excavator-mobile-shell-v263":"67+shell"},"shift":"closed","ver":"excavator-mobile-shell-v263","lock":"false","trucks":1,"dumps":6,"btn":"false/open"}
```
Релиз при закрытой смене не рисует ни самосвалов, ни точек (`trucks:0`; `dumps:1` — заглушка «Нет точек»).

## П.1 Холодный старт, сервер ВЫКЛЮЧЕН (порт свободен, curl 000)
```
9241 (02:29) {"resp":39,"dcl":216,"load":218,"fcp":220,"transfer":0,"lock":"false","lockReason":"ready","shift":"closed","trucks":0,"conn":"weak", "text":"…Нет погрузки\nНазначает горный мастер\nККД\n0…"}
9242 (03:05) {"dcl":509,"fcp":568,"transfer":0,"lock":"false","lockReason":"ready","conn":"weak","shift":"closed","trucks":["1/dis=true"],"dumps":6}
```

## П.1 Холодный старт, СЛАБЫЙ СИГНАЛ (на порту программа, принимающая соединение и не отвечающая; scripts/blackhole.js) — 03:17
Опрос заголовка вкладки каждые 2 с:
```
t+2s  release=[(no title)] offc1=[Экскаваторщик]
…
t+56s release=[(no title)] offc1=[Экскаваторщик]
```
Навигация c05a5925: `{"resp":2526,"dcl":7700,"fcp":5224,"lock":"false","shift":"closed"}`.
Релиз: страница так и не отрисовалась; `Page.captureScreenshot` → timeout.

## П.2 «Начать смену» без сети (топливо 60 %, моточасы 12000, удержание 1,4 с)
```
9241 {"label":"НАЧАТЬ СМЕНУ","action":"open","pending":false,"disabled":false,"shiftState":"closed","nativeShiftId":"","workAvailable":"false","activeTab":"shift","err":"Сервер недоступен. Действие не сохранено.","events":[]}
9242 {"label":"ЗАКРЫТЬ СМЕНУ","action":"close","shiftState":"open","localShiftId":"excavator-shift-mumxghhp-1a2jv8g5jxre","nativeShiftId":"","work":"true","downtime":"true","truck":["dis=false"],"fb":true,"settings":true,"err":"","events":["excavator.shift.opened:pending"]}
```
`fb:true` / `settings:true` — кнопка свободного ковша и «Применить настройки» остаются выключенными после местного открытия.

## П.3 Работа без сети
Релиз — смена 3 открыта онлайн, затем сервер остановлен (03:12). c05a5925 — местная смена из п.2 (03:06).

Погрузка (перетаскивание «Тест 1» на ККД):
```
9241 {"card":["Тест 1 СОХРАНЕНО НА ТЕЛЕФОНЕ"],"kkd":"ККД 0 Тест 1","events":["excavator.trip.loaded:pending:shift=3"]}
9242 {"card":["Тест 1 СОХРАНЕНО НА ТЕЛЕФОНЕ pending=false saved=true"],"kkd":"ККД 0 Тест 1","events":["excavator.shift.opened:pending:shift=0:local=excavator-shift-mumxghhp-1a2jv8g5jxre","excavator.trip.loaded:pending:shift=0:local=excavator-shift-mumxghhp-1a2jv8g5jxre"]}
```
Свободный ковш:
```
9241 поиск «10»: {"input":"10","results":"№ tmp-10 БелАЗ · Основное: ЭКС-2 · Доступен Актуально","accept":false}
9241 принять:    {"sheetVisible":false,"cards":["tmp-10 ДОСТУПЕН Свободный ковш","Тест 1 СОХРАНЕНО НА ТЕЛЕФОНЕ"],"events":["trip.loaded:pending","free_bucket.accepted:pending"]}
9241 погрузить:  {"cards":["Тест 1 СОХРАНЕНО НА ТЕЛЕФОНЕ"],"otval":"Отвал 0","events":["trip.loaded:pending","free_bucket.accepted:pending","free_bucket.loaded:pending"]}
9242 тап по кнопке ковша: {"sheetOpen":false,"btnDisabled":true}
```
Простой (БВР → Обед → «Завершить простой»):
```
9241 {"active":"17","sel":["БВР"],…"downtime.started:pending"]} → {"active":"18","sel":["Обед"],…} → {"active":"","sel":[],"closeDis":true,"events":[…,"downtime.ended:pending"]}
9242 {"active":"17","sel":["БВР"],…} → {"active":"18","sel":["Обед"],…} → {"active":"","sel":[],"close":true,"events":[…,"excavator.downtime.ended:pending"]}
```
Отмена погрузки (свайп вверх по ККД):
```
9241 {"kkd":"ККД 0 нет","cards":["Тест 1 НАЗНАЧЕН"],"notice":"Отмена сохранена на телефоне.","events":["downtime.ended:pending","trip.loaded.cancelled:pending"]}
9242 {"kkd":"ККД 0 нет","truck":["Тест 1 НАЗНАЧЕН dis=false"],"notice":"Отмена сохранена на телефоне.","events":[…,"excavator.trip.loaded.cancelled:pending"]}
```
Настройки забоя:
```
9241 горизонт 76 → «Применить»: {"label":"ПРИМЕНИТЬ НАСТРОЙКИ","disabled":false,"pending":false,"notice":"Сервер недоступен. Действие не сохранено.","horizon":"76","header":"ЭКС-1 Смена 1 Гор. 75 Бл. 52 Окисленная руда"}
9242 вкладка «Забой» в местной смене: {"available":"false","horizon":true,"block":true,"rock":true,"apply":true,"applyAvail":"false"}
```

## П.4 «Закрыть смену» без сети (топливо 55 %, моточасы 12006)
```
9241 {"label":"ЗАКРЫТИЕ СОХРАНЕНО","action":"close","disabled":true,"shiftState":"open","modal":false,"events":["trip.loaded.cancelled:pending","shift.closed:pending"]}
9242 {"label":"ЗАКРЫТИЕ СОХРАНЕНО","action":"open","disabled":true,"shiftState":"closed","localShiftId":"excavator-shift-mumxghhp-1a2jv8g5jxre","work":"false","events":[…,"excavator.shift.closed:pending"]}
9242 через 5 с: {"label":"ЗАКРЫТИЕ СОХРАНЕНО","action":"open","disabled":true,"cls":"mobile-shift__action mobile-shift__action--primary is-disabled",…}
9242 после перезапуска вкладки без сети: {"label":"ЗАКРЫТИЕ СОХРАНЕНО","action":"open","disabled":true,"shiftState":"closed","work":"false","events":"7 pending: shift.opened,trip.loaded,downtime.started,downtime.started,downtime.ended,trip.loaded.cancelled,shift.closed"}
```

## П.5 Сервер вернулся (очередь ушла сама, ретрай ≤ 8 с)
```
9242 (03:10) ["downtime.ended:conflict:Время окончания простоя раньше его начала.","shift.closed:conflict:Предыдущее связанное действие требует сверки."]
9242 экран:  {"label":"НАЧАТЬ СМЕНУ","action":"open","disabled":false,"shiftState":"closed","native":"3","local":"excavator-shift-mumxghhp-1a2jv8g5jxre","work":"false","fb":false,"attention":"","events":["downtime.ended:conflict","shift.closed:conflict"]}
9241 (03:15) ["downtime.ended:conflict:Время окончания простоя раньше его начала.","shift.closed:conflict:Предыдущее событие требует сверки или отклонено."]
```
На сервере смена 3 открыта (см. ВРЕМЕННАЯ_ШКАЛА.md), телефон c05a5925 показывает «закрыта»; предупреждения работнику на экране нет (`attention:""`).

## П.6 Повторный запуск с сетью — метрика transferSize ложноположительна
```
9242 Page.reload: {"navTransfer":437178,"staticTotal":33,"fromNetwork":0,"netList":[]}
9241 Page.reload: {"staticTotal":32,"transferNonZero":0}
```
При этом лог сервера в эти же секунды показывает скачивание всей статики полными 200 (logs/*_sync_and_relaunch.log): ответ через service worker всегда даёт transferSize 0.
