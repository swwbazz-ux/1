"use strict";
// Diagnostic execution of exact production JS; fake storage and transport, no browser/device.
const fs = require('fs'), path = require('path'), crypto = require('crypto'), assert = require('assert');
const base = __dirname;
const manifest = JSON.parse(fs.readFileSync(path.join(base, 'source-manifest.json')));
function verifiedSource(name) {
  const f = manifest.files.find(f => f.local === 'source/' + name);
  const b = fs.readFileSync(path.join(base, f.local));
  const hash = crypto.createHash('sha1').update(Buffer.concat([Buffer.from('blob '+b.length+'\0'), b])).digest('hex');
  assert.equal(hash, f.git_blob_sha);
  return require(path.join(base, f.local));
}
const {createDriverOfflineOutbox} = verifiedSource('driver-offline-outbox-v2.js');
const clone = value => JSON.parse(JSON.stringify(value));
function repo(seed=[]) {
  const rows = new Map(seed.map(e => [e.event_id, clone(e)]));
  const meta = new Map(), calls=[];
  return {kind:'diagnostic-memory-double',rows,meta,calls,
    async list(){return [...rows.values()].map(clone);},
    async get(id){return rows.has(id)?clone(rows.get(id)):undefined;},
    async put(e){calls.push(['put',e.event_id]);rows.set(e.event_id,clone(e));},
    async remove(id){calls.push(['remove',id]);rows.delete(id);},
    async getMeta(k){return meta.get(k);},async setMeta(k,v){calls.push(['meta',k]);meta.set(k,clone(v));}};
}
const context={actorId:1,accessId:1,equipmentId:3,shiftId:4,deviceId:'probe-device-1'};
const spec=id=>({event_id:id,event_type:'driver.downtime.started',occurred_at:'2026-10-03T10:00:00Z',payload:{reason_id:1}});
(async()=>{
  const observations=[];
  let callbacks=0;
  const quotaStorage={getItem(){return null;},setItem(){throw new Error('QuotaExceededError');}};
  const quota=createDriverOfflineOutbox({accessId:1,context,localStorage:quotaStorage,onState(){callbacks++;},send:async()=>({results:[]})});
  let rejection='';try{await quota.enqueue(spec('probe-quota'));}catch(e){rejection=e.message;}
  assert.equal(rejection,'QuotaExceededError');assert.equal(callbacks,0);
  observations.push({case:'durable-write-failure',observed:'enqueue rejects before onState',rejection,callbacks,status:'PASS_ISOLATED_FUNCTION'});
  const staleRepo=repo([{...spec('probe-old-conflict'),actor_id:1,access_id:1,role_code:'driver',device_id:context.deviceId,shift_id:4,equipment_id:3,sequence:1,depends_on:[],state:'conflict',updated_at:new Date(Date.now()-25*3600000).toISOString(),last_error:{code:'trip_already_completed'}}]);
  const stale=createDriverOfflineOutbox({accessId:1,context,repository:staleRepo,send:async()=>({results:[]})});
  await stale.initialize();
  assert.equal(staleRepo.rows.size,0);assert(staleRepo.calls.some(c=>c[0]==='remove'));assert.equal(staleRepo.meta.size,0);
  observations.push({case:'terminal-retention',observed:'25-hour terminal event removed from working storage; outbox writes no archive or identity receipt',calls:staleRepo.calls,status:'COUNTEREXAMPLE_REPRODUCED',scope:'outbox only; no assertion that server receipt itself is deleted'});
  const futureRepo=repo();
  const future=createDriverOfflineOutbox({accessId:1,context,repository:futureRepo,send:async b=>({results:b.events.map(e=>({event_id:e.event_id,status:'no_effect'}))})});
  await future.enqueue(spec('probe-future-noeffect'));await future.flush();
  const pending=await future.pending();assert.equal(pending.length,1);assert.equal(pending[0].state,'pending');
  observations.push({case:'future-no-effect-wire',observed:pending.map(e=>({state:e.state,attempt_count:e.attempt_count,last_error:e.last_error})),status:'FUTURE_PROTOCOL_INCOMPATIBILITY',scope:'synthetic future response; current production server is not claimed to emit literal no_effect'});
  const hungRepo=repo();let sends=0;
  const hung=createDriverOfflineOutbox({accessId:1,context,repository:hungRepo,send:()=>{sends++;return new Promise(()=>{});}});
  await hung.enqueue(spec('probe-hung-send'));const first=hung.flush();
  const state=await Promise.race([first.then(()=> 'settled'),new Promise(resolve=>setTimeout(()=>resolve('pending-after-25ms'),25))]);
  const second=hung.flush();assert.equal(second,first);assert.equal(sends,1);assert.equal(hungRepo.rows.size,1);
  observations.push({case:'unsettled-transport',observed:state,sends,secondFlushReturnsSamePromise:true,storedRows:hungRepo.rows.size,status:'BOUNDED_TRANSPORT_DOUBLE_OBSERVATION',scope:'does not measure real transport timeout; outbox delegates completion to send'});
  const result={release:manifest.release,scope:'Actual production JS; Node, memory/localStorage doubles. Not IndexedDB, browser, PostgreSQL or installed-device acceptance.',observations};
  fs.writeFileSync(path.join(base,'probe-result.json'),JSON.stringify(result,null,2)+'\n');
  console.log(JSON.stringify(result,null,2));
})().catch(e=>{console.error(e);process.exitCode=1;});
