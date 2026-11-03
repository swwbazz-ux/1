/* Local projection and signed outbox for the authentic mining-master tablet.
   An event, its audit copy and its visible effect commit in the same IDB transaction. */
(function(win){
    "use strict";
    if(win.FieldLedger)return;
    const DB="copper-master-tablet-ledger-v2", SLOT="tablet", LEASE=30000;
    const uuid=()=>crypto.randomUUID(), clone=v=>v===undefined?undefined:structuredClone(v);
    const error=(code,message)=>Object.assign(new Error(message),{code});
    const empty=()=>({excavators:[]});
    let openDB, keyPromise, draining, sender, timer, activeOwner=null,leaving=false,busy=()=>false;
    let channel;
    try{channel=new BroadcastChannel("copper-tablet-ledger-v2");channel.onmessage=()=>notify(false);}catch(_){}
    function notify(broadcast){win.dispatchEvent(new Event("fieldledgerchange"));if(broadcast)channel?.postMessage(true);}
    function initial(){
        return {rev:0,device_id:uuid(),sequence:0,last_event_id:null,actor:null,locked:false,actors:{},shifts:{},
            snapshot_ready:false,live:empty(),live_tokens:{},inventory:{trucks:[],excavators:[]},details:{},refs:{},
            draft:{revision:0,payload:empty(),dirty:false,initialized:false},outbox:[],receipts:{},lease:null,
            sync:{observed_version:0,applied_version:0,needs_reconcile:false},deferred:null};
    }
    function database(){
        if(!openDB)openDB=new Promise((resolve,reject)=>{
            const req=indexedDB.open(DB,2);
            req.onupgradeneeded=()=>{
                const db=req.result,has=name=>db.objectStoreNames.contains(name);
                if(!has("state"))db.createObjectStore("state").put(initial(),SLOT);
                if(!has("secrets"))db.createObjectStore("secrets");
                if(!has("journal"))db.createObjectStore("journal",{keyPath:"id"}).createIndex("sequence","sequence",{unique:true});
                if(!has("confirmations"))db.createObjectStore("confirmations",{keyPath:"id"});
            };
            req.onsuccess=()=>{req.result.onversionchange=()=>{req.result.close();openDB=null;};resolve(req.result);};
            req.onerror=req.onblocked=()=>reject(error("storage_unavailable","Не удалось открыть журнал планшета."));
        }).catch(e=>{openDB=null;throw e;});
        return openDB;
    }
    async function stateTx(write,operation){
        const db=await database();
        return new Promise((resolve,reject)=>{
            const tx=write?db.transaction(["state","journal","confirmations"],"readwrite",{durability:"strict"}):db.transaction("state","readonly");
            const shelf=tx.objectStore("state"), req=shelf.get(SLOT);
            let result,changed=false,failure;
            req.onsuccess=()=>{
                try{
                    const state=req.result;
                    if(!state)throw error("storage_corrupt","Отсутствует локальное состояние планшета.");
                    const before=write?JSON.stringify(state):"";
                    result=operation(state,tx);
                    changed=write&&before!==JSON.stringify(state);
                    if(changed){state.rev++;shelf.put(state,SLOT);}
                }catch(e){failure=e;tx.abort();}
            };
            tx.oncomplete=()=>{resolve(clone(result));if(changed)notify(true);};
            tx.onerror=tx.onabort=()=>reject(failure||error("storage_write_failed","Не удалось сохранить действие. Изменение не применено."));
        });
    }
    async function secret(name,value,onlyMissing=false){
        const db=await database();
        return new Promise((resolve,reject)=>{
            const tx=db.transaction("secrets",value===undefined?"readonly":"readwrite"),s=tx.objectStore("secrets"),r=s.get(name);
            let result;r.onsuccess=()=>{
                result=r.result;
                if(value!==undefined && (!onlyMissing||result===undefined)){result=value;s.put(value,name);}
            };
            tx.oncomplete=()=>resolve(result);tx.onerror=tx.onabort=()=>reject(error("key_storage_failed","Хранилище ключей недоступно."));
        });
    }
    async function deviceKey(){
        if(!keyPromise)keyPromise=(async()=>{
            const present=await secret("p256");if(present)return present;
            // Losing a pinned key is a service condition, not permission to manufacture a replacement.
            if((await stateTx(false,s=>s.sequence))!==0)throw error("key_missing","Недоступен исходный ключ планшета. Очередь сохранена.");
            const keypair=await crypto.subtle.generateKey({name:"ECDSA",namedCurve:"P-256"},false,["sign","verify"]);
            return secret("p256",keypair,true);
        })().catch(e=>{keyPromise=null;throw e;});
        return keyPromise;
    }
    async function deviceIdentity(){
        const pair=await deviceKey(),jwk=await crypto.subtle.exportKey("jwk",pair.publicKey);
        return {device_id:await stateTx(false,s=>s.device_id),public_key:{kty:jwk.kty,crv:jwk.crv,x:jwk.x,y:jwk.y}};
    }
    const b64u=bytes=>{
        let binary="";for(const byte of bytes)binary+=String.fromCharCode(byte);
        return btoa(binary).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,"");
    };
    // Canonical JSON is sorted recursively and rejects values JSON.stringify would silently drop.
    function canonical(v){
        if(v===null||typeof v==="boolean"||typeof v==="string"||Number.isFinite(v))return JSON.stringify(v);
        if(Array.isArray(v))return "["+v.map(canonical).join(",")+"]";
        if(v && typeof v==="object" && Object.getPrototypeOf(v)===Object.prototype)
            return "{"+Object.keys(v).sort().map(k=>JSON.stringify(k)+":"+canonical(v[k])).join(",")+"}";
        throw error("invalid_payload","В событии присутствует недопустимое значение.");
    }
    async function sign(event){
        const bytes=new TextEncoder().encode(canonical(event));
        if(bytes.byteLength>256*1024)throw error("event_too_large","Действие слишком велико для подтверждения. Изменение не применено.");
        const signature=await crypto.subtle.sign({name:"ECDSA",hash:"SHA-256"},(await deviceKey()).privateKey,bytes);
        return {signed_bytes:b64u(bytes),signature:b64u(new Uint8Array(signature))};
    }
    async function signRead(actor,challenge,time,type,resource={}){
        const s=await read();
        if(!s.actor?.verified||s.locked||s.actor.key!==actor.key||s.actor.generation!==actor.generation||
           s.actor.id!==actor.id||s.actor.access_id!==actor.access_id)
            throw error("wrong_actor","Нет действующего локального входа этого мастера.");
        requireCurrentGrant(s.actor);
        const event={id:uuid(),type,format_version:2,device_id:s.device_id,
            actor_key:s.actor.key,actor_id:s.actor.id,actor_access_id:s.actor.access_id,actor_generation:s.actor.generation,
            occurred_at:time,challenge,...resource};
        return {event,...await sign(event)};
    }
    const positive=v=>Number.isSafeInteger(v)&&v>0?v:null;
    function verifiedActor(a){
        if(a?.verified!==true || (a.role||a.role_code)!=="mining_master")throw error("actor_not_verified","Нужен подтверждённый личный доступ горного мастера.");
        const key=String(a.key||"");
        if(!key || ["constructor","prototype","__proto__"].includes(key) || key.length>256)throw error("invalid_actor","Система не подтвердила личность мастера.");
        return {key,id:positive(a.id),access_id:positive(a.access_id),role:"mining_master",verified:true,full_name:String(a.full_name||""),generation:String(a.generation||""),grant_until:a.grant_until||null};
    }
    const shiftOf=s=>s.actor && s.shifts[s.actor.key] || {is_open:false,local_shift_id:null,server_id:null};
    function choose(s,raw){
        const a=verifiedActor(raw),prior=s.actors[a.key];
        if(s.actor&&s.actor.key!==a.key&&shiftOf(s).is_open)throw error("shift_handover_required","Сначала завершите открытую смену другого мастера.");
        if(prior && ((prior.id&&a.id&&prior.id!==a.id)||(prior.access_id&&a.access_id&&prior.access_id!==a.access_id)))
            throw error("actor_scope_conflict","Не совпадает подтверждённая личность сотрудника.");
        if(s.deferred?.actor && s.deferred.actor.key!==a.key)s.deferred=null;
        s.actor=s.actors[a.key]=a;s.locked=false;return a;
    }
    function summary(s){
        const statuses=s.outbox.map(e=>e.status), blocked=statuses.filter(k=>k==="auth"||k==="conflict").length;
        return {...s,deferred:undefined,lease:undefined,shift:shiftOf(s),server_version:s.sync.applied_version,storage_ok:true,
            pending_count:s.outbox.length,blocked_count:blocked,sync:{...s.sync,status:blocked?"blocked":s.outbox.length?"pending":"local"}};
    }
    function read(){return stateTx(false,summary);}
    function requireWork(s){
        if(!s.actor||s.locked||!s.actor.verified||!s.actors[s.actor.key]?.verified)throw error("actor_not_verified","Войдите под своей учётной записью.");
        requireCurrentGrant(s.actor);
        if(!s.snapshot_ready)throw error("initial_snapshot_required","Для первой загрузки парка нужна связь.");
        if(!shiftOf(s).is_open)throw error("own_shift_required","Действие доступно в собственной открытой смене.");
    }
    function requireCurrentGrant(a){
        if(a.grant_until && !(Date.parse(a.grant_until)>Date.now()))throw error("grant_expired","Для продолжения обновите личный допуск при наличии связи.");
    }
    const readRaw=()=>stateTx(false,s=>s);
    function selectActor(a){return stateTx(true,s=>{choose(s,a);return summary(s);});}
    function revokeActor(actor){return stateTx(true,s=>{
        const a=s.actors[actor.key];
        if(a?.generation!==actor.generation || a.id!==actor.id || a.access_id!==actor.access_id)return;
        a.verified=false;
        if(s.actor?.key===actor.key && s.actor.generation===actor.generation){s.actor=null;s.locked=true;}
    });}
    function lock(){return stateTx(true,s=>{
        if(shiftOf(s).is_open)throw error("own_shift_open","Сначала завершите смену.");
        s.locked=true;s.actor=null;return summary(s);
    });}
    function checkedPlan(p,items){
        if(!Array.isArray(p?.excavators))throw error("invalid_plan","Недопустимая расстановка.");
        const knownTrucks=new Set(items?.trucks.map(t=>t.id)),knownExc=new Set(items?.excavators.map(t=>t.id));
        const exc=new Set(),trucks=new Set();
        const rows=p.excavators.map(r=>{
            if(!positive(r.excavator_id)||exc.has(r.excavator_id)||(items&&!knownExc.has(r.excavator_id))||!Array.isArray(r.truck_ids))
                throw error("invalid_plan","Не подтверждён экскаватор в расстановке.");
            exc.add(r.excavator_id);
            for(const id of r.truck_ids){
                if(!positive(id)||trucks.has(id)||(items&&!knownTrucks.has(id)))throw error("invalid_plan","Недопустимое назначение самосвала.");
                trucks.add(id);
            }
            return {excavator_id:r.excavator_id,truck_ids:[...r.truck_ids]};
        });
        return {excavators:rows};
    }
    // Prepare outside IDB: an IDB transaction cannot be kept alive over subtle.sign().
    // The revision CAS prevents a signed snapshot based on a stale actor/shift/sequence.
    async function commit(build,existingId){
        const id=existingId||uuid(),time=new Date().toISOString();
        if(existingId && await journal(existingId))return read();
        let initiatingScope;
        for(let attempt=0;attempt<20;attempt++){
            const s=await readRaw(), rev=s.rev;
            if(existingId && (s.outbox.some(e=>e.id===existingId)||s.receipts[existingId]))return summary(s);
            const scope=JSON.stringify([s.actor?.key,s.actor?.generation,shiftOf(s).local_shift_id,s.locked]);
            if(attempt && scope!==initiatingScope)throw error("shift_changed","Сотрудник или смена изменились во время записи. Действие не выполнено.");
            initiatingScope=scope;
            const operation=build(s,id,time);
            if(!operation)return summary(s);
            const {type,payload,base_payload,reduce,extraDeps,independent}=operation,actor=s.actor,shift=shiftOf(s);
            const deps=independent?[]:[...new Set([s.last_event_id,...(extraDeps||[])].filter(Boolean))];
            const wire={id,format_version:2,device_id:s.device_id,sequence:s.sequence+1,type,occurred_at:time,
                actor_key:actor.key,actor_id:actor.id,actor_access_id:actor.access_id,actor_generation:actor.generation,
                local_shift_id:shift.local_shift_id,server_shift_id:shift.server_id||null,depends_on:deps,payload:clone(payload)};
            if(base_payload!==undefined)wire.base_payload=clone(base_payload);
            const sig=await sign(wire);
            if(reduce && typeof reduce(s,clone(wire))?.then==="function")throw error("async_reducer","Действие должно обновлять проекцию атомарно.");
            const item={...wire,...sig,status:"pending",attempts:0,next_retry_at:0};
            s.sequence=wire.sequence;s.last_event_id=id;s.outbox.push(item);
            const committed=await stateTx(true,(current,tx)=>{
                if(current.rev!==rev)return null;
                tx.objectStore("journal").add({...wire,...sig});
                Object.assign(current,s);return summary(current);
            });
            if(committed){wake();return committed;}
        }
        throw error("write_contention","Пульт изменился во время записи. Повторите действие.");
    }
    function command(type,payload,options={}){
        return commit(s=>{
            requireWork(s);
            if(options.actor_key!=null && options.actor_key!==s.actor.key || options.local_shift_id!=null && options.local_shift_id!==shiftOf(s).local_shift_id)
                throw error("shift_changed","Действие относится к другой смене. Откройте карточку заново.");
            if(!/^[a-z][a-z0-9_.]{0,80}$/.test(type) || ["shift.open","shift.close","conflict.resolve"].includes(type))throw error("invalid_command","Неизвестная команда.");
            return {type,payload,base_payload:options.base_payload,reduce:options.reduce,extraDeps:options.depends_on};
        });
    }
    function openShift(actor,localId){
        return commit((s,id,time)=>{
            if(s.locked)throw error("actor_not_verified","Войдите с личным PIN перед началом смены.");
            choose(s,actor||s.actor);
            requireCurrentGrant(s.actor);
            if(shiftOf(s).is_open)return null;
            if(!s.snapshot_ready)throw error("initial_snapshot_required","Сначала загрузите парк на планшет.");
            const local=localId||id;
            if(!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(local)||s.outbox.some(e=>e.local_shift_id===local)||Object.values(s.receipts).some(e=>e.local_shift_id===local))
                throw error("invalid_shift_id","Нельзя повторно использовать номер локальной смены.");
            s.shifts[s.actor.key]={is_open:true,local_shift_id:local,server_id:null,opened_at:time,closed_at:null,actor_key:s.actor.key};
            return {type:"shift.open",payload:{}};
        });
    }
    function closeShift(){
        return commit(s=>{
            if(!shiftOf(s).is_open)throw error("own_shift_required","Нет открытой смены.");
            requireWork(s);return {type:"shift.close",payload:{},reduce:(st,e)=>{
                const sh=shiftOf(st);sh.is_open=false;sh.closed_at=e.occurred_at;st.locked=true;
            }};
        });
    }
    function expectedDraft(s,expected){
        if(expected!==undefined && s.draft.revision!==expected)throw error("draft_changed","Черновик изменился. Откройте обновлённую расстановку.");
    }
    function savePlan(name,value,expected){
        return commit(s=>{
            requireWork(s);
            const desired=checkedPlan(value,s.inventory),draft=name==="draft",old=draft?s.draft.payload:s.live;
            if(draft)expectedDraft(s,expected);
            if(canonical(old)===canonical(desired)&&!(draft&&s.draft.imported_dirty))return null;
            return {type:draft?"draft.save":"live.save",payload:desired,base_payload:draft?(s.draft.sync_base||old):old,reduce:state=>{
                if(draft){state.draft={payload:desired,revision:state.draft.revision+1,initialized:true,dirty:true};}
                else state.live=desired;
            }};
        });
    }
    function applyDraft(expected,migrationId){
        return commit(s=>{requireWork(s);
            expectedDraft(s,expected);
            const desired=checkedPlan(s.draft.payload,s.inventory);
            return {type:"draft.apply",payload:desired,base_payload:s.live,reduce:(state,event)=>{
                state.live=desired;state.draft.dirty=false;state.draft.imported_dirty=false;
                delete state.draft.sync_base;
                state.last_activation={id:event.id,actor_key:event.actor_key,local_shift_id:event.local_shift_id,
                    status:"queued",target:desired,base:event.base_payload};
            }};
        },migrationId);
    }
    function importDraft(old){
        return stateTx(true,s=>{
            if(s.legacy_imported)return summary(s);
            if(old && (old.dirty || old.activation && old.activation.status!=="applied" || !s.draft.initialized)){
                s.draft={...s.draft,revision:Math.max(s.draft.revision,Number(old.revision)||0)+1,
                    payload:checkedPlan(old.payload),dirty:!!old.dirty,initialized:true,imported_dirty:true,
                    sync_base:clone(s.draft.initialized?s.draft.payload:(old.base||old.payload))};
            }
            s.legacy_imported=true;
            return summary(s);
        });
    }
    function resolveConflict(id){
        return commit(s=>{
            const problem=s.outbox.find(e=>e.id===id && e.status==="conflict");
            if(!problem)throw error("no_conflict","Конфликт уже разрешён или действие ещё не отправлено.");
            if(s.actor?.key!==problem.actor_key || !s.actor?.verified || s.locked)throw error("wrong_actor","Сверка доступна первоначальному мастеру после входа.");
            requireCurrentGrant(s.actor);
            if(s.outbox.some(e=>e.type==="conflict.resolve"&&e.payload.event_id===id))return null;
            return {type:"conflict.resolve",payload:{event_id:id,decision:"accept_server"},independent:true};
        });
    }
    function applySnapshot(s,snap,defer=false){
        if(snap.actor && (s.actor&&s.actor.key!==snap.actor.key || s.locked&&!s.actor))return;
        const version=Number(snap.server_version??snap.version??0);
        if(!Number.isSafeInteger(version)||version<0)throw error("invalid_snapshot","Некорректная версия данных смены.");
        s.sync.observed_version=Math.max(s.sync.observed_version,version);
        if(version<s.sync.applied_version)return;
        if(snap.actor&&s.actor?.key===snap.actor.key&&snap.actor.verified===false){
            s.actor.verified=false;s.actors[s.actor.key].verified=false;s.locked=true;return;
        }
        if(snap.actor && !defer && !s.locked && !s.outbox.some(e=>e.actor_key===snap.actor.key) && (!s.actor || s.actor.key===snap.actor.key)){
            const a=choose(s,snap.actor),old=shiftOf(s),sh=snap.shift;
            if(sh)s.shifts[a.key]={...sh,actor_key:a.key,is_open:sh.is_open===true,
                local_shift_id:(old.server_id&&old.server_id===sh.server_id&&old.local_shift_id)||sh.local_shift_id ||
                    (old.is_open && sh.is_open && !old.server_id ? old.local_shift_id:null) || (sh.is_open?uuid():null)};
        }
        if(s.outbox.length || defer){
            if(!s.deferred || version>=Number(s.deferred.server_version??s.deferred.version??0))s.deferred=clone(snap);
            s.sync.needs_reconcile=true;return;
        }
        if(snap.inventory){
            if(!Array.isArray(snap.inventory.trucks)||!Array.isArray(snap.inventory.excavators))throw error("invalid_inventory","Неполные данные парка.");
            s.inventory=clone(snap.inventory);
        }
        if(snap.live)s.live=checkedPlan(snap.live);
        if(snap.live&&snap.live_tokens)s.live_tokens=clone(snap.live_tokens);
        if(snap.draft&&!s.draft.imported_dirty){
            const payload=checkedPlan(snap.draft.payload||snap.draft);
            // Local revisions are UI compare-and-swap counters, not server primary keys.
            s.draft={revision:s.draft.revision+(canonical(payload)===canonical(s.draft.payload)?0:1),payload,initialized:true,dirty:false};
        }
        else if(snap.live&&!s.draft.initialized)s.draft={revision:0,payload:clone(s.live),initialized:true,dirty:false};
        if(snap.details)s.details=clone(snap.details);
        if(snap.cards){
            const details={};
            for(const [key,item] of Object.entries(snap.cards))if(item?.card && /^(?:equipment|complex):[1-9][0-9]*$/.test(key))details[key]=clone(item.card);
            s.details=details;
        }
        if(snap.refs)s.refs=clone(snap.refs);
        if(snap.live && snap.inventory)s.snapshot_ready=true;
        if(snap.live){
            s.sync.applied_version=version;s.sync.needs_reconcile=version<s.sync.observed_version;s.deferred=null;
        }
    }
    function ingest(snapshot,opts={}){
        return stateTx(true,s=>{applySnapshot(s,snapshot,opts.defer||busy());return summary(s);});
    }
    function reconcile(){
        return stateTx(true,s=>{
            if(s.deferred && !s.outbox.length && !busy())applySnapshot(s,s.deferred);
            return summary(s);
        });
    }
    function cacheDetails(key,data,version=0,actorKey=null){
        if(!key || ["__proto__","prototype","constructor"].includes(key))return Promise.reject(error("invalid_card","Некорректная карточка."));
        return stateTx(true,s=>{
            if(!s.actor?.verified || s.locked || actorKey!==s.actor.key || !Number.isSafeInteger(version) || version<s.sync.applied_version)return summary(s);
            const topologyOnly=["shift.open","draft.save","draft.apply","live.move","live.assign"];
            const firstRead=!Object.hasOwn(s.details,key) && s.outbox.every(e=>e.actor_key===actorKey && topologyOnly.includes(e.type));
            if(!s.outbox.length || firstRead)s.details[key]=clone(data);
            return summary(s);
        });
    }
    const wireKeys=["id","format_version","device_id","sequence","type","occurred_at","actor_key","actor_id","actor_access_id","actor_generation","local_shift_id","server_shift_id","depends_on","payload","base_payload"];
    function wireOf(item){return Object.fromEntries(wireKeys.filter(k=>Object.hasOwn(item,k)).map(k=>[k,clone(item[k])]));}
    function ackMatches(item,ack){
        return ack?.id===item.id && ack.actor_key===item.actor_key && ack.actor_id===item.actor_id && ack.actor_access_id===item.actor_access_id;
    }
    async function drain(send,browserLock=false){
        const owner=uuid();
        activeOwner=owner;
        while(!leaving){
            const item=await stateTx(true,s=>{
                if(s.lease && s.lease.until>Date.now() && s.lease.owner!==owner && !(browserLock&&s.lease.browserLock))return null;
                const first=s.outbox[0];
                const candidate=first?.status==="conflict"?s.outbox.find(e=>e.type==="conflict.resolve"&&e.payload.event_id===first.id):first;
                if(!candidate||["auth","conflict"].includes(candidate.status)||(candidate.next_retry_at||0)>Date.now())return null;
                s.lease={owner,browserLock,until:Date.now()+LEASE};candidate.status="sending";candidate.attempts++;
                return candidate;
            });
            if(!item)break;
            let ack;
            const heartbeat=setInterval(()=>stateTx(true,s=>{if(s.lease?.owner===owner)s.lease.until=Date.now()+LEASE;}).catch(()=>{}),9000);
            try{ack=await send(wireOf(item),{signed_bytes:item.signed_bytes,signature:item.signature});}
            catch(e){ack={status:"retry",message:e.message};}
            finally{clearInterval(heartbeat);}
            const more=await stateTx(true,(s,tx)=>{
                if(s.lease?.owner!==owner)return false;
                s.lease=null;const current=s.outbox.find(e=>e.id===item.id);
                if(!current)return false;
                const valid=ackMatches(current,ack);
                if(valid && ack.status==="applied"){
                    if(current.type==="conflict.resolve"&&ack.resolved_event_id!==current.payload.event_id){
                        current.status="retry";current.next_retry_at=Date.now()+5000;return false;
                    }
                    s.receipts[current.id]={sequence:current.sequence,local_shift_id:current.local_shift_id,actor_key:current.actor_key,server_shift_id:ack.server_shift_id||ack.shift_id||null};
                    tx.objectStore("confirmations").put({id:current.id,...s.receipts[current.id],status:"applied",received_at:new Date().toISOString(),server_received_at:ack.server_received_at||null});
                    if(current.type==="conflict.resolve")s.receipts[ack.resolved_event_id]={resolved_by:current.id};
                    if(current.type==="conflict.resolve")tx.objectStore("confirmations").put({id:ack.resolved_event_id,status:"conflict",resolved_by:current.id});
                    const receiptKeys=Object.keys(s.receipts);
                    if(receiptKeys.length>512)delete s.receipts[receiptKeys[0]];
                    if(s.last_activation?.id===current.id)s.last_activation.status="applied";
                    if(s.last_activation?.id===ack.resolved_event_id && current.type==="conflict.resolve")s.last_activation.status="resolved";
                    s.outbox=s.outbox.filter(e=>e.id!==current.id && !(current.type==="conflict.resolve"&&e.id===ack.resolved_event_id));
                    const sh=s.shifts[current.actor_key];
                    if(current.type==="shift.open"&&sh?.local_shift_id===current.local_shift_id&&s.receipts[current.id].server_shift_id)
                        sh.server_id=s.receipts[current.id].server_shift_id;
                    s.sync.needs_reconcile=true;
                    if(ack.snapshot && (!ack.snapshot.actor || s.actor?.key===ack.snapshot.actor.key))applySnapshot(s,ack.snapshot,busy());
                    return true;
                }
                current.status=valid && ["auth","conflict"].includes(ack.status)?ack.status:"retry";
                current.last_error=String(ack?.message||"Сервер не подтвердил подписанное действие. Оно сохранено.");
                if(s.last_activation?.id===current.id){s.last_activation.status=current.status==="retry"?"queued":current.status;s.last_activation.message=current.last_error;}
                current.next_retry_at=Date.now()+Math.min(60000,2000*2**Math.min(current.attempts,5));
                if(valid && current.status==="conflict" && ack.snapshot && (!ack.snapshot.actor || s.actor?.key===ack.snapshot.actor.key))applySnapshot(s,ack.snapshot,true);
                if(valid && current.status==="conflict")tx.objectStore("confirmations").put({id:current.id,status:"conflict",code:ack.code,message:current.last_error});
                return false;
            });
            if(!more)break;
        }
        return read();
    }
    async function flush(send=sender){
        if(typeof send!=="function")return read();
        sender=send;
        if(leaving)return read();
        if(!draining){
            // A browser lock is automatically released when WebView discards its
            // document. Its next owner can immediately reclaim that abandoned lease.
            const work=win.navigator?.locks?.request?
                win.navigator.locks.request("cr-master-signed-outbox",{ifAvailable:true},lock=>lock?drain(send,true):read()):drain(send);
            draining=work.finally(()=>{activeOwner=null;draining=null;scheduleRetry();});
        }
        return draining;
    }
    async function scheduleRetry(){
        clearTimeout(timer);
        if(!sender||leaving)return;
        const s=await read();
        let head=s.outbox[0];
        if(head?.status==="conflict")head=s.outbox.find(e=>e.type==="conflict.resolve"&&e.payload.event_id===head.id);
        if(head&&!["auth","conflict"].includes(head.status))timer=setTimeout(()=>flush().catch(()=>{}),Math.max(1200,Math.min(60000,(head.next_retry_at||0)-Date.now())));
    }
    function wake(){if(sender)setTimeout(()=>flush().catch(()=>{}),0);}
    win.addEventListener("pagehide",()=>{
        leaving=true;clearTimeout(timer);const owner=activeOwner;
        if(owner)stateTx(true,s=>{
            if(s.lease?.owner!==owner)return;
            s.lease=null;
            const last=s.outbox.find(e=>e.status==="sending");
            if(last){last.status="retry";last.next_retry_at=0;}
        }).catch(()=>{});
    });
    win.addEventListener("pageshow",()=>{leaving=false;wake();});
    if(win.document)win.document.addEventListener("visibilitychange",()=>{if(!win.document.hidden)wake();});
    async function retry(actorKey=null,generation=null){
        await stateTx(true,s=>{for(const e of s.outbox){
            if(e.status==="retry")e.next_retry_at=0;
            if(e.status==="auth" && actorKey===e.actor_key && generation===e.actor_generation){e.status="pending";e.next_retry_at=0;}
        }});return flush();
    }
    async function lookup(shelf,id){
        const db=await database();return new Promise((resolve,reject)=>{
            const r=db.transaction(shelf).objectStore(shelf).get(id);
            r.onsuccess=()=>resolve(clone(r.result));r.onerror=()=>reject(r.error);
        });
    }
    const journal=id=>lookup("journal",id);
    win.FieldLedger={read,ingest,reconcile,cacheDetails,selectActor,revokeActor,lock,openShift,closeShift,command,resolveConflict,importDraft,
        saveDraft:(p,rev)=>savePlan("draft",p,rev),saveLive:p=>savePlan("live",p),applyDraft,flush,retry,journal,
        confirmation:id=>lookup("confirmations",id),remoteSnapshot:()=>stateTx(false,s=>s.deferred),deviceIdentity,
        signSnapshotRead:(actor,nonce,time)=>signRead(actor,nonce,time,"snapshot.read"),
        signReportRead:(actor,nonce,time,path)=>signRead(actor,nonce,time,"report.read",{path}),
        setBusy:fn=>{busy=typeof fn==="function"?fn:()=>false;},
        // Tickets are scoped to the exact actor and generation, independently of today's cookie.
        putTicket:(key,generation,ticket)=>secret("ticket:"+key+":"+generation,ticket),
        getTicket:(key,generation)=>secret("ticket:"+key+":"+generation),Error:error};
})(window);
