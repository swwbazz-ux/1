/* APK-only shared TV action; presentation is the complete Android screen. */
(function (global) {
    "use strict";
    if (global.CopperTabletTv) return;
    let state = {phase:"idle", name:""}, queued = false;
    const bound = new WeakSet(), frames = new WeakSet();
    const css = `
.dispatcher-nav[data-cr-tv-nav]{grid-template-columns:repeat(5,minmax(0,1fr))!important;width:100%!important;max-width:none!important;gap:calc(8px / var(--cr-mm-scale,1))!important;align-items:center}
.dispatcher-nav .cr-mm-tv-button{box-sizing:border-box!important;display:inline-flex;align-items:center;justify-content:center;gap:calc(6px / var(--cr-mm-scale,1));height:calc(44px / var(--cr-mm-scale,1));min-height:calc(44px / var(--cr-mm-scale,1))!important;max-height:calc(44px / var(--cr-mm-scale,1));padding:0 calc(8px / var(--cr-mm-scale,1));border:1px solid #647b86;border-radius:6px;background:#11252f;color:#dce8ed;font:inherit;font-size:calc(12px / var(--cr-mm-scale,1))!important;font-weight:700;text-decoration:none;white-space:nowrap;cursor:pointer}
.cr-mm-tv-button svg{width:calc(22px / var(--cr-mm-scale,1));height:calc(22px / var(--cr-mm-scale,1));flex:none}
.dispatcher-nav .cr-mm-tv-button[data-state=connected]{border-color:#7edb42;color:#9cef64;background:#15311b}
.dispatcher-nav .cr-mm-tv-button[data-state=connecting]{border-color:#ffd200;color:#ffd200}
@media print{.cr-mm-tv-button{display:none!important}}
`;
    function send(op) { global.CopperFieldPage?.postMessage(JSON.stringify({op:"tv_action",action:op})); }
    function install(doc) {
        if (!doc?.head || !doc.body) return;
        let style=doc.getElementById("cr-tablet-tv-style");
        if(!style){style=doc.createElement("style");style.id="cr-tablet-tv-style";doc.head.appendChild(style);}
        // The field archive can restore a previous APK's style/control. Upgrade our own component.
        if(style.textContent!==css)style.textContent=css;
        for (const nav of doc.querySelectorAll(".dispatcher-nav")) {
            if(nav.dataset.crTvNav!=="true")nav.dataset.crTvNav="true";
            let button=nav.querySelector("[data-cr-tv]");
            if(button && button.tagName!=="A"){button.remove();button=null;}
            if (!button) {
                button=doc.createElement("a");button.href="#";button.setAttribute("role","button");button.className="cr-mm-tv-button";button.dataset.crTv="";
                button.setAttribute("data-role-readonly-safe","");
                button.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8m-4-4v4"/></svg><span></span>';
                nav.appendChild(button);
                console.info("cr-tv: button mounted");
            }
            const text=state.phase==="connected"?"Остановить":state.phase==="connecting"?"Подключаем…":state.phase==="stopping"?"Останавливаем…":"На ТВ";
            if (button.querySelector("span").textContent!==text) button.querySelector("span").textContent=text;
            if (button.dataset.state!==state.phase) button.dataset.state=state.phase;
            const aria=state.phase==="connected"?"Остановить показ на ТВ":"Показать приложение на ТВ";
            if(button.getAttribute("aria-label")!==aria)button.setAttribute("aria-label",aria);
            const title=(state.name?"ТВ: "+state.name:"Первое подключение телевизора")+". Удерживайте для выбора другого ТВ.";
            if(button.title!==title)button.title=title;
        }
        if (!bound.has(doc)) {
            bound.add(doc);
            doc.addEventListener("click",event=>{if(event.target.closest?.("[data-cr-tv]")){event.preventDefault();event.stopImmediatePropagation();send("toggle");}},true);
            doc.addEventListener("contextmenu",event=>{if(event.target.closest?.("[data-cr-tv]")){event.preventDefault();send("settings");}},true);
            new MutationObserver(schedule).observe(doc.body,{childList:true,subtree:true});
        }
    }
    function render() {
        queued=false;install(global.document);
        for (const frame of global.document.querySelectorAll("iframe[data-cr-planning-frame]")) {
            if (!frames.has(frame)) { frames.add(frame);frame.addEventListener("load",schedule); }
            try { install(frame.contentDocument); } catch (_) {}
        }
    }
    function schedule() { if(!queued){queued=true;global.requestAnimationFrame(render);} }
    global.CopperTabletTv={setState(value){state=value;schedule();}};
    if(global.document.readyState==="loading")global.document.addEventListener("DOMContentLoaded",schedule,{once:true});else schedule();
    send("state");
})(window);
