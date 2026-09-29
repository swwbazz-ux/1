const assert = require('node:assert/strict');
const test = require('node:test');
const createLedger = require('./source/СИСТЕМА_MVP/backend/static/js/excavator-local-shift-v1.js');
const clone = x => JSON.parse(JSON.stringify(x));
function memory() { let state=null; return {read:async()=>clone(state),write:async x=>{state=clone(x)},inspect:()=>clone(state)}; }
function make(options={}) { return createLedger({adapter:memory(),outbox:{confirmed:async()=>[],queue:async e=>({...e,sync_state:'pending'})},accessId:7,actorId:12,roleCode:'excavator_operator',deviceId:'D1',...options}); }
function open() { return {event_id:'shift1',event_type:'excavator.shift.opened',local_shift_id:'shift1',actor_id:12,access_id:7,role_code:'excavator_operator',device_id:'D1',equipment_id:7,sequence:1,occurred_at:'2026-09-29T00:00:00Z',payload:{local_shift_id:'shift1',engine_hours:'1200',fuel:'100'}}; }
function load(id,at) { return {event_id:id,event_type:'excavator.trip.loaded',local_shift_id:'shift1',actor_id:12,access_id:7,role_code:'excavator_operator',device_id:'D1',equipment_id:7,sequence:2,local_trip_id:'local-'+id,depends_on:['shift1'],occurred_at:at,payload:{local_shift_id:'shift1',truck_id:17,dump_point_id:4,dump_point_name:'P1',local_fleet_code:'belaz',local_volume_m3:'49.4'}}; }
test('FR1: cached 00:05 report must include durable 00:06 load viewed at 00:15',async()=>{
 const l=make(); await l.recordAndQueue(open());
 const cached=await l.hourlyReport(null,Date.parse('2026-09-29T00:05:00Z'));
 await l.recordAndQueue(load('L1','2026-09-29T00:06:00Z'));
 const result=await l.hourlyReport(cached,Date.parse('2026-09-29T00:15:00Z'));
 console.log('FR1',JSON.stringify({period:result.hours[0].period,trip_count:result.hours[0].totals.trip_count,local_facts:(await l.facts()).length}));
 assert.equal(result.hours[0].totals.trip_count,1);
});
test('FR2: server projection arriving before lost load receipt must not double count',async()=>{
 const l=make(); const o=open();const e=load('L2','2026-09-29T00:06:00Z'); await l.recordAndQueue(o);
 await l.confirm(o,{server_ids:{shift_id:81}}); await l.recordAndQueue(e);
 // Server commits L2 as Trip 901; HTTP receipt is lost. Refresh already contains that Trip.
 const result=await l.shiftSummary({trip_count:1,volume_m3:49.4,source_trip_ids:[901]});
 const server=await l.hourlyReport(null,Date.parse('2026-09-29T00:15:00Z'));
 server.hours[0].source_trip_ids=[901]; // real server reports one Trip; local event still has no returned trip_id
 const hourly=await l.hourlyReport(server,Date.parse('2026-09-29T00:15:00Z'));
 console.log('FR2',JSON.stringify({summary:result,hourly:hourly.hours[0].totals,local_fact:(await l.facts())[0]}));
 assert.equal(hourly.hours[0].totals.trip_count,1);
});
test('FR3: failed durable write must not leave an unsaved open shift in authoritative memory',async()=>{
 const adapter={read:async()=>null,write:async()=>{throw Error('quota exceeded')}};
 const l=make({adapter}); await l.ready();await assert.rejects(l.recordAndQueue(open()),/quota/);
 console.log('FR3',JSON.stringify({current:l.currentShift()}));
 assert.equal(l.currentShift(),null);
});
test('FR4: unavailable transport confirmation storage must not block healthy independent ledger restart',async()=>{
 const adapter=memory();const first=make({adapter}); await first.recordAndQueue(open());
 const restarted=make({adapter,outbox:{confirmed:async()=>{throw Error('outbox IDB unavailable')},queue:async()=>{throw Error('outbox IDB unavailable')}}});
 let error;try{await restarted.ready()}catch(e){error=e.message};
 console.log('FR4',JSON.stringify({readyError:error||null,saved_shift:adapter.inspect().current_local_shift_id}));
 assert.equal(error,undefined);
});
