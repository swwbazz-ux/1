const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const backend = process.env.P28_BACKEND;
if (!backend) throw new Error('P28_BACKEND is required');
const sourcePath = path.join(backend, 'static/js/driver-shift-voice-v1.js');
const source = fs.readFileSync(sourcePath, 'utf8');

function extract(name, nextMarker) {
  const start = source.indexOf(`function ${name}(`);
  const end = source.indexOf(nextMarker, start + 1);
  assert.ok(start >= 0 && end > start, `cannot extract ${name}`);
  return source.slice(start, end);
}

test('real production guard suppresses duplicate and later same-trip version after finalize', async () => {
  const announcements = [];
  const claimed = new Set();
  const context = {
    console,
    navigator: { vibrate() {} },
    window: {
      DriverVoiceGuard: {
        claim(key) { if (claimed.has(key)) return false; claimed.add(key); return true; },
      },
      MobileOperationalSounds: {
        announceDumpPoint(point, meta) {
          announcements.push({ point, meta });
          return Promise.resolve({ supported: true, announced: true });
        },
      },
    },
    settleDriverVoiceClaim() {},
    reportDriverAudioDiagnostic() {},
    playDriverSound() { return true; },
    setTimeout,
    clearTimeout,
  };
  vm.createContext(context);
  // Independent supplement: execute unchanged production guard and settlement.
  const guardStart = source.indexOf('window.DriverVoiceGuard = (function () {');
  const guardEnd = source.indexOf('})();', guardStart) + 5;
  assert.ok(guardStart >= 0 && guardEnd > guardStart);
  vm.runInContext(source.slice(guardStart, guardEnd) +
    extract('settleDriverVoiceClaim', '/* Единственная точка обращения к мосту'), context);
  const actualClaim = context.window.DriverVoiceGuard.claim;
  const observedKeys = [];
  context.window.DriverVoiceGuard.claim = (key) => {
    observedKeys.push(key);
    return actualClaim(key);
  };
  vm.runInContext(
    extract('latestDriverDumpPointEvent', 'function playDriverDumpPointAlert(') +
    extract('playDriverDumpPointAlert', 'window.addEventListener("operational-state-refresh-applied"'),
    context,
  );

  const first = { type: 'trip_changed', version: 10, payload: { action: 'truck_loaded', trip_id: 501, dump_point_id: 1, dump_point_name: 'P1' } };
  const repeated = { type: 'trip_changed', version: 10, payload: { action: 'truck_loaded', trip_id: 501, dump_point_id: 1, dump_point_name: 'P1' } };
  const sameTripNewVersion = { type: 'trip_changed', version: 11, payload: { action: 'truck_loaded', trip_id: 501, dump_point_id: 2, dump_point_name: 'P2' } };
  const pointOnly = { type: 'trip_changed', version: 12, payload: { action: 'driver_dump_point_changed', trip_id: 501, dump_point_id: 3, dump_point_name: 'P3' } };

  assert.equal(context.latestDriverDumpPointEvent({ events: [pointOnly] }), null);
  context.playDriverDumpPointAlert({ events: [first] });
  context.playDriverDumpPointAlert({ events: [repeated] });
  context.playDriverDumpPointAlert({ events: [sameTripNewVersion] });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(context.latestDriverDumpPointEvent({ events: [first, repeated, sameTripNewVersion] }).eventVersion, 11);
  assert.equal(context.window.DriverVoiceGuard.state('dump:501'), 'announced');
  context.playDriverDumpPointAlert({ events: [sameTripNewVersion] });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(observedKeys, ['dump:501', 'dump:501', 'dump:501', 'dump:501']);
  assert.equal(announcements.length, 1);
  assert.equal(announcements[0].point.dumpPointName, 'P1');
  console.log('P28_VOICE_EVIDENCE ' + JSON.stringify({ announcements, pointOnlySelected: false, dedupeKey: 'dump:501' }));
});

