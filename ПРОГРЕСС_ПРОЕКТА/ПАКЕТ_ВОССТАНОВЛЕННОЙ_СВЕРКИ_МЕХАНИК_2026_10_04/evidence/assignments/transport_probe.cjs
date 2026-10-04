/* Diagnostic probe, not product acceptance. Executes the exact deployed JS in a VM.
   localStorage/fetch/timers are doubles; no browser, PostgreSQL or production request. */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const sourcePath = path.join(__dirname, 'source/static/js/dispatcher-transport-v1.js');
const bytes = fs.readFileSync(sourcePath);
const blob = crypto.createHash('sha1').update(Buffer.from(`blob ${bytes.length}\0`)).update(bytes).digest('hex');
assert.equal(blob, 'cb99c44ec9256d21181b5f54424438ff080de37e');
const queueKey = 'mining-master-mobile-sync-queue-v3';
function fixture({status=200, storageFails=false, networkFails=false, readonly=false}={}) {
  const storage = new Map(); const writes = []; const errors = []; const fetches = [];
  const window = {
    localStorage: {
      getItem(k) { return storage.get(k) || null; },
      setItem(k,v) { writes.push({key:k, failed:storageFails}); if(storageFails) throw new Error('QuotaExceededError'); storage.set(k,v); },
      removeItem(k) { storage.delete(k); }
    },
    isAppRoleReadonly: () => readonly,
    setTimeout: () => 1, clearTimeout: () => {},
  };
  vm.runInNewContext(bytes.toString('utf8'), {window}, {filename: sourcePath});
  const transport = window.createDispatcherTransport({
    fetch: async (url, options) => {
      fetches.push({url, durableQueueAtSend: JSON.parse(storage.get(queueKey)||'[]').length});
      if(networkFails) throw new Error('network offline');
      return {ok:status>=200 && status<300, status, json:async()=> status===200 ? {ok:true} : {error:'fixture server response', code:status===409?'state_conflict':'server_unavailable'}};
    },
    onServerError: error => errors.push({code:error.code, status:error.status})
  });
  const request={id:'stable-queue-id', kind:'json', url:'/fixture/assign/', data:{client_action_id:'original-command-id',truck_id:7,excavator_id:9,expected_assignment_state_id:11}};
  return {transport,request,storage,writes,errors,fetches};
}
const drain = async()=>{ for(let i=0;i<24;i++) await Promise.resolve(); };
(async()=>{
  const observations=[];
  for(const status of [409,503,200]) {
    const f=fixture({status}); assert.equal(f.transport.enqueue(f.request),true);
    assert.equal(f.transport.readQueue().length,1); f.transport.flush(); await drain();
    assert.equal(f.transport.readQueue().length,0);
    observations.push({scenario:`queued_http_${status}`,before:1,after:0,serverErrors:f.errors,verdict:status===200?'OBSERVED_POSITIVE_BASELINE':'OBSERVED_GAP',meaning:status===200?'success removes working request':'request removed on HTTP error; no receipt/archive is written by this module'});
  }
  {
    const f=fixture({networkFails:true}); f.transport.enqueue(f.request); f.transport.flush(); await drain();
    assert.equal(f.transport.readQueue().length,1);
    observations.push({scenario:'network_failure_retains_queue',after:1,attempts:f.transport.readQueue()[0].attempts,verdict:'OBSERVED_POSITIVE_BASELINE'});
  }
  {
    const f=fixture({storageFails:true}); const returned=f.transport.enqueue(f.request);
    assert.equal(returned,true); assert.equal(f.transport.readQueue().length,0);
    observations.push({scenario:'enqueue_storage_failure',returned,durableQueue:0,failedWrites:f.writes.length,verdict:'OBSERVED_GAP',meaning:'enqueue acknowledges true although storage write threw'});
  }
  {
    const f=fixture(); await f.transport.post(f.request.url,f.request.data);
    assert.equal(f.fetches[0].durableQueueAtSend,0); assert.equal(f.writes.length,0);
    observations.push({scenario:'initial_send_precedes_durable_write',fetches:f.fetches,writeCount:f.writes.length,verdict:'OBSERVED_GAP'});
  }
  const result={run_at_utc:new Date().toISOString(),source_commit:'ecb61af55b699a7a2417abba6df4dd490f3464a9',source_path:'СИСТЕМА_MVP/backend/static/js/dispatcher-transport-v1.js',git_blob_sha:blob,sha256:crypto.createHash('sha256').update(bytes).digest('hex'),runtime:process.version,scope:'actual whole JS module; VM with fetch/localStorage/timer doubles; diagnostic assertions',result:'6_DIAGNOSTIC_OBSERVATIONS_REPRODUCED',acceptance:'NOT_RUN_BROWSER_POSTGRESQL_DEVICE_FIELD',observations};
  fs.writeFileSync(path.join(__dirname,'transport_probe_results.json'),JSON.stringify(result,null,2)+'\n');
  process.stdout.write(JSON.stringify(result,null,2)+'\n');
})().catch(error=>{console.error(error);process.exitCode=1;});
