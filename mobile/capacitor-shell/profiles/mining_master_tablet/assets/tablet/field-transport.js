/* The network never owns the local journal. Only a receipt identifying the event and
   the original employee may acknowledge it. Refresh can be started without a UI. */
(function(win,doc){
    "use strict";
    if(win.FieldTransport)return;
    const PROTOCOL="master-field/2",ENDPOINT="/mining-master/tablet/field-sync/",ROLE="mining_master";
    const ledger=()=>win.FieldLedger;
    const wireFetch=win.fetch.bind(win);
    const status={connected:false,protocol:null,message:"Ожидание обмена",last_ack_at:null,last_snapshot_at:null};
    let pending=null,timer,wasSupported=false,uiBusy=()=>false;
    const fail=(code,message)=>Object.assign(new Error(message),{code});
    function update(fields){
        if(Object.entries(fields).every(([name,value])=>status[name]===value))return;
        Object.assign(status,fields);win.dispatchEvent(new CustomEvent("fieldtransportchange",{detail:{...status}}));
    }
    async function request(path,init={},timeoutMs=7500){
        const ctl=new AbortController(), timeout=setTimeout(()=>ctl.abort(),timeoutMs);
        try{
            const res=await wireFetch(path,{credentials:"same-origin",cache:"no-store",...init,signal:ctl.signal});
            if(res.url && new URL(res.url,location.href).origin!==location.origin)throw fail("untrusted","Неверный адрес системы.");
            return res;
        }catch(e){
            if(e.code==="untrusted")throw e;
            throw fail("network","Нет связи с сервером. Действия остаются на планшете.");
        }finally{clearTimeout(timeout);}
    }
    function csrf(){
        const token=doc.cookie.split(";").map(x=>x.trim()).find(x=>x.startsWith("csrftoken="))?.slice(10);
        return token||doc.querySelector('[name="csrfmiddlewaretoken"]')?.value||"";
    }
    async function challenge(deviceId){
        const response=await request(ENDPOINT+"?"+new URLSearchParams({protocol:PROTOCOL,device_id:deviceId,challenge:"1"}));
        if(!response.ok||!jsonResponse(response))throw fail("auth","Не удалось подтвердить запрос планшета.");
        const result=await response.json();
        if(result.protocol!==PROTOCOL || !result.csrf_token || !result.challenge || !Number.isFinite(Date.parse(result.server_time)))
            throw fail("auth","Сервер не поддерживает проверку подписанного запроса.");
        return result;
    }
    async function csrfForPost(){
        const cookie=doc.cookie.split(";").map(x=>x.trim()).find(x=>x.startsWith("csrftoken="))?.slice(10);
        if(cookie)return cookie;
        // A rotated/missing session cookie is common after cold WebView startup.
        // The independent challenge grants no rights or identity.
        try{return (await challenge((await ledger().read()).device_id)).csrf_token;}
        catch(e){if(e.code==="network")throw e;}
        if(csrf())return csrf();
        const page=await request("/mining-master/tablet/login/?intent=login");
        if(!page.ok)throw fail("network","Не удалось получить защитный код запроса.");
        return csrf() || new DOMParser().parseFromString(await page.text(),"text/html").querySelector('[name="csrfmiddlewaretoken"]')?.value || "";
    }
    async function post(body,csrfOverride,timeoutMs){
        const tok=csrfOverride||await csrfForPost();
        if(!tok)throw fail("auth","Система не подтвердила защитный код запроса. Очередь сохранена.");
        return request(ENDPOINT,{method:"POST",headers:{"Content-Type":"application/json","X-CSRFToken":tok},body:JSON.stringify(body)},timeoutMs);
    }
    function jsonResponse(res){return (res.headers.get("content-type")||"").includes("json");}
    function legacy(id){return {device_id:id,protocol:null,legacy:true,actor:null,snapshot:null,ticket:null};}
    async function identify(){
        const {device_id,public_key}=await ledger().deviceIdentity();
        let res=await request(ENDPOINT+"?"+new URLSearchParams({protocol:PROTOCOL,device_id}));
        if([404,405].includes(res.status))return legacy(device_id);
        if(res.status>=500)throw fail("network","Сервис обмена недоступен. Очередь сохранена.");
        if(!jsonResponse(res)){
            if(new URL(res.url,location.href).pathname.includes("/login"))throw fail("auth","Требуется личный вход для обмена.");
            return legacy(device_id);
        }
        let data=await res.json();
        if(res.status===409 && data.code==="enrollment_required"){
            res=await post({op:"enroll",protocol:PROTOCOL,device_id,public_key});
            if(!jsonResponse(res))throw fail("auth","Не удалось зарегистрировать ключ планшета.");
            data=await res.json();
        }
        if(!res.ok)throw fail(['key_mismatch','device_key_mismatch','device_key_conflict','key_conflict'].includes(data.code)?"key_conflict":"auth",data.message||"Требуется действующий личный вход горного мастера.");
        if(data.protocol!==PROTOCOL)return legacy(device_id);
        const actor=data.snapshot?.actor;
        if(typeof data.ticket!=="string" || !data.ticket || actor?.verified!==true || actor.role!==ROLE ||
           !actor.key || !actor.generation || !Number.isSafeInteger(actor.id) || !Number.isSafeInteger(actor.access_id))
            throw fail("auth","Сервер не подтвердил сотрудника и его допуск.");
        if(data.public_key && (data.public_key.x!==public_key.x||data.public_key.y!==public_key.y))
            throw fail("key_conflict","Ключ планшета отличается от зарегистрированного. Очередь сохранена.");
        if(data.grant_until)actor.grant_until=data.grant_until;
        await ledger().putTicket(actor.key,actor.generation,data.ticket);
        for(const alias of (data.actor_aliases||[])){
            if(/^session:[a-f0-9]{64}$/.test(alias))await ledger().putTicket(alias,actor.generation,data.ticket);
        }
        const previous=await ledger().read();
        if(previous.shift.is_open && previous.actor?.generation===actor.generation &&
            data.actor_aliases?.includes(previous.actor.key)){
            // Only a server-proven alias for this very session may finish its legacy shift.
            data.snapshot.actor={...actor,key:previous.actor.key};
        }
        wasSupported=true;
        return {device_id,protocol:PROTOCOL,legacy:false,actor:data.snapshot.actor,snapshot:data.snapshot,ticket:data.ticket,actor_aliases:data.actor_aliases||[]};
    }
    async function send(event,proof){
        const ack=(state,message)=>({id:event.id,actor_key:event.actor_key,actor_id:event.actor_id,
            actor_access_id:event.actor_access_id,status:state,message});
        try{
            const ticket=await ledger().getTicket(event.actor_key,event.actor_generation);
            if(!ticket)return ack("auth","Для обмена нужен подтверждённый личный допуск автора этого действия.");
            const response=await post({protocol:PROTOCOL,ticket,event,...proof});
            if(response.status>=500 || !jsonResponse(response))return ack("retry","Сервер не подтвердил событие. Оно сохранено.");
            const data=await response.json();
            // Never convert HTTP 200 or a malformed body into an applied receipt.
            if(data.status==="applied" && !response.ok)return ack("retry","Ответ сервера противоречив. Очередь сохранена.");
            if(!data.id && data.status==="auth" && !response.ok)return ack("auth",data.message||"Не подтверждён допуск или ключ планшета.");
            if(data.status==="applied" && data.id===event.id && data.actor_key===event.actor_key)update({last_ack_at:new Date().toISOString()});
            return data;
        }catch(e){update({connected:e.code!=="network",message:e.message});return ack(e.code==="auth"?"auth":"retry",e.message);}
    }
    async function readAs(actor,ticket){
        const device_id=(await ledger().read()).device_id, nonce=await challenge(device_id);
        const proof=await ledger().signSnapshotRead(actor,nonce.challenge,nonce.server_time);
        const res=await post({protocol:PROTOCOL,op:"snapshot",ticket,...proof},nonce.csrf_token);
        if(!res.ok || !jsonResponse(res))throw fail("auth","Требуется обновить личный допуск мастера.");
        const data=await res.json(),actual=data.snapshot?.actor;
        const matches=actual?.key===actor.key || actor.key.startsWith("session:") && actual?.generation===actor.generation;
        if(data.protocol!==PROTOCOL||!actual?.verified||actual.role!==ROLE||!matches||
            actual.generation!==actor.generation || (actor.id!=null && actual.id!==actor.id) ||
            (actor.access_id!=null && actual.access_id!==actor.access_id))throw fail("auth","Система не подтвердила автора текущей смены.");
        data.snapshot.actor={...actual,key:actor.key,grant_until:actor.grant_until};
        return {protocol:PROTOCOL,legacy:false,device_id,actor:data.snapshot.actor,snapshot:data.snapshot};
    }
    function sameLocal(actor,confirmed){
        return !actor || (confirmed?.key===actor.key && confirmed?.generation===actor.generation);
    }
    async function readReport(path,actor){
        if(!win.FieldReports?.handles(path) || !path.startsWith("/mining-master/tablet/") || path.includes("#"))
            throw fail("report_not_allowed","Этот запрос не входит в отчёты мастера.");
        const state=await ledger().read();
        if(state.locked || !state.shift?.is_open || !sameLocal(actor,state.actor) || !state.actor?.verified)
            throw fail("auth","Нет открытой смены владельца отчёта.");
        const ticket=await ledger().getTicket(actor.key,actor.generation);
        if(!ticket)throw fail("auth","У этого сотрудника нет подписанного допуска для отчётов.");
        const nonce=await challenge(state.device_id), proof=await ledger().signReportRead(actor,nonce.challenge,nonce.server_time,path);
        const response=await post({protocol:PROTOCOL,op:"report",ticket,...proof},nonce.csrf_token,30000);
        if(!response.ok || response.headers.get("X-Copper-Report-Read")!=="1")
            throw fail("report_unavailable","Система не вернула запрошенный отчёт мастера.");
        const after=await ledger().read();
        if(after.locked || !sameLocal(actor,after.actor) || !after.shift?.is_open || after.shift.local_shift_id!==state.shift.local_shift_id)
            throw fail("auth","Во время загрузки отчёта изменился сотрудник или смена.");
        return response;
    }
    async function cycle(){
        let cap;
        try{
            const local=await ledger().read(),head=local.outbox[0];
            // A signed old shift is allowed to arrive before the new UI signs in.
            if(head && await ledger().getTicket(head.actor_key,head.actor_generation))await ledger().flush(send);
            const own=local.actor?.verified&&!local.locked?local.actor:null;
            const localTicket=own && await ledger().getTicket(own.key,own.generation);
            try{cap=await identify();}
            catch(e){
                if(e.code!=="auth"||!localTicket)throw e;
                cap=await readAs(own,localTicket);
            }
            if(own && !cap.legacy && !sameLocal(own,cap.actor)){
                if(!localTicket)throw fail("auth","Сеанс сервера принадлежит другому мастеру. Сохранён ваш локальный пульт.");
                cap=await readAs(own,localTicket);
            }
            if(cap.legacy){update({connected:true,protocol:null,message:"Сервер ещё не принимает подписанные события мастера. Действия сохранены."});return cap;}
            update({connected:true,protocol:PROTOCOL,message:""});
            if(!uiBusy())await ledger().ingest(cap.snapshot);
            else await ledger().ingest(cap.snapshot,{defer:true});
            await ledger().flush(send);
            await ledger().retry(cap.actor.key,cap.actor.generation);
            const afterFlush=await ledger().read();
            if(afterFlush.sync.needs_reconcile && !afterFlush.outbox.length){
                // Read after the commit(s), never replace the projection with the pre-flush snapshot.
                const later=own && localTicket ? await readAs(own,localTicket):await identify();
                if(later.actor?.key===cap.actor.key)await ledger().ingest(later.snapshot,{defer:uiBusy()});
            }
            if(!uiBusy())await ledger().reconcile();
            update({last_snapshot_at:new Date().toISOString()});return cap;
        }catch(e){
            update({connected:e.code!=="network",message:e.message});
            // An expired UI session cannot switch ownership of a previously signed event.
            // A valid device ticket can still submit it to the CSRF-protected endpoint.
            if(e.code==="auth" && (wasSupported || (await ledger().read()).outbox.length))await ledger().flush(send);
            return null;
        }
    }
    function refresh(){if(!pending)pending=cycle().finally(()=>{pending=null;});return pending;}
    function wake(){clearTimeout(timer);timer=setTimeout(()=>refresh().catch(()=>{}),180);}
    function setBusy(fn){uiBusy=typeof fn==="function"?fn:()=>false;ledger().setBusy(uiBusy);}
    function idle(){if(!uiBusy())return ledger().reconcile();}
    win.FieldTransport={identify,refresh,wake,setBusy,idle,status,wireFetch,readReport,protocol:PROTOCOL};
    win.addEventListener("online",wake);
    win.addEventListener("native-resume",wake);
    win.addEventListener("offline",()=>update({connected:false,message:"Нет связи. Действия сохраняются."}));
    doc.addEventListener("visibilitychange",()=>{if(!doc.hidden)wake();});
    // Ordinary foreground polling also catches server recovery with Wi-Fi still connected.
    setInterval(()=>{if(!doc.hidden)wake();},30000);
})(window,document);
