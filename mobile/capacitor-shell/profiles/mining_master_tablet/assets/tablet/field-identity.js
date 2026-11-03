/* Personal admission is verified online once for this shared tablet. The only
   offline credential stored is a salted verifier protected by a non-exportable key. */
(function(win,doc){
    "use strict";
    if(win.FieldIdentity)return;
    const ROOT="/mining-master/tablet/",LOGIN=ROOT+"login/", ACTIVATE=ROOT+"activate/", ROLE="mining_master";
    const encoder=new TextEncoder(),GRANT_TTL=30*86400000,AGAIN="Телефон или личный PIN не подошли.";
    const rawFetch=win.fetch.bind(win),ledger=()=>win.FieldLedger;
    let authBusy=false,paintPending=false;
    function fail(message,kind="denied"){return Object.assign(new Error(message),{kind});}
    async function request(path,opts={}){
        const ctl=new AbortController(), timer=setTimeout(()=>ctl.abort(),6000);
        try{
            const res=await rawFetch(path,{...opts,credentials:"same-origin",cache:"no-store",signal:ctl.signal});
            if(res.url && new URL(res.url).origin!==location.origin)throw fail("Система не подтвердила адрес.");
            if(res.status>=500)throw fail("Нет связи с системой.","network");
            return res;
        }catch(err){if(err.kind)throw err;throw fail("Нет связи с системой.","network");}
        finally{clearTimeout(timer);}
    }
    const docOf=text=>new DOMParser().parseFromString(text,'text/html');
    const csrf=doc=>doc?.querySelector('input[name=csrfmiddlewaretoken]')?.value || document.cookie.split(';').map(x=>x.trim()).find(x=>x.startsWith('csrftoken='))?.slice(10) || '';
    function digits(raw){let d=String(raw||'').replace(/\D/g,''); if(d.length===11 && /^[78]/.test(d)) d=d.slice(1); return d;}
    const hex=buffer=>Array.from(new Uint8Array(buffer),x=>x.toString(16).padStart(2,'0')).join('');
    const unhex=text=>Uint8Array.from(text.match(/../g)||[],pair=>parseInt(pair,16));
    const hash=async text=>hex(await crypto.subtle.digest('SHA-256',encoder.encode(text)));
    // IndexedDB never stores a PIN, clear phone, employee password, or exportable device key.
    let dbPromise;
    const db=()=>dbPromise ||= new Promise((resolve,reject)=>{
        const op=indexedDB.open('cr-master-personal-field-access-v2',1);
        op.onupgradeneeded=()=>op.result.createObjectStore('grants');
        op.onsuccess=()=>resolve(op.result); op.onerror=()=>reject(op.error);
    });
    async function vault(key,newValue,replace=true) {
        const database=await db();
        return new Promise((resolve,reject)=>{
            const txn=database.transaction('grants',newValue===undefined?'readonly':'readwrite'), shelf=txn.objectStore('grants');
            let value; const read=shelf.get(key); read.onsuccess=()=>{
                value=read.result;
                if(newValue!==undefined && (replace || value===undefined)){value=newValue;shelf.put(value,key);}
            };
            txn.oncomplete=()=>resolve(value); txn.onabort=txn.onerror=()=>reject(txn.error||new Error('Нет доступа к памяти планшета.'));
        });
    }
    async function deviceKey(){
        const current=await vault('device-key'); if(current) return current;
        const key=await crypto.subtle.generateKey({name:'HMAC',hash:'SHA-256'},false,['sign']);
        return vault('device-key',key,false);
    }
    async function proof(pin,salt){
        const protectedPin=await crypto.subtle.sign('HMAC',await deviceKey(),encoder.encode('master-personal-pin:'+pin));
        const key=await crypto.subtle.importKey('raw',protectedPin,'PBKDF2',false,['deriveBits']);
        return hex(await crypto.subtle.deriveBits({name:'PBKDF2',hash:'SHA-256',salt:unhex(salt),iterations:210000},key,256));
    }
    function same(a,b){let diff=a.length^b.length; for(let i=0;i<Math.max(a.length,b.length);i++) diff|=(a.charCodeAt(i)||0)^(b.charCodeAt(i)||0); return diff===0;}
    async function enroll(phoneKey,pin,actor){
        const salt=hex(crypto.getRandomValues(new Uint8Array(16)));
        await vault('grant:'+phoneKey,{actor,salt,proof:await proof(pin,salt),until:Date.now()+GRANT_TTL,failures:0,retry:0});
    }
    // Recover the original server deadline lost by pre-.45 ACKs. The PIN verifier's
    // local enrollment date is not the server ticket expiry.
    async function enrolledGrantUntil(actor){
        if(!actor?.verified || actor.role!==ROLE || !actor.id || !actor.access_id || !actor.generation)return null;
        const records=[await vault("scope:"+await hash(actor.generation))];
        if(/^phone:[a-f0-9]{64}$/.test(actor.key))records.push((await vault("grant:"+actor.key.slice(6)))?.actor);
        const deadlines=records.filter(r=>r?.verified && r.role===ROLE && r.key===actor.key && r.id===actor.id &&
            r.access_id===actor.access_id && r.generation===actor.generation && Object.hasOwn(r,"grant_until")).map(r=>r.grant_until);
        if(!deadlines.length || deadlines.some(value=>!(Date.parse(value)>Date.now())))return null;
        return deadlines.reduce((earlier,value)=>Date.parse(earlier)<=Date.parse(value)?earlier:value);
    }
    async function localAuth(phoneKey,pin){
        const verify=()=>verifyLocalPin(phoneKey,pin);
        return navigator.locks ? navigator.locks.request("cr-personal-pin:"+phoneKey,verify) : verify();
    }
    async function verifyLocalPin(phoneKey,pin){
        const key='grant:'+phoneKey, g=await vault(key);
        if(!g || g.until<Date.now()) throw fail('Для первого входа этого мастера на планшете нужна связь. После проверки можно работать без сети.');
        const prior=(await ledger().read()).actors[g.actor.key];
        if(prior && (!prior.verified || prior.grant_cleared || prior.generation!==g.actor.generation ||
            prior.grant_until && !(Date.parse(prior.grant_until)>Date.now())))
            throw fail('Система отозвала допуск. Для проверки доступа нужен вход с сетью.');
        if(g.retry>Date.now()) throw fail('Слишком много попыток. Повторите вход позже.');
        if(!same(g.proof,await proof(pin,g.salt))){
            g.failures=(g.failures||0)+1; g.retry=g.failures>=5?Date.now()+Math.min(3600000,30000*2**Math.min(g.failures-5,7)):0;
            await vault(key,g); throw fail(AGAIN);
        }
        g.retry=g.failures=0; await vault(key,g); return g.actor;
    }
    const pos=value=>{const n=Number(value);return Number.isSafeInteger(n)&&n>0?n:null;};

    async function role(){
        const res=await request("/realtime/state/?include_events=0&role_app_code=mining_master");
        if(!res.ok || !res.headers.get("content-type")?.includes("json"))throw fail("Истёк личный вход.","auth");
        const v=await res.json();
        if(!v.authenticated || !v.role_active || v.active_role_code!==ROLE || v.session_role_code!==ROLE || v.role_app_code!==ROLE)
            throw fail("Система не подтвердила действующий доступ горного мастера.","auth");
        return v;
    }
    async function confirmScope(actor){
        if(!actor?.verified || actor.role!==ROLE || !actor.generation)return false;
        const state=await role();
        return String(state.session_revision||"")===String(actor.generation);
    }
    function projected(page,remote,actor){
        const board=page.querySelector(".dispatcher-board[data-dispatcher-own-shift-open]");
        if(!board||page.body.dataset.observerMode==="true" || page.body.dataset.appRoleCode!==ROLE)
            throw fail("Подлинный личный пульт недоступен.","auth");
        const trucks=new Map(), excavators=new Map(), rows=[];
        function item(n,map){
            const id=pos(n.dataset.equipmentId);if(!id)return null;
            const number=(map===excavators?n.dataset.excavatorSlot:n.querySelector(":scope > strong")?.textContent)||n.dataset.equipmentName||String(id);
            map.set(id,{id,number:String(number).trim(),label:n.dataset.equipmentName||number});return id;
        }
        board.querySelectorAll('[data-dispatcher-drag="truck"][data-equipment-id]').forEach(n=>item(n,trucks));
        board.querySelectorAll('[data-dispatcher-drag="excavator"][data-equipment-id]').forEach(n=>item(n,excavators));
        board.querySelectorAll('[data-dispatcher-drop="complex"][data-equipment-id]').forEach(n=>{
            const id=item(n,excavators);if(id)rows.push({excavator_id:id,truck_ids:[...n.querySelectorAll('[data-dispatcher-drag="truck"]')].map(t=>pos(t.dataset.equipmentId)).filter(Boolean)});
        });
        const own=board.dataset.dispatcherOwnShiftOpen==="true";
        const person=board.querySelector(".dispatcher-active-badge:not(.is-muted) .dispatcher-active-text strong")?.textContent.trim();
        if(own&&person)actor.full_name=person;
        let details={};try{details=JSON.parse(page.getElementById("gd-equipment-cards-data")?.textContent||"{}");}catch{/* Never invent card facts. */}
        return {actor,server_version:Number(board.dataset.operationalStateVersion)||Number(remote.version)||0,
            inventory:{trucks:[...trucks.values()],excavators:[...excavators.values()]}, live:{excavators:rows},
            ...(Object.keys(details).length?{details}:{}),
            shift:{is_open:own,server_id:null}, occupied:!!board.querySelector(".dispatcher-active-badge:not(.is-muted)")&&!own};
    }
    async function verifiedState(page,phoneKey){
        const cap=await win.FieldTransport.identify();
        if(cap.actor && !cap.legacy)return {actor:cap.actor,snap:cap.snapshot,occupied:!!cap.snapshot.blocking_shift};
        const v=await role(),gen=String(v.session_revision||"");
        if(!gen)throw fail("Система не подтвердила личную сессию.","auth");
        let a=phoneKey?{key:"phone:"+phoneKey,role:ROLE,id:null,access_id:null,verified:true,generation:gen,full_name:"Горный мастер"}:
            await vault("scope:"+await hash(gen));
        const own=page.querySelector(".dispatcher-board")?.dataset.dispatcherOwnShiftOpen==="true";
        if(!a && own)a={key:"session:"+await hash(gen),id:null,access_id:null,role:ROLE,verified:true,generation:gen,full_name:"Горный мастер"};
        if(!a)throw fail("Начните смену по личному телефону и PIN.","auth");
        const snap=projected(page,v,a);
        return {actor:a,snap,occupied:snap.occupied};
    }
    function notice(message){
        const form=doc.querySelector("form[data-tablet-shift-entry]"),slot=form?.parentNode||doc.querySelector(".dispatcher-shell")||doc.body;
        if(!slot)return;
        let box=doc.querySelector("[data-field-notice]");
        if(!box){box=doc.createElement("div");box.dataset.fieldNotice="";box.setAttribute("role","status");
            box.className=form?"tablet-auth-error":"dispatcher-messages";slot.prepend(box);}
        box.textContent=String(message);
    }
    async function onlineLogin(phone,pin,activationForm){
        const gate=activationForm?null:await request(LOGIN+"?intent=login");
        if(gate&&!gate.ok)throw fail("Система не разрешила вход.");
        const token=activationForm?csrf(doc):csrf(docOf(await gate.text()));
        if(!token)throw fail("Форма входа устарела.");
        const data=activationForm?new URLSearchParams(new FormData(activationForm)):
            new URLSearchParams({phone:"+7"+phone,access_code:pin,intent:"login"});
        data.set("csrfmiddlewaretoken",token);
        const result=await request(activationForm?ACTIVATE:LOGIN,{method:"POST",body:data,headers:{"X-CSRFToken":token}});
        const route=new URL(result.url).pathname;
        if(!activationForm && route===ACTIVATE){
            sessionStorage.setItem("cr-first-phone-key",await hash("7"+phone));location.assign(ACTIVATE);
            throw fail("Создайте личный PIN на планшете.","activation");
        }
        const raw=await result.text(),page=docOf(raw);
        if(route!==ROOT||!result.ok)throw fail(page.querySelector(".tablet-auth-error")?.textContent.trim()||AGAIN);
        return {raw,page};
    }
    async function signIn(phoneValue,pin,activationForm=null){
        const phone=digits(phoneValue), phoneKey=activationForm?sessionStorage.getItem("cr-first-phone-key"):await hash("7"+phone);
        if(!phoneKey||(!activationForm&&!/^\d{10}$/.test(phone))||!/^\d{6}$/.test(pin))throw fail(AGAIN);
        if(authBusy)throw fail("Вход уже выполняется.");
        authBusy=true;
        try{
            const before=await ledger().read(), key="phone:"+phoneKey;
            if(before.shift?.is_open && before.actor && before.actor.key!==key)
                throw fail("На планшете открыта смена другого мастера. Сначала завершите её.");
            let actor,snap,raw;
            try{
                if(navigator.onLine===false)throw fail("Нет сети.","network");
                const result=await onlineLogin(phone,pin,activationForm);raw=result.raw;
                const v=await verifiedState(result.page,phoneKey);actor=v.actor;snap=v.snap;
                if(v.occupied)throw fail("На пульте открыта чужая смена. Нужна пересменка.");
                if(actor.key!==key || actor.role!==ROLE || !actor.verified)throw fail("Не совпал подтверждённый доступ сотрудника.");
                await enroll(phoneKey,pin,actor);
                await vault("scope:"+await hash(actor.generation),actor);
                sessionStorage.removeItem("cr-first-phone-key");
            }catch(error){
                if(activationForm||error.kind!=="network")throw error;
                actor=await localAuth(phoneKey,pin);
            }
            // An authorized shell and park must exist before starting a new local shift.
            if(raw){
                try{
                    await win.FieldArchive.prime(actor,{raw});
                    await Promise.all([win.FieldArchive.capture(LOGIN,actor),win.FieldArchive.capture(LOGIN+"?intent=start_shift",actor)]);
                }catch(error){
                    if(!await win.FieldArchive.has(actor)||!await win.FieldArchive.has(null,LOGIN+"?intent=start_shift"))throw error;
                }
            }
            if(!(await win.FieldArchive.has(actor)))throw fail("Для первой подготовки пульта этого мастера нужна связь.");
            await ledger().selectActor(actor);
            if(snap)await ledger().ingest(snap);
            if(!(await ledger().read()).shift.is_open)await ledger().openShift(actor);
            win.FieldTransport.wake();
            if(!await win.FieldArchive.render(ROOT,{actor}))throw fail("Пульт недоступен.");
        }finally{authBusy=false;}
    }
    async function finish(){
        await ledger().closeShift();await ledger().lock();win.FieldTransport.wake();
        if(!await win.FieldArchive.render(LOGIN))location.assign(LOGIN);
    }
    function controlForm(form,submitter){
        if(!form || form.method.toLowerCase()!=="post")return null;
        // "action" is also the name of the shift button; on such a form
        // form.action resolves to the element, not to the destination URL.
        const u=new URL(form.getAttribute("action")||location.pathname,location.href);if(u.origin!==location.origin)return null;
        const action=submitter?.value || form.querySelector('[name="action"]')?.value;
        if(u.pathname===ROOT && ["start_shift","end_shift"].includes(action))return action;
        if(form.matches("form[data-tablet-shift-entry]") && u.pathname===LOGIN)return "login";
        if(form.matches("form[data-tablet-shift-entry]") && u.pathname===ACTIVATE && submitter?.value!=="cancel")return "activate";
        return null;
    }
    async function act(form,cmd){
        if(cmd==="end_shift")return finish();
        const p=form.elements.phone||form.elements.reauth_phone;
        const secret=form.elements.access_code||form.elements.reauth_access_code||form.elements.new_access_code;
        if(cmd==="start_shift"&&(!p||!secret)){
            if(!await win.FieldArchive.render(LOGIN+"?intent=start_shift"))location.assign(LOGIN+"?intent=start_shift");return;
        }
        if(!form.reportValidity())return;
        try{await signIn(p?.value||"",String(secret?.value||"").replace(/\D/g,""),cmd==="activate"?form:null);}
        finally{if(secret)secret.value="";const repeat=form.elements.confirm_access_code;if(repeat)repeat.value="";}
    }
    doc.addEventListener("submit",event=>{
        const form=event.target,cmd=controlForm(form,event.submitter);
        if(!cmd){
            if(doc.documentElement.hasAttribute("data-cr-field-local") && form.method==="get" && new URL(form.action).pathname===LOGIN){
                event.preventDefault();event.stopImmediatePropagation();
                win.FieldArchive.render(LOGIN+"?intent=start_shift").then(ok=>{if(!ok)notice("Для первичного входа нужна связь.");});
            }
            return;
        }
        event.preventDefault();
        if((event.submitter?.dataset.confirm||form.dataset.confirm) && form.dataset.confirmed!=="1")return;
        event.stopImmediatePropagation();act(form,cmd).catch(e=>notice(e.message));
    },true);
    const priorSubmit=HTMLFormElement.prototype.submit;
    HTMLFormElement.prototype.submit=function(){
        const cmd=controlForm(this);
        if(!cmd)return priorSubmit.call(this);
        if(cmd==="end_shift" && this.dataset.confirmed!=="1")return;
        act(this,cmd).catch(e=>notice(e.message));
    };
    async function recover(){
        let route;
        try{route=decodeURIComponent((location.hash.split("#cr-saved-board=")[1]||ROOT).split("&startup=1")[0]);}catch{route=ROOT;}
        const state=await ledger().read();
        if(!state.actor?.verified||!state.shift?.is_open||state.locked)route=LOGIN;
        const restored=await win.FieldArchive.render(route);
        if(!restored && location.hash.endsWith("&startup=1") && navigator.onLine){
            location.replace(route.startsWith("/")?route:ROOT);return true;
        }
        return restored;
    }
    async function attach(){
        const marker=doc.documentElement;
        if(marker.hasAttribute("data-cr-saved-board"))return;
        if(location.pathname!==ROOT)return;
        const board=doc.querySelector(".dispatcher-board");if(!board)return;
        try{
            const state=await ledger().read();
            if(!marker.hasAttribute("data-cr-field-local")){
                const v=await verifiedState(doc);
                if(!state.locked && (!state.actor || state.actor.key===v.actor.key) && (v.snap.shift?.is_open||state.shift?.is_open)){
                    await ledger().selectActor(v.actor);await ledger().ingest(v.snap);
                    if(v.actor.generation)await vault("scope:"+await hash(v.actor.generation),v.actor);
                    win.FieldArchive.prime(v.actor).catch(err=>notice("Автономная копия: "+err.message));
                }else if(state.locked && state.outbox?.some(e=>e.type==="shift.close")){
                    await win.FieldArchive.render(LOGIN);return;
                }
            }
            await paint();win.FieldTransport.wake();
        }catch(error){await paint();notice(error.message);}
    }
    async function paint(){
        if(paintPending)return;paintPending=true;
        try{
            const state=await ledger().read(), board=doc.querySelector(".dispatcher-board");
            if(!board)return;
            const open=!!state.actor?.verified&&state.shift?.is_open&&!state.locked;
            if(doc.documentElement.hasAttribute("data-cr-field-local")||state.pending_count){
                board.dataset.dispatcherOwnShiftOpen=board.dataset.dispatcherShiftOpen=String(open);
                board.classList.toggle("is-readonly",!open);
                if(open){
                    doc.body.dataset.roleAccessActive="true";
                    const name=board.querySelector(".dispatcher-active-text strong");
                    const badge=board.querySelector(".dispatcher-active-badge"), avatar=badge?.querySelector(".dispatcher-active-avatar");
                    const parts=String(state.actor.full_name||"").trim().split(/\s+/);
                    const shortName=[parts[0],parts.slice(1).map(x=>x[0]+".").join("")].filter(Boolean).join(" ");
                    if(name && name.textContent!==shortName)name.textContent=shortName;
                    // Only the personal, verified header supplies a photo. Formatting the name must preserve it.
                    if(avatar && !avatar.querySelector("img")) {
                        const initials=parts.slice(0,2).map(x=>x[0]).join("").toUpperCase();
                        if(avatar.textContent!==initials)avatar.textContent=initials;
                    }
                    badge?.classList.remove("is-muted");
                    const slot=board.querySelector(".dispatcher-shift-slot");
                    if(slot&&!slot.querySelector('button[value=end_shift]')){
                        const f=doc.createElement("form");f.className="dispatcher-shift-form";f.method="post";f.action=ROOT;
                        const b=doc.createElement("button");b.type="submit";b.name="action";b.value="end_shift";b.className="dispatcher-shift-button is-danger";b.textContent="Завершить смену";
                        Object.assign(b.dataset,{confirm:"Завершить свою смену?",confirmVariant:"shift-end",confirmDelay:"10",
                            confirmPerson:state.actor.full_name,confirmDate:new Date(state.shift.opened_at).toLocaleDateString("ru"),
                            confirmTime:new Date(state.shift.opened_at).toLocaleTimeString("ru",{hour:"2-digit",minute:"2-digit"})});
                        f.append(b);slot.replaceChildren(f);
                        win.initAppConfirmForms?.(f);
                    }
                    const button=slot?.querySelector('button[value=end_shift]');
                    if(button && state.shift.opened_at){
                        const started=new Date(state.shift.opened_at);
                        button.dataset.confirmDate=started.toLocaleDateString("ru");
                        button.dataset.confirmTime=started.toLocaleTimeString("ru",{hour:"2-digit",minute:"2-digit"});
                        button.dataset.confirmPerson=state.actor.full_name;
                        const since=board.querySelector(".dispatcher-active-text em");
                        if(since)since.textContent="с "+button.dataset.confirmTime;
                    }
                }
            }
            const badge=doc.querySelector(".app-connection-dot");
            if(badge){const status=win.FieldTransport.status,blocked=state.blocked_count,n=state.pending_count;
                const msg=blocked?"Нужна сверка: "+blocked:n?"На планшете, ожидает подтверждения: "+n:status.message||"Состояние связи";
                badge.title=msg;badge.setAttribute("aria-label",msg);
                if(n || blocked){badge.style.setProperty("--app-connection-color",blocked?"#ff6259":"#ffc54f");badge.dataset.fieldPending="true";}
                else if(badge.dataset.fieldPending){delete badge.dataset.fieldPending;badge.style.removeProperty("--app-connection-color");}}
        }finally{paintPending=false;}
    }
    const actionTitles={"shift.open":"Начало смены","shift.close":"Завершение смены","live.assign":"Назначение техники",
        "live.move":"Изменение комплекса","draft.save":"Сохранение черновика","draft.apply":"Активация расстановки",
        "card.settings":"Настройки комплекса","downtime.close":"Завершение простоя","assignment.cancel":"Отмена назначения",
        "trip.cancel":"Отмена рейса","trip.complete":"Завершение рейса","trip.manual":"Ручной рейс",
        "worker_shift.close":"Завершение смены сотрудника","report.trip_correct":"Исправление рейса",
        "report.template_save":"Сохранение шаблона отчёта","conflict.resolve":"Сверка с сервером"};
    async function queueDialog(){
        const state=await ledger().read();if(!state.outbox.length||doc.querySelector("[data-field-queue]"))return;
        const modal=doc.createElement("dialog");modal.dataset.fieldQueue="";modal.className="app-confirm-dialog";
        modal.style.cssText="max-width:min(560px,94vw);max-height:78vh;overflow:auto;background:#142127;color:#f2f6f8";
        const title=doc.createElement("h2");title.textContent="Действия на планшете";modal.append(title);
        for(const e of state.outbox){
            const row=doc.createElement("p"),description=doc.createElement("strong");
            description.textContent=new Date(e.occurred_at).toLocaleTimeString("ru",{hour:"2-digit",minute:"2-digit"})+" · "+(actionTitles[e.type]||"Изменение смены");
            row.append(description,doc.createElement("br"),doc.createTextNode(e.message||(e.status==="conflict"?"Требуется сверка.":e.status==="auth"?"Ожидает личного допуска.":"Ожидает подтверждения.")));
            if(e.status==="conflict" && e.actor_key===state.actor?.key && !state.locked){
                const button=doc.createElement("button");button.type="button";button.className="secondary";button.textContent="Сверить";
                button.onclick=()=>{
                    const help=doc.createElement("p");help.textContent="Принять состояние сервера для этого действия? Исходная запись останется в журнале.";
                    const approve=doc.createElement("button");approve.className="danger";approve.textContent="Принять состояние сервера";approve.type="button";
                    approve.onclick=async()=>{try{await ledger().resolveConflict(e.id);win.FieldTransport.wake();modal.close();}
                        catch(err){help.textContent=err.message;}};
                    button.replaceWith(help,approve);
                };row.append(doc.createElement("br"),button);
            }
            modal.append(row);
        }
        const close=doc.createElement("button");close.type="button";close.className="secondary";close.textContent="Закрыть";close.onclick=()=>modal.close();
        modal.append(close);modal.onclose=()=>modal.remove();doc.body.append(modal);modal.showModal();
    }
    doc.addEventListener("click",event=>{
        if(!event.target.closest?.(".app-connection-dot[data-field-pending]"))return;
        event.preventDefault();event.stopImmediatePropagation();queueDialog().catch(e=>notice(e.message));
    },true);
    win.FieldIdentity={confirmScope,enrolledGrantUntil,notice,signIn,finish,recover,attach,paint};
    win.addEventListener("fieldledgerchange",()=>paint().catch(()=>{}));
    win.addEventListener("fieldtransportchange",()=>paint().catch(()=>{}));
    if(doc.readyState==="loading")doc.addEventListener("DOMContentLoaded",()=>attach(),{once:true});else attach();
})(window,document);
