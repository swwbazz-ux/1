/* The tablet is shared. Keep thumbnails with the employee/access identity, never with public static assets. */
(function(win,doc){
    "use strict";
    if(win.FieldPortrait)return;
    const ROOT="/mining-master/tablet/", HOLDER=".dispatcher-active-badge .dispatcher-active-avatar";
    const network=win.fetch.bind(win), MEDIA="/media/employee_photos/";
    const bitmap=/^data:image\/(?:png|jpeg|webp|gif|avif);base64,[A-Za-z0-9+/]+=*$/i;
    let database,bound="",stored=null,scheduled=false,repaint=false,job=null,desired=null,lastAttempt=null,lastDiagnostic="";
    const migrated=new Set(),attemptedReports=new Set();

    function scope(a){
        return a?.verified && a.role==="mining_master" && a.key && a.generation &&
            Number.isSafeInteger(a.id) && a.id>0 && Number.isSafeInteger(a.access_id) && a.access_id>0 ?
            JSON.stringify([a.key,a.id,a.access_id]) : "";
    }
    async function current(a){
        const s=await win.FieldLedger.read();
        return !s.locked && !!s.shift?.is_open && scope(a)!=="" && scope(a)===scope(s.actor) &&
            a.generation===s.actor.generation && !(s.actor.grant_until && Date.parse(s.actor.grant_until)<=Date.now());
    }
    function db(){
        return database||=(new Promise((resolve,reject)=>{
            const req=indexedDB.open("cr-master-personal-portraits",1);
            req.onupgradeneeded=()=>req.result.createObjectStore("portraits");
            req.onsuccess=()=>resolve(req.result);req.onerror=()=>reject(req.error);
        })).catch(e=>{database=null;throw e;});
    }
    async function row(a,value,onlyMissing=false){
        const key=scope(a),conn=await db();
        return new Promise((resolve,reject)=>{
            const tx=conn.transaction("portraits",value?"readwrite":"readonly"),store=tx.objectStore("portraits"),req=store.get(key);
            let result;req.onsuccess=()=>{
                result=req.result||null;
                if(value && (!result || !onlyMissing && value.at>=result.at)){
                    result={...value,key};store.put(result,key);
                }
            };
            tx.oncomplete=()=>resolve(result);tx.onerror=tx.onabort=()=>reject(tx.error);
        });
    }
    function diagnostic(state,source){
        const stamp=JSON.stringify([bound,state,source]);if(stamp===lastDiagnostic)return;
        lastDiagnostic=stamp;
        try{win.CopperFieldPage?.postMessage(JSON.stringify({op:"portrait_status",state,source}));}catch{/* Page closed. */}
    }
    function ownHeader(page,a){
        if(!scope(a)||page.body?.dataset.appRoleCode!=="mining_master"||
            page.body.dataset.observerMode==="true"||page.body.dataset.observerControl==="true")return null;
        const board=page.querySelector(".dispatcher-board"),header=page.querySelector(".dispatcher-topbar");
        const holder=header?.querySelector(".dispatcher-active-badge:not(.is-muted) .dispatcher-active-avatar");
        const end=header?.querySelector('button[name=action][value=end_shift][data-confirm-person]');
        if(!holder || (board && board.dataset.dispatcherOwnShiftOpen!=="true") ||
            !end || !a.full_name || end.dataset.confirmPerson.trim()!==a.full_name.trim())return null;
        return holder;
    }
    const readBlob=b=>new Promise((resolve,reject)=>{
        const r=new FileReader();r.onload=()=>resolve(r.result);r.onerror=()=>reject(r.error);r.readAsDataURL(b);
    });
    async function thumbnail(data){
        if(typeof data!=="string"||data.length>11*1024*1024||!bitmap.test(data))throw Error("Invalid portrait data");
        const img=new Image();img.src=data;await img.decode();
        if(!img.naturalWidth || !img.naturalHeight)throw Error("Empty portrait");
        const scale=Math.min(1,160/Math.max(img.naturalWidth,img.naturalHeight));
        if(scale===1 && data.length<=180000)return data;
        const canvas=doc.createElement("canvas");
        canvas.width=Math.max(1,Math.round(img.naturalWidth*scale));canvas.height=Math.max(1,Math.round(img.naturalHeight*scale));
        canvas.getContext("2d").drawImage(img,0,0,canvas.width,canvas.height);return canvas.toDataURL("image/png");
    }
    async function fetchOwn(url){
        const ctl=new AbortController(),timer=setTimeout(()=>ctl.abort(),9000);
        try{
            const r=await network(url,{credentials:"same-origin",cache:"no-store",redirect:"error",signal:ctl.signal});
            if(!r.ok||new URL(r.url).origin!==location.origin)throw Error("Personal resource unavailable");
            return r;
        }finally{clearTimeout(timer);}
    }
    function mediaUrl(address,base){
        const url=new URL(address,base);
        if(url.origin!==location.origin || url.username || url.password || !url.pathname.startsWith(MEDIA))throw Error("Invalid personal media path");
        return url;
    }
    async function image(address,base=location.href,onlyCached=false){
        if(typeof address!=="string"||!address)throw Error("No portrait source");
        if(address.startsWith("data:"))return thumbnail(address);
        const url=mediaUrl(address,base);
        const r=onlyCached ? await network(url.href,{mode:"same-origin",credentials:"same-origin",cache:"only-if-cached",redirect:"error"}) :
            await fetchOwn(url.href);
        if(!r.ok || r.url!==url.href)throw Error("Personal photo unavailable");
        const b=await r.blob();
        if(!/^image\/(?:png|jpeg|webp|gif|avif)$/i.test(b.type)||!b.size||b.size>8*1024*1024)throw Error("Invalid photo response");
        return thumbnail(await readBlob(b));
    }
    async function archived(page,a){
        const holder=ownHeader(page,a),url=holder?.querySelector("img")?.getAttribute("src");
        if(!url?.startsWith("data:image/"))return null;
        try{return await thumbnail(url);}catch{return null;}
    }
    function documents(){
        const pages=[doc];
        const frame=doc.querySelector('iframe[data-cr-planning-frame][data-ready="true"]');
        if(frame?.contentDocument)pages.push(frame.contentDocument);
        return pages;
    }
    function display(a,portrait){
        const initials=a?(a.full_name||"").trim().split(/\s+/).slice(0,2).map(x=>x[0]||"").join("").toUpperCase():"";
        for(const page of documents()){
            const holder=page.querySelector(".dispatcher-topbar "+HOLDER);
            if(!holder)continue;
            const photo=holder.querySelector("img");
            if(portrait?.data){
                if(photo?.getAttribute("src")===portrait.data && !photo.hidden)continue;
                const img=page.createElement("img");img.alt="";img.src=portrait.data;holder.replaceChildren(img);
            }else if(portrait || photo){
                if(photo || holder.textContent.trim()!==initials)holder.textContent=initials;
            }
        }
    }
    async function refreshDisplay(){
        if(doc.body?.dataset.appRoleCode!=="mining_master" || doc.body.dataset.observerMode==="true" || doc.body.dataset.observerControl==="true" ||
            !doc.querySelector(".dispatcher-topbar "+HOLDER))return;
        const s=await win.FieldLedger.read(), a=s.actor, key=scope(a);
        if(!key || !await current(a)){
            if(bound || doc.documentElement.hasAttribute("data-cr-field-local"))display(null,{data:null});
            bound="";stored=null;return;
        }
        if(bound && bound!==key){display(a,{data:null});bound="";stored=null;}
        const owner=doc.documentElement.dataset.crFieldPortraitOwner;
        if(owner?owner!==key:!ownHeader(doc,a)){
            if(doc.documentElement.hasAttribute("data-cr-field-local"))display(a,{data:null});return;
        }
        bound=key;
        const entry=stored?.key===key?stored:await row(a);
        if(!await current(a))return;
        if(entry){stored=entry;display(a,entry);diagnostic(entry.data?"shown":"source_missing",entry.source);return;}
        // A scoped 0.1.43 archive can restore a photo even when its old web cookie has expired.
        if(!migrated.has(key) && win.FieldArchive){
            migrated.add(key);
            const at=Date.now(),data=await win.FieldArchive.findPortrait(a,archived);
            if(data && await current(a)){
                stored=await row(a,{data,revision:null,source:"archive",at},true);
                if(await current(a)){display(a,stored);diagnostic(stored.data?"shown":"source_missing",stored.source);}
                return;
            }
        }
        if(doc.documentElement.hasAttribute("data-cr-field-local"))display(a,null);
    }
    function paint(){
        if(scheduled){repaint=true;return;}scheduled=true;repaint=false;
        Promise.resolve().then(refreshDisplay).catch(()=>{}).finally(()=>{scheduled=false;if(repaint)paint();});
    }
    async function cookieMatches(a){
        const c=await win.FieldTransport.identify();
        return !c.legacy && c.snapshot?.shift?.is_open && scope(c.actor)===scope(a) && c.actor.generation===a.generation;
    }
    async function fromHeader(a,onlyCached=false){
        const base=new URL(ROOT,location.origin).href;
        const response=onlyCached ? await win.FieldTransport.readReport(ROOT+"dispatcher/shift-log/",a) : await fetchOwn(base);
        const page=new DOMParser().parseFromString(await response.text(),"text/html");
        const holder=ownHeader(page,a);
        if(!holder)throw Error("no_authoritative_header");
        const img=holder.querySelector("img");
        const data=img?await image(img.getAttribute("src"),base,onlyCached):null;
        if(!onlyCached && !await cookieMatches(a))throw Error("needs_session");
        return data;
    }
    function firstReportAttempt(a){
        const k="cr-master-portrait-report:"+JSON.stringify([scope(a),a.generation]);
        if(attemptedReports.has(k))return false;
        attemptedReports.add(k);
        try{if(sessionStorage.getItem(k))return false;sessionStorage.setItem(k,"1");}catch{/* Private session storage disabled. */}
        return true;
    }
    async function refresh(req){
        const {actor:a,revision,via}=req;
        if(!await current(a))return;
        const prior=await row(a);
        if(revision!==undefined && prior?.revision===revision && (revision?!!prior.data:!prior.data)){stored=prior;paint();return;}
        const at=Date.now(),key=JSON.stringify([scope(a),a.generation,revision,via]);
        if(lastAttempt?.key===key && at<lastAttempt.next)return;
        lastAttempt={key,next:at+30000};
        let data,source;
        if(revision===""){data=null;source="signed";}
        else if(revision!==undefined){
            const result=await win.FieldTransport.readPortrait(a);
            // A changed revision is picked up by the next signed snapshot.
            if(result.employee_id!==a.id || result.access_id!==a.access_id || result.revision!==revision || !result.data)throw Error("portrait_changed");
            data=await thumbnail(result.data);source="signed";
        }else{
            let hasCookie=false;
            if(via!=="device_ticket"){
                try{hasCookie=await cookieMatches(a);}catch(e){if(e.code!=="auth")throw e;}
            }
            if(hasCookie){data=await fromHeader(a);source="header";}
            else{
                // A signed report proves which exact media URL belongs to this master. The
                // expired cookie must never cause a network GET of private media.
                lastAttempt.next=at+300000;
                if(prior?.data){stored=prior;paint();return;}
                if(!firstReportAttempt(a))throw Error("needs_session");
                try{data=await fromHeader(a,true);source="archive";}
                catch{throw Error("needs_session");}
            }
        }
        if(desired!==req || !await current(a))return;
        stored=await row(a,{data,source,revision:revision??null,at});
        if(!await current(a))return;
        lastAttempt.next=at+300000;paint();
        diagnostic(stored.data?"shown":"source_missing",stored.source);
    }
    function observe(actor,snapshot,via){
        if(!snapshot?.shift?.is_open || !scope(actor) || scope(snapshot.actor)!==scope(actor) || snapshot.actor.generation!==actor.generation)return;
        const raw=snapshot.portrait_revision;
        const revision=raw==="" || typeof raw==="string"&&/^[0-9a-f]{64}$/.test(raw)?raw:undefined;
        if(!desired||scope(actor)!==scope(desired.actor)||actor.generation!==desired.actor.generation||revision!==desired.revision||via!==desired.via)
            desired={actor,revision,via};
        if(job)return job;
        const req=desired;
        job=refresh(req).catch(e=>{
            if(desired===req)diagnostic(e.message==="needs_session"||e.code==="auth"?"needs_session":"retry",revision===undefined?"header":"signed");
        }).finally(()=>{job=null;if(desired!==req)observe(desired.actor,{actor:desired.actor,shift:{is_open:true},portrait_revision:desired.revision},desired.via);});
        return job;
    }
    function watch(){
        const relevant=n=>n.nodeType===1 && (n.closest(HOLDER) || n.matches("iframe[data-cr-planning-frame]") || n.querySelector(HOLDER));
        new MutationObserver(list=>{
            if(list.some(m=>m.type==="attributes"?relevant(m.target):m.target.closest?.(HOLDER)||[...m.addedNodes].some(relevant)))paint();
        }).observe(doc.documentElement,{subtree:true,childList:true,attributes:true,attributeFilter:["src","data-ready"]});
        paint();
    }
    win.FieldPortrait={paint,observe,ownHeader,image};
    if(doc.readyState==="loading")doc.addEventListener("DOMContentLoaded",watch,{once:true});else watch();
    win.addEventListener("fieldledgerchange",paint);
    win.addEventListener("online",()=>{lastAttempt=null;});
})(window,document);
