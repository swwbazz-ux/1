/* Планшет горного мастера: принудительное обновление service worker.

   Браузер перепроверяет скрипт service worker только при навигации и то
   не всегда: на планшете, который сутками не перезагружается, обновлённый
   воркер так и не активируется и страница остаётся в старом кэше.
   Поэтому на одном экране (/mining-master/tablet) мы сами просим
   регистрацию проверить обновление и сразу отдаём ожидающему воркеру
   SKIP_WAITING. После активации нового воркера браузер шлёт
   controllerchange — и страница перезагружается один раз на версию
   оболочки, чтобы уже отдавать разметку новой версии. Ключ защиты от
   петли включает версию (data-app-shell-version у <body>): после
   выпуска следующей версии перезагрузка снова возможна, иначе
   механизм сработал бы один раз за всё время жизни вкладки.

   Блок намеренно стоит первым в файле и ничего не ждёт от остального
   кода холста. На других экранах он выходит сразу по проверке пути. */
(function () {
    if (typeof navigator === "undefined" || !navigator.serviceWorker) return;
    var path = (location.pathname || "").replace(/\/+$/, "");
    if (path !== "/mining-master/tablet") return;
    var shellVersion = (document.body && document.body.dataset
        && document.body.dataset.appShellVersion) || "0";
    var reloadKey = "mm-sw-forced-reload:" + shellVersion;

    /* Пока оператор вводит PIN или открыт диалог/подтверждение, страницу
       не трогаем: перезагрузка сотрёт ввод. Проверка та же, что у штатного
       AppPwaContractGuard (templates/base.html, hasUnsafeWorkInProgress):
       активное поле ввода, открытые модальные окна, незавершённые действия. */
    function hasUnfinishedWork() {
        var guard = window.AppPwaContractGuard;
        if (guard && typeof guard.hasUnsafeWorkInProgress === "function") {
            try {
                if (guard.hasUnsafeWorkInProgress()) return true;
            } catch (error) {}
        }
        return Boolean(document.querySelector("dialog[open]"));
    }

    navigator.serviceWorker.getRegistration().then(function (registration) {
        if (!registration) return;
        registration.update().catch(function () {});
        if (registration.waiting) {
            registration.waiting.postMessage({ type: "SKIP_WAITING" });
        }
    }).catch(function () {});
    navigator.serviceWorker.addEventListener("controllerchange", function () {
        try {
            if (sessionStorage.getItem(reloadKey)) return;
        } catch (error) {}
        if (hasUnfinishedWork()) return;
        try {
            sessionStorage.setItem(reloadKey, "1");
        } catch (error) {}
        location.reload();
    });
})();

/* Холст диспетчерских экранов: пульт, аналитика, журнал, отчёты.
   Один файл на все экраны — раньше скрипт жил внутри шаблона пульта,
   и остальные вкладки оставались без холста: на мониторе с масштабом
   дисплея 125% они рисовались мелко и не заполняли окно. Подключается
   сразу после <main> внутри обёртки .dispatcher-canvas — до первой
   отрисовки, чтобы страница не мигала неотмасштабированной. */
(function () {
    /* Пульт целиком масштабируется как картинка.

       Раскладка пульта нарисована руками под конкретный размер и в
       нескольких местах опирается на vw/vh. Поэтому пульт живёт внутри
       холста постоянного логического размера, а под окно подгоняется
       не раскладка, а весь холст сразу — одним transform: scale().
       Внутренняя геометрия при этом не пересчитывается вообще: что на
       большом мониторе, что на маленьком видно одну и ту же картинку,
       только крупнее или мельче.

       Постоянна только ВЫСОТА холста — 1108 точек. От неё зависит
       читаемость: пульт нарисован под такую высоту, и на любом мониторе
       он показывается именно в этой вертикальной пропорции, просто
       крупнее или мельче. Ширина холста не фиксирована, а считается от
       пропорции окна: designWidth = высота * innerWidth / innerHeight.
       Поэтому масштаб выходит одинаковым по обеим осям, холст занимает
       окно целиком без пустых полей, а лишнюю ширину раскладка
       распределяет сама, как на широком мониторе. Растягивать пульт
       отдельно по ширине нельзя — исказились бы буквы и значки.

       Ниже 1400 опорная ширина не опускается: у́же раскладка пульта уже
       не собирается, и там лучше честные поля сверху и снизу.

       Откуда взялась высота 1108. Монитор 1920x1200, но в Windows включён
       масштаб дисплея 125%, поэтому браузер при зуме 100% отдаёт странице
       1536 точек, а не 1920 (окно приложения 1536x886, dpr 1.25 — измерено
       диагностической плашкой, гадать тут бесполезно). Прежний вид при
       зуме 80%, который просили повторить, собирался на 886/0.8 = 1108
       точек по высоте.

       Единицы вьюпорта внутри холста не работают: vw/vh продолжают мерить
       настоящее окно. Поэтому JS отдаёт в CSS --gd-vw и --gd-vh — сотые
       доли опорного размера, — а правила, которые от них зависят,
       перечислены в dispatcher-control-v1.css (секция про опорный размер).

       Единицы вьюпорта внутри холста не помогают: vw/vh считаются от
       настоящего окна, а не от логического размера, поэтому те немногие
       правила, которые от них зависят, зафиксированы отдельными
       правилами в dispatcher-control-v1.css (секция "Холст пульта") —
       посчитанными ровно для 2400x1500. Существующие правила при этом
       не переписаны ни одного.

       На телефоне в landscape пульт использует отдельный опорный размер
       1400×800. Этот режим включается только явным атрибутом
       data-dispatcher-phone-fit на холсте пульта, поэтому отчётные экраны
       не получают чужую трансформацию. Мобильный экран горного мастера
       сюда не попадает: desktop-файл для него не подключается. */
    var CANVAS_HEIGHT = 1108;
    var CANVAS_WIDTH_MIN = 1400;
    var PHONE_HEIGHT = 800;
    var PHONE_WIDTH_MIN = 1400;

    function fitDispatcherPhoneShell(shell, enabled) {
        /* innerWidth/innerHeight берутся напрямую: vh/dvh внутри уже
           уменьшенной transform-ом оболочки давали повторное сжатие и
           плавающую высоту при появлении системных панелей телефона. */
        if (!shell) return;
        if (!enabled) {
            shell.style.transform = "";
            shell.style.width = "";
            return;
        }
        var designWidth = Math.max(
            PHONE_WIDTH_MIN,
            Math.round(PHONE_HEIGHT * window.innerWidth / window.innerHeight)
        );
        var scale = Math.min(
            window.innerWidth / designWidth,
            window.innerHeight / PHONE_HEIGHT
        );
        shell.style.width = designWidth + "px";
        shell.style.transform = "scale(" + scale + ")";
    }

    function disableDispatcherCanvas(canvas) {
        canvas.setAttribute("data-dispatcher-canvas", "off");
        canvas.style.removeProperty("--dispatcher-canvas-w");
        canvas.style.removeProperty("--dispatcher-canvas-h");
        canvas.style.removeProperty("--dispatcher-canvas-scale");
        canvas.style.removeProperty("--gd-vw");
        canvas.style.removeProperty("--gd-vh");
    }

    function fitDispatcherCanvas() {
        var canvas = document.querySelector("[data-dispatcher-canvas]");
        if (!canvas) return;
        var shell = canvas.querySelector(".dispatcher-shell");
        var isMiningMasterMobile = document.body.classList.contains(
            "mining-master-mobile-screen"
        );
        var isPhoneLandscape = window.matchMedia(
            "(orientation: landscape) and (max-width: 1180px)"
        ).matches;
        var usePhoneShellFit = isPhoneLandscape
            && canvas.hasAttribute("data-dispatcher-phone-fit");
        if (isMiningMasterMobile || isPhoneLandscape) {
            disableDispatcherCanvas(canvas);
            fitDispatcherPhoneShell(shell, usePhoneShellFit);
            return;
        }
        fitDispatcherPhoneShell(shell, false);
        var canvasWidth = Math.max(
            CANVAS_WIDTH_MIN,
            Math.round(CANVAS_HEIGHT * window.innerWidth / window.innerHeight)
        );
        var scale = Math.min(
            window.innerWidth / canvasWidth,
            window.innerHeight / CANVAS_HEIGHT
        );
        canvas.style.setProperty("--dispatcher-canvas-w", canvasWidth + "px");
        canvas.style.setProperty("--dispatcher-canvas-h", CANVAS_HEIGHT + "px");
        canvas.style.setProperty("--dispatcher-canvas-scale", String(scale));
        /* Замена vw/vh внутри холста: сотые доли опорного размера. */
        canvas.style.setProperty("--gd-vw", (canvasWidth / 100) + "px");
        canvas.style.setProperty("--gd-vh", (CANVAS_HEIGHT / 100) + "px");
        canvas.setAttribute("data-dispatcher-canvas", "on");
    }

    fitDispatcherCanvas();
    window.addEventListener("resize", fitDispatcherCanvas);
    window.addEventListener("orientationchange", function () {
        setTimeout(fitDispatcherCanvas, 60);
    });
    if (window.visualViewport) {
        window.visualViewport.addEventListener("resize", fitDispatcherCanvas);
    }
})();
