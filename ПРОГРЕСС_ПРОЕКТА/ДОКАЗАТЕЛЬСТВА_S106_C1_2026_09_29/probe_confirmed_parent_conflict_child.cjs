'use strict';
// Read-only invocation of the published Driver runtime; transport is mocked.
// Usage: node probe_confirmed_parent_conflict_child.cjs /absolute/repository/root
const assert = require('node:assert/strict');
const cp = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(process.argv[2] || '/workspace/scratch/a66293d272e2/p28-i2-r2-candidate');
const sourcePath = 'СИСТЕМА_MVP/backend/static/js/driver-offline-outbox-v2.js';
const release = '5721c045d665f5811fc8d343d7374575f386af66';
const expectedBlob = '0f7a0deef70c96e1e3169229f435615c25bf0620';
const git = (...args) => cp.execFileSync('git', ['-C', root, ...args], {encoding:'utf8'}).trim();
const bytes = fs.readFileSync(path.join(root, sourcePath));
const worktreeBlob = crypto.createHash('sha1').update(Buffer.from('blob ' + bytes.length + '\0')).update(bytes).digest('hex');
assert.equal(git('rev-parse', release + ':' + sourcePath), expectedBlob);
assert.equal(worktreeBlob, expectedBlob);
const {createDriverOfflineOutbox, localRepository} = require(path.join(root, sourcePath));
const values = new Map();
const local = {getItem:k=>values.get(k)||null, setItem:(k,v)=>values.set(k,String(v)), removeItem:k=>values.delete(k)};
const repo = localRepository(local, 7);
const context = {actorId:11,accessId:7,shiftId:23,equipmentId:58,deviceId:'install-uuid-1'};
function make(send) { return createDriverOfflineOutbox({repository:repo,localStorage:local,accessId:7,context,send}); }
(async () => {
  // This mocked receipt transcript prepares an explicitly stated client state;
  // it is NOT a claim that this batch was reproduced on the Django server.
  const first = make(async batch => ({results:batch.events.map(e=>({
    event_id:e.event_id, status:e.event_id==='p'?'accepted':'conflict',
    code:e.event_id==='p'?'':'dependency_rejected',
    server_ids:e.event_id==='p'?{downtime_event_id:123}:undefined
  }))}));
  await first.enqueue({event_id:'p',event_type:'driver.downtime.started',payload:{reason_id:9}});
  await first.enqueue({event_id:'c',event_type:'driver.downtime.ended',local_downtime_id:'p',depends_on:['p'],payload:{local_downtime_id:'p'}});
  await first.flush();
  const before = await repo.list();
  assert.equal(before.length, 1);
  assert.equal(before[0].event_id, 'c');
  assert.equal(before[0].state, 'conflict');
  assert.ok(await repo.getMeta('event-identity:p'));
  assert.deepEqual(await repo.getMeta('server-map:p'), {downtime_event_id:123});
  const sent = [];
  const second = make(async batch => {
    sent.push(...batch.events.map(e=>e.event_id));
    return {results:batch.events.map(e=>({event_id:e.event_id,status:'accepted'}))};
  });
  await second.initialize();
  const remaining = (await repo.list()).map(e=>({id:e.event_id,state:e.state,code:e.last_error?.code}));
  assert.deepEqual(sent, []);
  assert.deepEqual(remaining, [{id:'c',state:'conflict',code:'dependency_rejected'}]);
  console.log(JSON.stringify({
    probe:'confirmed-parent-conflict-child',
    method:'real published JS runtime; mocked transport; no backend execution',
    release_sha:release, checkout_sha:git('rev-parse','HEAD'), runtime_path:sourcePath,
    runtime_git_blob:worktreeBlob, node:process.version,
    parentStoredIdentity:true, parentServerMapping:await repo.getMeta('server-map:p'),
    sentAfterRestart:sent, remaining,
    result:'CONFIRMED_REMAINING_GAP: child is not resent; assertions verify current behavior, not desired acceptance'
  }, null, 2));
})().catch(error => { console.error(error); process.exitCode=1; });
