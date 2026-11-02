/* Reports are scoped by verified employee, access generation, shift and the exact
   original query string. A pending GET can never become an operational mutation. */
(function(win,doc){
    "use strict";
    if(win.top!==win||win.FieldReports||location.origin!=="https://mining-master.driverform.ru")return;
    const ROOT="/mining-master/tablet/";
    const pages=new Set(["dispatcher/reports/","dispatcher/shift-log/","dispatcher/management/","dispatcher/mining-volumes/","dispatcher/transport/","dispatcher/downtimes/","dispatcher/shift-reports/trucks/","dispatcher/shift-reports/excavation/","dispatcher/shift-reports/hourly/","dispatcher/shift-reports/hourly-grid/","reports/shift-analytics/","reports/volume/","reports/customer-daily/","reports/templates/","reports/downtimes/"].map(s=>ROOT+s));
    const exports=new Set([...pages].filter(s=>!s.endsWith("hourly-grid/")&&!s.endsWith("templates/")).map(s=>s+"export/"));
    const MAX=50*1024*1024, fetching=new Map(), waiters=new Map(), writers=new Map();
    let database,bridgeBound=false,pumping=null,lastWrite=Promise.resolve();
    const ledger=()=>win.FieldLedger;
    function route(address){
        try{const u=new URL(address,location.href);
            if(u.origin!==location.origin || u.username || u.password || u.search.length>600 || !pages.has(u.pathname)&&!exports.has(u.pathname))return "";
            // Preserve repeated values: their order can be meaningful to the report server.
            u.searchParams.sort();return u.pathname+u.search;
        }catch{return "";}
    }
    function isExport(address){return exports.has(address.split("?")[0]);}
    function identity(s){
        const a=s.actor;
        if(s.locked||!a?.verified||a.role!=="mining_master"||!s.shift?.is_open||!a.generation||
            a.grant_until&&Date.parse(a.grant_until)<=Date.now())return null;
        return {key:a.key,access:a.access_id,generation:a.generation,shift:s.shift.local_shift_id||String(s.shift.server_id||"")};
    }
    function key(scope,path){return JSON.stringify([scope.key,scope.access,scope.generation,scope.shift,path]);}
    function same(a,b){return a&&b&&a.key===b.key&&a.access===b.access&&a.generation===b.generation&&a.shift===b.shift;}
    function notice(msg){if(win.FieldIdentity?.notice)win.FieldIdentity.notice(msg);else win.dispatchEvent(new CustomEvent("fieldreportnotice",{detail:msg}));}
    function db(){return database||=(new Promise((resolve,reject)=>{
        const req=indexedDB.open("cr-master-scoped-reports",2);
        req.onupgradeneeded=()=>{for(const name of ["files","pages","requests","sizes"])if(!req.result.objectStoreNames.contains(name))req.result.createObjectStore(name,{keyPath:"key"});};
        req.onsuccess=()=>resolve(req.result);req.onerror=()=>reject(req.error);req.onblocked=()=>reject(Error("Хранилище отчётов занято."));
    })).catch(e=>{database=null;throw e;});}
    async function idb(name,op,arg){
        if(op==="put"&&name!=="requests")return boundedPut(name,arg);
        const conn=await db();return new Promise((resolve,reject)=>{
            const write=op==="put"||op==="delete",tx=conn.transaction(name,write?"readwrite":"readonly"),shelf=tx.objectStore(name),req=arg===undefined?shelf[op]():shelf[op](arg);
            tx.oncomplete=()=>resolve(req.result);tx.onerror=tx.onabort=()=>reject(tx.error||Error("Не удалось сохранить отчёт на планшете."));
        });
    }
    // Report copies must not exhaust storage needed by the shift journal.
    // Evict only completed cached views/files; queued requests are independent.
    async function boundedPut(name,entry){
        const conn=await db(),cost=entry.blob?.size??(entry.html?.length||0)*2;
        return new Promise((resolve,reject)=>{
            const tx=conn.transaction(["pages","files","sizes"],"readwrite"),sizes=tx.objectStore("sizes"),all=sizes.getAll();
            all.onsuccess=()=>{
                const records=all.result.filter(v=>v.key!==entry.key).sort((a,b)=>a.at-b.at);
                let bytes=records.reduce((n,v)=>n+v.cost,cost);
                while(records.length && (records.length>=48 || bytes>64*1024*1024)){
                    const drop=records.shift();tx.objectStore(drop.name).delete(drop.key);sizes.delete(drop.key);bytes-=drop.cost;
                }
                sizes.put({key:entry.key,name,at:entry.at,cost});
                tx.objectStore(name).put(entry);
            };
            tx.oncomplete=()=>resolve(entry.key);tx.onabort=tx.onerror=()=>reject(tx.error||Error("Не удалось сохранить отчёт."));
        });
    }
    async function scopeNow(){return identity(await ledger().read());}
    async function scopeId(actor){
        const current=await scopeNow();
        return current && current.key===actor?.key && current.access===actor.access_id && current.generation===actor.generation ? key(current,""):null;
    }
    async function owner(scope){
        const state=await ledger().read();
        if(!same(identity(state),scope))throw Error("Нужен личный вход владельца отчёта.");
        return state.actor;
    }
    async function hasOwnCookie(scope){
        const actor=await owner(scope);
        try{return !!await win.FieldIdentity?.confirmScope?.(actor);}catch{return false;}
    }
    async function queued(scope,path){
        const k=key(scope,path),old=await idb("requests","get",k);
        if(!old)await idb("requests","put",{key:k,scope,path,id:crypto.randomUUID(),at:Date.now()});
        return old||await idb("requests","get",k);
    }
    async function remember(page,actor){
        const path=route(page?.route||"");
        if(!path||isExport(path)||!page?.html||!actor?.verified||page.scope!==win.FieldArchive?.scope(actor))return false;
        const scope=await scopeNow();
        if(!scope||scope.key!==actor.key||scope.generation!==actor.generation||page.report_scope!==key(scope,""))return false;
        await idb("pages","put",{key:key(scope,path),scope,path,html:page.html,at:page.at});
        if(path===route(location.href))primeFile(page.html,scope,path);
        return true;
    }
    const stamp=at=>new Date(at).toLocaleString("ru-RU");
    function archivedDocument(html,title){
        const p=new DOMParser().parseFromString(html,"text/html");
        let tag=p.querySelector("[data-field-report-snapshot]");
        if(!tag){tag=p.createElement("p");tag.dataset.fieldReportSnapshot="true";tag.setAttribute("role","status");
            const host=p.querySelector("main")||p.body;host.prepend(tag);}
        tag.textContent=title+" ";
        const button=p.createElement("button");button.type="button";button.dataset.fieldReportRefresh="";button.textContent="Обновить";tag.append(button);
        return "<!doctype html>"+p.documentElement.outerHTML;
    }
    async function render(page,scope){
        if(!same(await scopeNow(),scope) || !win.CopperFieldPage?.postMessage)return false;
        win.CopperFieldPage.postMessage(JSON.stringify({route:page.path,html:archivedDocument(page.html,"Отчёт на планшете · данные получены "+stamp(page.at))}));return true;
    }
    // Do not assume a particular server filename: preserve the response's real export format.
    function filename(res,path){
        const d=res.headers.get("content-disposition")||"";
        let n=d.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
        if(n){try{n=decodeURIComponent(n);}catch{n="";}}
        if(!n)n=d.match(/filename="?([^";]+)"?/i)?.[1]||"";
        if(!/\.(xlsx|xls|csv|pdf)$/i.test(n)){
            const ct=res.headers.get("content-type")||"";
            const ext=/spreadsheetml/i.test(ct)?"xlsx":/ms-excel/i.test(ct)?"xls":/pdf/i.test(ct)?"pdf":/csv/i.test(ct)?"csv":"";
            if(!ext)throw Error("Сервер не вернул файл отчёта.");
            n="Отчёт-"+path.split("/").filter(Boolean).slice(-2,-1)[0]+"."+ext;
        }
        return n.replace(/[^\p{L}\p{N}._ -]/gu,"_").slice(-80);
    }
    async function readFile(res){
        const expected=Number(res.headers.get("content-length"))||0;
        if(expected>MAX)throw Error("Файл превышает ограничение планшета.");
        const reader=res.body?.getReader();if(!reader)throw Error("Недоступно чтение файла на устройстве.");
        let total=0;const pieces=[];
        for(;;){
            const {value,done}=await reader.read();if(done)break;total+=value.length;
            if(total>MAX){await reader.cancel();throw Error("Файл превышает ограничение планшета.");}
            pieces.push(value);
        }
        if(!total)throw Error("Получен пустой отчёт.");
        return new Blob(pieces,{type:res.headers.get("content-type")||"application/octet-stream"});
    }
    function isDocument(name,buf){
        const b=new Uint8Array(buf);
        if(/\.xlsx$/i.test(name))return b[0]===80&&b[1]===75&&b[2]===3&&b[3]===4;
        if(/\.xls$/i.test(name))return b[0]===208&&b[1]===207&&b[2]===17&&b[3]===224;
        if(/\.pdf$/i.test(name))return String.fromCharCode(...b.slice(0,5))==="%PDF-";
        return /\.csv$/i.test(name)&&!/^\s*</.test(new TextDecoder().decode(b.slice(0,50)));
    }
    async function fetchFile(scope,path){
        const actor=await owner(scope),signed=!await hasOwnCookie(scope);
        const fetcher=win.FieldTransport?.wireFetch||win.fetch.bind(win);
        const res=signed?await win.FieldTransport.readReport(path,actor):
            await fetcher(path,{method:"GET",credentials:"same-origin",cache:"no-store",signal:AbortSignal.timeout(20000)});
        const landed=new URL(res.url,location.origin);
        if(!res.ok||!signed&&route(landed.href)!==path||/text\/html/i.test(res.headers.get("content-type")||""))throw Error("Система не предоставила этот файл.");
        const name=filename(res,path),blob=await readFile(res),buf=await blob.arrayBuffer();
        if(!isDocument(name,buf))throw Error("Получен повреждённый файл отчёта.");
        // A login during a slow HTTP download must never attach the new employee's file to the old one.
        await owner(scope);
        if(!signed && !await hasOwnCookie(scope))throw Error("Изменился владелец файла отчёта.");
        const sha=Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",buf)),b=>b.toString(16).padStart(2,"0")).join("");
        const entry={key:key(scope,path),scope,path,id:crypto.randomUUID(),at:Date.now(),blob,sha,name};
        await idb("files","put",entry);return entry;
    }
    async function fetchEntry(scope,path){
        const keyName=key(scope,path);
        if(!fetching.has(keyName))fetching.set(keyName,(async()=>{
            if(isExport(path))return fetchFile(scope,path);
            const state=await ledger().read(),signedReport=!await hasOwnCookie(scope);
            const raw=await win.FieldArchive.capture(path,state.actor,{force:true,signedReport});
            if(!raw||!await remember(raw,state.actor))throw Error("Не удалось подтвердить отчёт сотрудника.");
            return idb("pages","get",keyName);
        })().finally(()=>fetching.delete(keyName)));
        return fetching.get(keyName);
    }
    function primeFile(html,scope,pagePath){
        if(!navigator.onLine)return;
        const p=new DOMParser().parseFromString(html,"text/html"),link=[...p.querySelectorAll("a[href]")].map(a=>route(new URL(a.getAttribute("href"),location.origin+pagePath))).find(u=>u&&isExport(u));
        // Cache the real Excel paired with the viewed report. Do not download every period of every report.
        if(link)idb("files","get",key(scope,link)).then(v=>{if(!v)return fetchEntry(scope,link);}).catch(()=>{});
    }
    function bridgeCall(data){
        const bridge=win.CopperOfflineReport;
        if(!bridge?.postMessage)throw Error("Системное сохранение файлов недоступно. Отчёт остался на планшете.");
        if(!bridgeBound){bridgeBound=true;bridge.onmessage=e=>{
            let res;try{res=JSON.parse(e.data);}catch{return;}
            const job=waiters.get(res.id+":"+res.serial);if(job){if(res.ok)job.resolve();else job.reject(Error("Не удалось сохранить файл. Сохранённый отчёт доступен для повтора."));}
        };}
        let timer;
        return new Promise((resolve,reject)=>{
            const id=data.id+":"+data.serial;timer=setTimeout(()=>reject(Error("Планшет не подтвердил запись отчёта.")),12000);
            waiters.set(id,{resolve,reject});try{bridge.postMessage(JSON.stringify(data));}catch(e){reject(e);}
        }).finally(()=>{clearTimeout(timer);waiters.delete(data.id+":"+data.serial);});
    }
    async function writeFile(entry,scope){
        if(writers.has(entry.id))return writers.get(entry.id);
        const job=lastWrite.catch(()=>{}).then(async()=>{
            if(!same(await scopeNow(),scope))throw Error("Изменился сотрудник планшета.");
            const size=entry.blob.size,id=entry.id;let serial=0;
            await bridgeCall({id,serial:serial++,op:"begin",name:entry.name,size,at:entry.at,sha256:entry.sha});
            for(let offset=0;offset<size;offset+=49152){
                if(!same(await scopeNow(),scope))throw Error("Сотрудник сменился до завершения записи.");
                const bytes=new Uint8Array(await entry.blob.slice(offset,offset+49152).arrayBuffer());
                await bridgeCall({id,serial:serial++,op:"chunk",offset,bytes:btoa(String.fromCharCode(...bytes))});
            }
            if(!same(await scopeNow(),scope))throw Error("Сотрудник сменился до завершения записи.");
            await bridgeCall({id,serial:serial++,op:"commit"});notice("Файл сохранён в отчётах планшета. Данные на "+stamp(entry.at)+".");
        }).finally(()=>writers.delete(entry.id));
        lastWrite=job;
        writers.set(entry.id,job);return job;
    }
    async function open(address,{background=false,force=false}={}){
        const path=route(address);if(!path)return false;
        const scope=await scopeNow();if(!scope){if(!background)notice("Для доступа к отчётам войдите в свою смену.");return false;}
        const shelf=isExport(path)?"files":"pages",k=key(scope,path);
        const cached=await idb(shelf,"get",k);
        // As soon as the user requests an unseen report, the intent is durable before any network request.
        await queued(scope,path);
        if(cached&&!background&&!force){
            if(isExport(path))await writeFile(cached,scope);
            else await render(cached,scope);
            if(isExport(path))await idb("requests","delete",k);
            if(navigator.onLine){
                // Returning to last week's known filter should not block on a broken Wi-Fi.
                void fetchEntry(scope,path).then(()=>idb("requests","delete",k)).catch(()=>{});
            }
            return true;
        }
        let entry=cached,fresh=false;
        if(!entry||navigator.onLine){
            try{entry=await fetchEntry(scope,path);fresh=true;}catch{}
        }
        if(background&&!fresh)return false;
        if(!entry || !same(await scopeNow(),scope)){
            if(!background)notice("Запрос отчёта сохранён. Этот период будет получен при восстановлении связи.");
            return false;
        }
        if(isExport(path)){await writeFile(entry,scope);if(entry!==cached || !cached || !navigator.onLine)await idb("requests","delete",k);}
        else{
            if(!background)await render(entry,scope);
            if(entry!==cached || !cached)await idb("requests","delete",k);
            else if(!background)notice("Показаны сохранённые данные на "+stamp(entry.at)+".");
        }
        return true;
    }
    async function missing(address){
        const path=route(address),s=await scopeNow();if(!path||!s)return false;
        await queued(s,path);notice("Запрос сохранён. Отчёт будет получен при восстановлении связи.");wake();return false;
    }
    function wake(){
        if(!pumping && navigator.onLine)pumping=(async()=>{
            const scope=await scopeNow();if(!scope)return;
            const reqs=(await idb("requests","getAll")).filter(q=>same(q.scope,scope)).sort((a,b)=>a.at-b.at);
            for(const q of reqs.slice(0,4)){
                try{await open(q.path,{background:true});}catch{/* A different requested period may still be available. */}
            }
        })().catch(()=>{}).finally(()=>{pumping=null;});
    }
    function submit(form){
        const path=route(form.action||location.href);if(!path || isExport(path) || (form.method||"get").toUpperCase()!=="GET")return false;
        const u=new URL(path,location.origin);u.search=new URLSearchParams([...new FormData(form)].filter(([k,v])=>k!=="csrfmiddlewaretoken"&&typeof v==="string")).toString();
        if(!route(u))return false;
        open(u.href).catch(e=>notice(e.message));return true;
    }
    doc.addEventListener("click",e=>{
        if(e.defaultPrevented||e.button!==0||e.ctrlKey||e.metaKey||e.shiftKey||e.altKey)return;
        if(e.target.closest?.("[data-field-report-refresh]")){e.preventDefault();open(location.href,{force:true}).catch(err=>notice(err.message));return;}
        const link=e.target.closest?.("a[href]");if(!link||link.getAttribute("href").startsWith("#")||!route(link.href))return;
        e.preventDefault();e.stopImmediatePropagation();open(link.href).catch(err=>notice(err.message));
    },true);
    doc.addEventListener("submit",e=>{if(submit(e.target)){e.preventDefault();e.stopImmediatePropagation();}},true);
    const originalSubmit=HTMLFormElement.prototype.submit;
    HTMLFormElement.prototype.submit=function(){if(!submit(this))return originalSubmit.call(this);};
    win.addEventListener("online",wake);win.addEventListener("native-resume",wake);
    setInterval(()=>{if(!doc.hidden)wake();},30000);
    win.FieldReports={handles:url=>!!route(url),open,remember,missing,wake,scopeId};
    setTimeout(wake,100);
})(window,document);
