/* A shared tablet displays only the current shift's portrait. The image lives in
   this document and is requested again after a cold start or a change of master. */
(function(win,doc){
    "use strict";
    if(win.FieldPortrait)return;
    const ROOT="/mining-master/tablet/", HOLDER=".dispatcher-active-badge .dispatcher-active-avatar";
    const network=win.fetch.bind(win), bitmap=/^data:image\/(?:png|jpeg|webp|gif|avif);base64,[A-Za-z0-9+/]+=*$/i;
    let session="",photo=null,attempt=null,inflight=null,suspended=false,painting=false,again=false,lastStatus="";

    // 0.1.44 stored thumbnails per employee in this separate database.
    try{
        const purge=indexedDB.deleteDatabase("cr-master-personal-portraits");
        purge.onsuccess=()=>diagnostic("purged","storage");
    }catch{/* Storage unavailable. */}

    function scope(a){
        return a?.verified && a.role==="mining_master" && a.key && a.generation &&
            Number.isSafeInteger(a.id) && a.id>0 && Number.isSafeInteger(a.access_id) && a.access_id>0 ?
            JSON.stringify([a.key,a.id,a.access_id]) : "";
    }
    function shiftKey(s){
        const a=s.actor,owner=scope(a),sh=s.shift;
        if(!owner || s.locked || !sh?.is_open || a.grant_cleared ||
            a.grant_until && !(Date.parse(a.grant_until)>Date.now()))return "";
        const shift=sh.local_shift_id || sh.server_id;
        return shift ? JSON.stringify([owner,a.generation,shift]) : "";
    }
    function bind(key){
        if(session===key)return;
        session=key;photo=null;attempt=null;inflight=null;lastStatus="";
    }
    function diagnostic(state,source){
        const stamp=state+" "+source;
        if(lastStatus===stamp)return;
        lastStatus=stamp;
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
    function display(a,data){
        const pages=[doc],frame=doc.querySelector('iframe[data-cr-planning-frame][data-ready="true"]');
        if(frame?.contentDocument)pages.push(frame.contentDocument);
        const initials=(a?.full_name||"").trim().split(/\s+/).slice(0,2).map(x=>x[0]||"").join("").toUpperCase();
        for(const page of pages){
            const holder=page.querySelector(".dispatcher-topbar "+HOLDER);
            if(!holder)continue;
            const img=holder.querySelector("img");
            if(!data){
                if(img || holder.textContent.trim()!==initials)holder.textContent=initials;
            }else if(img?.getAttribute("src")!==data || img.hidden){
                const tag=page.createElement("img");tag.alt="";tag.src=data;holder.replaceChildren(tag);
            }
        }
    }
    async function refreshDisplay(){
        if(doc.body?.dataset.appRoleCode!=="mining_master" || doc.body.dataset.observerMode==="true" ||
            doc.body.dataset.observerControl==="true" || suspended)return;
        const state=await win.FieldLedger.read();
        if(suspended)return;
        const key=shiftKey(state),a=state.actor,owner=doc.documentElement.dataset.crFieldPortraitOwner;
        bind(key);
        if(!key || (owner?owner!==scope(a):!ownHeader(doc,a))){
            display(null,null);return;
        }
        display(a,photo?.data);
    }
    function paint(){
        if(painting){again=true;return;}painting=true;again=false;
        Promise.resolve().then(refreshDisplay).catch(()=>{}).finally(()=>{painting=false;if(again)paint();});
    }
    async function thumbnail(data){
        if(typeof data!=="string"||data.length>11*1024*1024||!bitmap.test(data))throw Error("invalid_image");
        const img=new Image();img.src=data;await img.decode();
        if(!img.naturalWidth || !img.naturalHeight)throw Error("invalid_image");
        const scale=Math.min(1,160/Math.max(img.naturalWidth,img.naturalHeight));
        if(scale===1 && data.length<=180000)return data;
        const canvas=doc.createElement("canvas");canvas.width=Math.max(1,Math.round(img.naturalWidth*scale));
        canvas.height=Math.max(1,Math.round(img.naturalHeight*scale));
        canvas.getContext("2d").drawImage(img,0,0,canvas.width,canvas.height);return canvas.toDataURL("image/png");
    }
    async function fetchOwn(url,media=false){
        const ctl=new AbortController(),timer=setTimeout(()=>ctl.abort(),9000);
        try{
            const r=await network(url,{credentials:"same-origin",cache:"no-store",redirect:"error",signal:ctl.signal,
                headers:media?{"X-Copper-Field-Portrait":"1"}:{}});
            if(!r.ok||new URL(r.url).origin!==location.origin)throw Error("personal_resource_unavailable");
            return media ? await r.blob() : await r.text();
        }finally{clearTimeout(timer);}
    }
    async function cookieMatches(a){
        const c=await win.FieldTransport.identify();
        return !c.legacy && c.snapshot?.shift?.is_open && scope(c.actor)===scope(a) && c.actor.generation===a.generation;
    }
    async function fromHeader(a){
        if(!await cookieMatches(a))throw Error("needs_session");
        const base=new URL(ROOT,location.origin).href;
        const page=new DOMParser().parseFromString(await fetchOwn(base),"text/html"),holder=ownHeader(page,a);
        if(!holder)throw Error("unconfirmed_header");
        const address=holder.querySelector("img")?.getAttribute("src");
        let result=null;
        if(address){
            if(address.startsWith("data:"))result=await thumbnail(address);
            else{
                const url=new URL(address,base);
                if(url.origin!==location.origin || url.username || url.password || !url.pathname.startsWith("/media/employee_photos/"))
                    throw Error("invalid_media_path");
                const b=await fetchOwn(url.href,true);
                if(!b.type.startsWith("image/")||!b.size||b.size>8*1024*1024)throw Error("invalid_image");
                const data=await new Promise((resolve,reject)=>{
                    const r=new FileReader();r.onload=()=>resolve(r.result);r.onerror=()=>reject(r.error);r.readAsDataURL(b);
                });
                result=await thumbnail(data);
            }
        }
        if(!await cookieMatches(a))throw Error("needs_session");
        return result;
    }
    async function load(task,a,revision,via){
        const source=revision===undefined?"header":"signed";
        try{
            let data=null;
            if(revision===undefined){
                // Old servers only expose private photos with an active personal web session.
                if(via==="device_ticket")throw Error("needs_session");
                data=await fromHeader(a);
            }else if(revision){
                const p=await win.FieldTransport.readPortrait(a);
                if(p.employee_id!==a.id || p.access_id!==a.access_id || p.revision!==revision || !p.data)throw Error("portrait_changed");
                data=await thumbnail(p.data);
            }
            const state=await win.FieldLedger.read();
            if(suspended || inflight!==task || shiftKey(state)!==task.key)return;
            photo={revision,data,checked:Date.now()};paint();diagnostic(data?"shown":"source_missing",source);
        }catch(e){
            if(inflight===task)diagnostic(e.message==="needs_session"||e.code==="auth"?"needs_session":"retry",source);
        }
    }
    async function observe(actor,snapshot,via){
        if(suspended || !snapshot?.shift?.is_open || !scope(actor) || scope(snapshot.actor)!==scope(actor) ||
            snapshot.actor.generation!==actor.generation)return;
        const state=await win.FieldLedger.read(),key=shiftKey(state);
        if(suspended || !key || scope(state.actor)!==scope(actor) || state.actor.generation!==actor.generation ||
            (snapshot.shift.server_id && state.shift.server_id && snapshot.shift.server_id!==state.shift.server_id) ||
            (snapshot.shift.local_shift_id && state.shift.local_shift_id && snapshot.shift.local_shift_id!==state.shift.local_shift_id))return;
        bind(key);
        const raw=snapshot.portrait_revision;
        const revision=raw==="" || typeof raw==="string"&&/^[0-9a-f]{64}$/.test(raw)?raw:undefined;
        const stamp=JSON.stringify([key,revision,via]);
        if(inflight?.stamp===stamp)return inflight.promise;
        if(inflight)inflight=null;
        if(photo && photo.revision===revision && (revision!==undefined || Date.now()-photo.checked<300000))return;
        if(attempt?.stamp===stamp && Date.now()-attempt.at<30000)return;
        attempt={stamp,at:Date.now()};
        const task={key,stamp};inflight=task;
        task.promise=load(task,actor,revision,via).finally(()=>{if(inflight===task)inflight=null;});
        return task.promise;
    }
    function watch(){
        const relevant=n=>n.nodeType===1 && (n.closest(HOLDER) || n.matches("iframe[data-cr-planning-frame]") || n.querySelector(HOLDER));
        new MutationObserver(list=>{
            if(list.some(m=>m.type==="attributes"?relevant(m.target):m.target.closest?.(HOLDER)||[...m.addedNodes].some(relevant)))paint();
        }).observe(doc.documentElement,{subtree:true,childList:true,attributes:true,attributeFilter:["src","data-ready"]});
        paint();
    }
    win.FieldPortrait={paint,observe,ownHeader};
    if(doc.readyState==="loading")doc.addEventListener("DOMContentLoaded",watch,{once:true});else watch();
    win.addEventListener("fieldledgerchange",paint);
    win.addEventListener("online",()=>{attempt=null;win.FieldTransport?.wake?.();});
    win.addEventListener("pagehide",()=>{suspended=true;bind("");display(null,null);});
    win.addEventListener("pageshow",e=>{if(e.persisted){suspended=false;paint();win.FieldTransport?.wake?.();}});
})(window,document);
