/* Keep a display-only copy of the verified board. Draft data lives in its own database. */
(function(win,doc){
    "use strict";
    if(win.CopperTabletPlanningCache)return;
    const KEY="cr-tablet-board-cache", SLOT="latest";
    const ignored="script,noscript,iframe,template,object,embed,base,link,meta,style,svg script,input[type=hidden],input[type=password],[data-observer-token]";
    let dbPromise, timer, watching=false, lastSignature="", writing=false, dirty=false;
    function database(){
        if(!dbPromise)dbPromise=new Promise((resolve,reject)=>{
            const op=indexedDB.open(KEY,1);
            op.onupgradeneeded=()=>op.result.createObjectStore("board");
            op.onerror=()=>reject(op.error);op.onsuccess=()=>resolve(op.result);
        }).catch(err=>{dbPromise=null;throw err;});
        return dbPromise;
    }
    async function record(value){
        const db=await database();
        return new Promise((resolve,reject)=>{
            const tx=db.transaction("board",value===undefined?"readonly":"readwrite");
            const shelf=tx.objectStore("board");
            const op=value===undefined?shelf.get(SLOT):value===null?shelf.delete(SLOT):shelf.put(value,SLOT);
            tx.oncomplete=()=>resolve(op.result);tx.onerror=tx.onabort=()=>reject(tx.error);
        });
    }
    function safeUrl(raw,base){
        try{const u=new URL(raw,base);
            return u.origin===location.origin&&u.pathname.startsWith("/static/")?u.href:"";
        }catch(_){return "";}
    }
    const imageCache=new Map();
    function packagedUrl(raw,base){
        if(!raw||raw[0]==="#"||raw.startsWith("data:"))return Promise.resolve(raw);
        const url=safeUrl(raw,base); if(!url)return Promise.resolve("");
        if(!imageCache.has(url)) {
            const pending=fetch(url,{credentials:"same-origin",cache:"force-cache",signal:AbortSignal.timeout(6000)})
            .then(async res=>{
                if(!res.ok)return "";
                const blob=await res.blob();
                if(blob.size>1200000 || blob.type.includes("text/html"))return "";
                return new Promise(resolve=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>resolve("");reader.readAsDataURL(blob);});
            }).catch(()=>"");
            imageCache.set(url,pending);
        }
        return imageCache.get(url);
    }
    let cachedStyles;
    async function styles(){
        if(cachedStyles)return cachedStyles;
        const parts=[];
        for(const sheet of doc.styleSheets){
            let css;
            try {css=Array.from(sheet.cssRules,r=>r.cssText).join("\n");}catch(_){continue;}
            const base=sheet.href||doc.baseURI, values=new Map();
            for(const match of css.matchAll(/url\(\s*(?:"([^"]*)"|'([^']*)'|([^)'"]+))\s*\)/gi)){
                const raw=match[1]??match[2]??match[3];
                if(!values.has(match[0]))values.set(match[0],packagedUrl(raw.trim(),base));
            }
            const replacements=new Map(await Promise.all([...values].map(async ([key,value])=>[key,'url("'+await value+'")'])));
            parts.push(css.replace(/url\(\s*(?:"([^"]*)"|'([^']*)'|([^)'"]+))\s*\)/gi,m=>replacements.get(m)||m));
        }
        cachedStyles=parts.join("\n");
        return cachedStyles;
    }
    function scrub(node){
        node.querySelectorAll(ignored).forEach(el=>el.remove());
        for(const el of [node,...node.querySelectorAll("*")]){
            for(const at of [...el.attributes]){
                if(/^on|csrf|token|password|srcset|formaction/i.test(at.name)||/^(action|form|target|autofocus|srcdoc)$/i.test(at.name))el.removeAttribute(at.name);
            }
            if(el.matches("input,textarea,select")){el.removeAttribute("value");el.disabled=true;}
            if(el.matches("form"))el.removeAttribute("method");
            if(el.matches("a")) {
                const route=el.getAttribute("href")||"";
                if(!route.startsWith("/mining-master/tablet/"))el.removeAttribute("href");
            }
        }
    }
    async function capture(){
        if(writing){dirty=true;return;}
        const board=doc.querySelector(".dispatcher-board[data-dispatcher-own-shift-open]"),
            canvas=board?.closest(".dispatcher-canvas");
        if(!canvas || !doc.documentElement.classList.contains("cr-mm-tablet") || doc.body.dataset.observerMode==="true")return;
        const version=board.dataset.operationalStateVersion;
        const signature=[version,board.dataset.dispatcherOwnShiftOpen,board.querySelectorAll("[data-equipment-id]").length].join(":");
        if(signature===lastSignature)return;
        writing=true;
        try{
            // Never include the employee login page, storage tokens, or unexpanded dialogs.
            const shell=canvas.querySelector("main.dispatcher-shell"), main=shell.cloneNode(false), clone=canvas.cloneNode(false);
            main.append(board.cloneNode(true));clone.append(main);
            for(const node of doc.body.querySelectorAll(":scope > #cr-mm-toolbar,:scope > .cr-mm-panel-handle,:scope > .cr-mm-panel-edge"))
                main.append(node.cloneNode(true));
            scrub(clone);
            const assets=[...clone.querySelectorAll("img")].map(async img=>{
                const src=await packagedUrl(img.getAttribute("src"),doc.baseURI);
                if(src)img.src=src;else img.removeAttribute("src");
            });
            const [css]=await Promise.all([styles(),...assets]);
            const saved={markup:clone.outerHTML,css,at:Date.now(),version,root:doc.documentElement.className,
                inline:doc.documentElement.getAttribute("style")||"",body:doc.body.className,
                attrs:Object.fromEntries([...doc.documentElement.attributes].filter(at=>/^data-cr-/.test(at.name)).map(at=>[at.name,at.value]))};
            await record(saved);lastSignature=signature;
            console.info("cr-tablet-copy: original board cached");
        }finally{writing=false;if(dirty){dirty=false;schedule();}}
    }
    function schedule(){clearTimeout(timer);timer=setTimeout(()=>capture().catch(()=>{}),500);}
    function watch(){
        if(watching)return;watching=true;
        const running=doc.querySelector(".dispatcher-shell");if(!running)return;
        schedule();win.addEventListener("online",schedule);
        new MutationObserver(changes=>{
            if(changes.some(c=>c.type==="attributes" || [...c.addedNodes].some(n=>n.nodeType===1&&n.matches(".dispatcher-board"))))schedule();
        }).observe(running,{childList:true,subtree:true,attributes:true,attributeFilter:["data-operational-state-version","data-dispatcher-own-shift-open"]});
    }
    let savedLayout, savedOwner;
    function layoutKey(rows){
        return JSON.stringify(rows.map(row=>[row.excavator_id,[...row.truck_ids].sort((a,b)=>a-b)])
            .sort((a,b)=>a[0]-b[0]));
    }
    async function showActivated(){
        if(!savedLayout || !savedOwner)return;
        const batch=(await win.CopperTabletDraft?.current())?.activation;
        if(batch?.status!=="queued"||batch.stamp!==savedOwner||!batch.base||
            layoutKey(batch.base.excavators)!==savedLayout)return;
        if(win.CopperTabletPlanning?.projectSavedBoard(doc,batch.target)){
            const label=doc.querySelector(".cr-copy-reconnect");
            if(label)label.textContent="Применено на планшете · Ожидает связи";
        }
    }
    async function restore(){
        if(!doc.documentElement.hasAttribute("data-cr-saved-board"))return;
        const retry=doc.querySelector("[data-cr-reconnect]");
        const reconnect=()=>location.replace("/mining-master/tablet/");
        retry?.addEventListener("click",reconnect);
        const saved=await record();
        if(!saved?.markup)return;
        const style=doc.createElement("style");style.textContent=saved.css;doc.head.append(style);
        doc.documentElement.className=saved.root;doc.documentElement.style.cssText=saved.inline;
        for(const [name,value] of Object.entries(saved.attrs||{}))doc.documentElement.setAttribute(name,value);
        doc.body.className=saved.body; doc.body.dataset.appRoleCode="mining_master";
        const parsed=new DOMParser().parseFromString(saved.markup,"text/html"),canvas=parsed.querySelector(".dispatcher-canvas");
        if(!canvas)return;
        const board=canvas.querySelector(".dispatcher-board");
        // Keep only the authentic header open with the link to the independent draft.
        doc.documentElement.setAttribute("data-cr-panel-open","top");
        for(const el of canvas.querySelectorAll("[data-cr-panel]")){
            const top=el.dataset.crPanel==="top";el.inert=!top;el.setAttribute("aria-hidden",String(!top));el.dataset.crPanelState=top?"open":"closed";
        }
        canvas.querySelectorAll(".cr-mm-panel-edge,.cr-mm-panel-handle").forEach(n=>n.remove());
        const top=board.querySelector(".dispatcher-topbar");
        if(top){top.inert=false;top.setAttribute("aria-hidden","false");}
        doc.body.replaceChildren(canvas);
        const tools=doc.createElement("button");tools.type="button";tools.className="cr-copy-reconnect";
        tools.textContent="Без связи · Повторить";tools.addEventListener("click",reconnect);
        doc.body.append(tools);
        // This document has no live handlers and cannot submit a form or navigate
        // to any live mutation even if the browser has since rejoined the network.
        doc.addEventListener("submit",e=>{e.preventDefault();e.stopImmediatePropagation();},true);
        doc.addEventListener("click",e=>{
            if(tools.contains(e.target)||e.target.closest("a[data-cr-planning-tab]"))return;
            if(e.target.closest("a,button,form")){e.preventDefault();e.stopImmediatePropagation();}
        },true);
        // An unverified new operator cannot edit a previous shift indefinitely.
        if(Date.now()-saved.at>13*3600000)board.dataset.dispatcherOwnShiftOpen="false";
        const end=board.querySelector('[data-confirm-variant="shift-end"]');
        if(board.dataset.dispatcherOwnShiftOpen==="true"&&end?.dataset.confirmPerson){
            savedOwner=JSON.stringify([end.dataset.confirmDate,end.dataset.confirmTime,end.dataset.confirmPerson]);
            savedLayout=layoutKey([...board.querySelectorAll('.dispatcher-complex-card[data-equipment-id]')].filter(n=>Number(n.dataset.equipmentId)>0)
                .map(n=>({excavator_id:Number(n.dataset.equipmentId),truck_ids:[...n.querySelectorAll('[data-dispatcher-drag="truck"][data-equipment-id]')].map(t=>Number(t.dataset.equipmentId))})));
            await showActivated();
        }
    }
    win.CopperTabletPlanningCache={watch,restore,showActivated};
})(window,document);
