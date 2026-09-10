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

       Телефонный landscape-режим пульта (скрипт выше) и мобильный
       экран горного мастера сюда не попадают — там холст выключен и
       раскладка остаётся ровно прежней. */
    var CANVAS_HEIGHT = 1108;
    var CANVAS_WIDTH_MIN = 1400;

    function fitDispatcherCanvas() {
        var canvas = document.querySelector("[data-dispatcher-canvas]");
        if (!canvas) return;
        var isMiningMasterMobile = document.body.classList.contains(
            "mining-master-mobile-screen"
        );
        var isPhoneLandscape = window.matchMedia(
            "(orientation: landscape) and (max-width: 1180px)"
        ).matches;
        if (isMiningMasterMobile || isPhoneLandscape) {
            canvas.setAttribute("data-dispatcher-canvas", "off");
            canvas.style.removeProperty("--dispatcher-canvas-w");
            canvas.style.removeProperty("--dispatcher-canvas-h");
            canvas.style.removeProperty("--dispatcher-canvas-scale");
            canvas.style.removeProperty("--gd-vw");
            canvas.style.removeProperty("--gd-vh");
            return;
        }
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
