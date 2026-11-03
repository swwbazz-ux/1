/* A prepared tablet keeps the authentic role pages and their static files. Login pages
   are public shells. A workplace page is scoped to the individually verified master. */
(function (win,doc) {
    "use strict";
    if(win.FieldArchive)return;
    const root="/mining-master/tablet/", login=root+"login/";
    const paths=new Set([root,login,...["dispatcher/reports/","dispatcher/shift-log/","dispatcher/management/",
        "dispatcher/mining-volumes/","dispatcher/transport/","dispatcher/downtimes/","dispatcher/shift-reports/trucks/",
        "dispatcher/shift-reports/excavation/","dispatcher/shift-reports/hourly/","dispatcher/shift-reports/hourly-grid/",
        "reports/shift-analytics/","reports/volume/","reports/customer-daily/","reports/templates/","reports/downtimes/"].map(p=>root+p)]);
    const network=win.fetch.bind(win), active=new Map(), files=new Map(), maxPages=40;
    let database, background, rendering=false;
    const photoPath="/media/employee_photos/";
    function removePortraits(page){
        let changed=false;
        for(const holder of page.querySelectorAll(".dispatcher-active-avatar")){
            if(!holder.querySelector("img,picture,source"))continue;
            const badge=holder.closest(".dispatcher-active-badge");
            const name=badge?.querySelector(".dispatcher-active-text strong")?.textContent.trim()||"";
            holder.textContent=badge?.classList.contains("is-muted")?"-":name.split(/\s+/).slice(0,2).map(w=>w[0]||"").join("").toUpperCase();
            changed=true;
        }
        for(const node of page.querySelectorAll('img[src*="/media/employee_photos/"],source[srcset*="/media/employee_photos/"],img[srcset*="/media/employee_photos/"]')){
            node.remove();changed=true;
        }
        return changed;
    }
    function withoutPortraits(html,fragment=false){
        if(typeof html!=="string" || !/<(?:img|source|picture)\b/i.test(html) ||
            !(html.includes("dispatcher-active-avatar")||html.includes(photoPath)))return html;
        const page=new DOMParser().parseFromString(html,"text/html");
        if(!removePortraits(page))return html;
        return fragment?page.body.innerHTML:"<!doctype html>"+page.documentElement.outerHTML;
    }
    async function clearLegacyPhotoResponses(){
        const done="cr-master-photo-response-cleanup-v1";
        if(!win.caches||localStorage.getItem(done))return;
        for(const name of await caches.keys()){
            const cache=await caches.open(name);
            for(const request of await cache.keys()){
                const url=new URL(request.url);
                if(url.origin===location.origin&&url.pathname.startsWith(photoPath))await cache.delete(request,{ignoreVary:true});
            }
        }
        localStorage.setItem(done,"1");
    }
    function db(){
        if(!database)database=new Promise((resolve,reject)=>{
            const q=indexedDB.open("cr-original-master-pages",2);
            q.onupgradeneeded=e=>{
                for(const name of ["pages","static"])if(!q.result.objectStoreNames.contains(name))q.result.createObjectStore(name);
                if(!e.oldVersion)return;
                const rows=q.transaction.objectStore("pages").openCursor();
                rows.onsuccess=()=>{const row=rows.result;if(!row)return;
                    const html=withoutPortraits(row.value.html);
                    if(html!==row.value.html)row.update({...row.value,html});
                    row.continue();
                };
                const shelf=q.transaction.objectStore("static"),keys=shelf.openKeyCursor();
                keys.onsuccess=()=>{const key=keys.result;if(!key)return;
                    if(String(key.key).includes(photoPath))shelf.delete(key.key);
                    key.continue();
                };
            };
            q.onsuccess=()=>resolve(q.result);q.onerror=()=>reject(q.error);
        }).catch(err=>{database=null;throw err;});
        return database;
    }
    async function record(shelf,key,value){
        const database=await db();
        return new Promise((resolve,reject)=>{
            const tx=database.transaction(shelf,value===undefined?"readonly":"readwrite"),s=tx.objectStore(shelf);
            const req=value===undefined?s.get(key):s.put(value,key);
            tx.oncomplete=()=>resolve(req.result);tx.onabort=tx.onerror=()=>reject(tx.error);
        });
    }
    const scope=actor=>actor?.verified && actor.role==="mining_master" ?
        JSON.stringify([actor.key,actor.id??null,actor.access_id??null]) : "";
    const pageKey=(actor,route)=>JSON.stringify([new URL(route,location.origin).pathname===login?"@login":scope(actor),route]);
    function routeOf(address){
        const u=new URL(address,location.origin);
        if(u.origin!==location.origin || !paths.has(u.pathname)||u.username||u.password||u.search.length>600)return "";
        return u.pathname+u.search;
    }
    function safeStatic(address,base){
        if(!address || /^(?:#|data:|blob:)/i.test(address))return "";
        try{const u=new URL(address,base);return u.origin===location.origin && u.pathname.startsWith("/static/")?u.href:"";}catch{return "";}
    }
    const packagedIcon=url=>/^\/static\/img\/equipment\/(?:truck|excavator)-(?:gray|blue|green|red|yellow)\.png$/.test(new URL(url).pathname);
    async function get(url,timeout=9000){
        const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),timeout);
        try{
            const response=await network(url,{credentials:"same-origin",cache:"no-store",signal:controller.signal});
            if(!response.ok||new URL(response.url).origin!==location.origin)throw Error("Страница временно недоступна.");
            return response;
        }finally{clearTimeout(timer);}
    }
    const dataUrl=blob=>new Promise((resolve,reject)=>{const read=new FileReader();read.onload=()=>resolve(read.result);read.onerror=()=>reject(read.error);read.readAsDataURL(blob);});
    async function asset(url){
        if(!files.has(url)){
            const p=(async()=>{
                const cached=await record("static",url);
                if(cached)return cached;
                const response=await get(url),type=response.headers.get("content-type")||"";
                if(type.includes("text/html"))throw Error("Вместо файла пришла страница входа.");
                const blob=await response.blob();
                if(blob.size>12*1024*1024)throw Error("Файл слишком велик для автономной копии.");
                const data=await dataUrl(blob);await record("static",url,data);return data;
            })().catch(error=>{files.delete(url);throw error;});
            files.set(url,p);
        }
        return files.get(url);
    }
    async function styleText(css,base){
        const replacements=new Map();
        for(const m of css.matchAll(/url\(\s*(?:"([^"]*)"|'([^']*)'|([^)'"]+))\s*\)/gi)){
            const uri=safeStatic((m[1]??m[2]??m[3]).trim(),base);
            if(uri&&!replacements.has(m[0]))replacements.set(m[0],packagedIcon(uri)?'url("'+uri+'")':asset(uri).then(value=>'url("'+value+'")').catch(()=>'url("")'));
        }
        const resolved=new Map(await Promise.all([...replacements].map(async ([k,v])=>[k,await v])));
        return css.replace(/url\(\s*(?:"([^"]*)"|'([^']*)'|([^)'"]+))\s*\)/gi,m=>resolved.get(m)||m);
    }
    async function stylesheet(url){
        const key="inline-css:"+url,previous=await record("static",key);
        if(previous!==undefined)return previous;
        const res=await get(url);
        if(!/text\/css/i.test(res.headers.get("content-type")||""))throw Error("Система не вернула стили страницы.");
        const css=await styleText(await res.text(),url);await record("static",key,css);return css;
    }
    async function collect(raw,route,actor) {
        const page=new DOMParser().parseFromString(raw,"text/html"),entry=route.split("?")[0]===login;
        if(page.body.dataset.observerMode==="true"||page.body.dataset.observerControl==="true")throw Error("Нельзя сохранить пульт наблюдателя.");
        if(entry) {
            if(!page.querySelector(".tablet-auth-panel"))throw Error("Не удалось проверить форму сотрудника.");
        }else if(!scope(actor)||page.body.dataset.appRoleCode!=="mining_master" ||
                 (route.split("?")[0]===root&&!page.querySelector(".dispatcher-board")))throw Error("Не удалось подтвердить рабочий экран.");
        const base=new URL(route,location.origin).href;
        const tokens=[...page.querySelectorAll('input[name=csrfmiddlewaretoken],meta[name=csrf-token]')].map(n=>n.value||n.content).filter(Boolean);
        page.querySelectorAll('meta[http-equiv],base,iframe,object,embed,link[rel~=preload],link[rel~=preconnect],link[rel~=manifest],link[rel~=icon],link[rel=apple-touch-icon],input[name=tablet_handover_token]').forEach(n=>n.remove());
        page.querySelectorAll('input[name=csrfmiddlewaretoken],input[type=password],input[name=phone],input[name=reauth_phone],input[name*=access_code]').forEach(n=>n.setAttribute("value",""));
        page.querySelectorAll("meta[name=csrf-token]").forEach(n=>n.content="");
        page.querySelectorAll("[data-observer-token]").forEach(n=>n.removeAttribute("data-observer-token"));
        removePortraits(page);
        const assign=[];
        for(const n of page.querySelectorAll("script[src],link[rel~=stylesheet],img[src]")){
            const address=n.getAttribute("src")||n.getAttribute("href"),url=safeStatic(address,base);
            if(n.matches("img")&&!url){
                if(!address?.startsWith("data:image/"))n.remove();
                continue;
            }
            if(!url){n.remove();continue;}
            if(n.matches("img")&&packagedIcon(url)){n.src=url;continue;}
            if(n.matches("script")){
                assign.push(async()=>{n.src=await asset(url);n.removeAttribute("integrity");});
            }else if(n.matches("link")){
                assign.push(async()=>{
                    const style=page.createElement("style");
                    if(n.media)style.media=n.media;
                    style.textContent=await stylesheet(url);n.replaceWith(style);
                });
            }else assign.push(async()=>{try{n.src=await asset(url);}catch{n.removeAttribute("src");}});
        }
        let index=0;
        await Promise.all(Array.from({length:Math.min(5,assign.length)},async()=>{while(index<assign.length)await assign[index++]();}));
        for(const style of page.querySelectorAll("style")) {
            if(/url\([^)]*(?:\/static\/|\.\.\/)/.test(style.textContent))style.textContent=await styleText(style.textContent,base);
        }
        page.documentElement.dataset.crFieldLocal="true";
        if(!entry)page.documentElement.dataset.crFieldPortraitOwner=scope(actor);
        const policy=page.createElement("meta");policy.httpEquiv="Content-Security-Policy";
        policy.content="default-src 'none'; script-src 'unsafe-inline' data:; style-src 'unsafe-inline' data:; img-src data:; font-src data:; connect-src 'self'; frame-src 'self' about:; object-src 'none'; base-uri 'none'; form-action 'none'";
        page.head.prepend(policy);
        let html="<!doctype html>"+page.documentElement.outerHTML;
        for(const token of tokens){if(token.length>15)html=html.split(token).join("");}
        return {html,route,scope:entry?"@login":scope(actor),version:Number(page.body.dataset.operationalStateVersion)||0,at:Date.now()};
    }
    async function capture(route,actor,{force=false,raw,signedReport=false}={}){
        route=routeOf(route);
        if(!route || (!scope(actor)&&!route.startsWith(login)))return null;
        const key=pageKey(actor,route);
        const reportScope=win.FieldReports?.handles(route)?await win.FieldReports.scopeId(actor):null;
        const jobKey=JSON.stringify([key,actor?.generation||"",reportScope,signedReport]);
        if(active.has(jobKey))return active.get(jobKey);
        const job=(async()=>{
            const last=await record("pages",key);
            if(last&&!raw&&!force&&(!reportScope||last.report_scope===reportScope)&&Date.now()-last.at<90000)return last;
            if(signedReport){
                // Only this fresh role- and route-bound response can bypass the shared browser cookie.
                const response=await win.FieldTransport.readReport(route,actor);
                if(!response.headers.get("content-type")?.includes("text/html"))throw Error("Система не вернула страницу отчёта.");
                raw=await response.text();
            }else if(!raw){
                const response=await get(route);
                if(new URL(response.url).pathname!==route.split("?")[0])throw Error("Нужен личный вход в систему.");
                raw=await response.text();
            }
            const data=await collect(raw,route,actor);
            if(reportScope)data.report_scope=reportScope;
            // A login/handover during a long download can never bind another user's HTML to this actor.
            if(signedReport){
                const current=await win.FieldLedger.read();
                if(current.locked || !current.shift.is_open || scope(current.actor)!==scope(actor) || current.actor.generation!==actor.generation)
                    throw Error("Сотрудник сменился во время загрузки.");
            }else if(!route.startsWith(login)){
                const live=await win.FieldIdentity.confirmScope(actor);
                if(!live)throw Error("Сотрудник сменился во время загрузки.");
            }
            if(data.html.length>24*1024*1024)throw Error("Страница слишком велика для локального хранения.");
            await record("pages",key,data);
            if(route===root)win.CopperFieldPage?.postMessage(JSON.stringify({op:"prepared"}));
            win.FieldReports?.remember(data,actor)?.catch(()=>{});
            if(route.includes("?")&&!route.startsWith(login))await prune();
            return data;
        })().finally(()=>active.delete(jobKey));
        active.set(jobKey,job);return job;
    }
    async function prune(){
        const database=await db();
        await new Promise((resolve,reject)=>{
            const tx=database.transaction("pages","readwrite"),s=tx.objectStore("pages"),request=s.openCursor(),queries=[];
            request.onsuccess=()=>{const cursor=request.result;if(cursor){if(cursor.value.route.includes("?")&&cursor.value.scope!=="@login")queries.push([cursor.key,cursor.value.at]);cursor.continue();return;}
                queries.sort((a,b)=>b[1]-a[1]).slice(maxPages).forEach(([key])=>s.delete(key));};
            tx.oncomplete=resolve;tx.onabort=tx.onerror=()=>reject(tx.error);
        });
    }
    async function prime(actor,options={}){
        if(!scope(actor))throw Error("Система не подтвердила мастера.");
        const core=await capture(root,actor,options);
        if(!background)background=(async()=>{
            const pages=[login+"?intent=start_shift",login,...Array.from(paths).filter(p=>p!==root&&p!==login)];
            let index=0;
            await Promise.all(Array.from({length:2},async()=>{while(index<pages.length){try{await capture(pages[index++],actor);}catch{/* Keep the last successful copy. */}}}));
        })().finally(()=>{background=null;});
        return core;
    }
    async function render(route,{actor}={}){
        route=routeOf(route);if(!route||rendering)return false;
        const state=await win.FieldLedger.read();
        actor=actor||state.actor;
        if(!route.startsWith(login) && (!scope(actor)||state.locked || !state.shift?.is_open))route=login;
        if(win.FieldReports?.handles(route))return win.FieldReports.open(route);
        const page=await record("pages",pageKey(actor,route));
        if(!page || (page.scope!=="@login" && page.scope!==scope(actor)))return !!(await win.FieldReports?.missing(route,actor));
        if(!win.CopperFieldPage?.postMessage)throw Error("Нативный компонент полевого пульта недоступен.");
        // Old prepared pages predate the explicit personal owner tag. Their IDB key
        // and record scope were checked above; stamp that identity before rendering.
        let html=page.html;
        if(page.scope!=="@login" && !/^<!doctype html><html\b[^>]*\bdata-cr-field-portrait-owner=/i.test(html)){
            const owner=page.scope.replace(/&/g,"&amp;").replace(/"/g,"&quot;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
            html=html.replace(/<html\b/i,()=>'<html data-cr-field-portrait-owner="'+owner+'"');
        }
        rendering=true;
        try{win.CopperFieldPage.postMessage(JSON.stringify({route,html}));}
        finally{setTimeout(()=>{rendering=false;},400);}
        return true;
    }
    async function has(actor,route=root){return !!(await record("pages",pageKey(actor,route)));}
    function notice(msg){if(win.FieldIdentity?.notice)win.FieldIdentity.notice(msg);else console.warn(msg);}
    doc.addEventListener("click",e=>{
        if(!doc.documentElement.hasAttribute("data-cr-field-local"))return;
        const link=e.target.closest?.("a[href]"); if(!link||link.hasAttribute("data-cr-planning-tab")||link.getAttribute("href").startsWith("#"))return;
        if(win.FieldReports?.handles(link.href))return;
        const route=routeOf(link.href);if(!route)return;
        e.preventDefault();e.stopImmediatePropagation();
        void render(route).then(ok=>{if(!ok)notice("Этот разрез ещё не был получен на планшет. Данные будут доступны после синхронизации.");}).catch(err=>notice(err.message));
    },true);
    win.FieldArchive={capture,prime,render,has,withoutPortraits,root,login,routeOf,scope};
    void db().catch(()=>{}); // Also migrate retired employees' pages on the login screen.
    void clearLegacyPhotoResponses().catch(()=>{});
})(window,document);
