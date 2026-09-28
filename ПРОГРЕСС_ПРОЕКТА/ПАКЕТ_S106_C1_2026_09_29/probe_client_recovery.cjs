'use strict';

const assert = require('node:assert/strict');
const cp = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(process.argv[2] || '.');
const label = String(process.argv[3] || '');
const variants = {
    release: {
        sha: '8c215ae2a45196e3e74e566c54f2ab5cb321be02',
        driverBlob: '0f7a0deef70c96e1e3169229f435615c25bf0620',
        excavatorBlob: '9dbe739b304606e2e0172c006ab777ff3e5a7169',
        expected: {
            driverConfirmedParent: [],
            driverForeignParent: [],
            driverBucketChain: ['fb-load', 'fb-complete'],
            excavatorConfirmedParent: [],
            excavatorForeignParent: [],
        },
    },
    pr106: {
        sha: '17cbb3e913dfdc98aaa9585ba4090d0f4cf17ee0',
        driverBlob: 'b4ec7303651624af0670d90ccee9bc187538176b',
        excavatorBlob: '74f27be85b868cf23b07466630c09eb166949b4f',
        expected: {
            driverConfirmedParent: ['c'],
            driverForeignParent: ['foreign-child'],
            driverBucketChain: ['fb-complete'],
            excavatorConfirmedParent: ['ec'],
            excavatorForeignParent: ['exc-foreign-child'],
        },
    },
};
assert.ok(variants[label], 'second argument must be release or pr106');
const variant = variants[label];

const driverSource = 'СИСТЕМА_MVP/backend/static/js/driver-offline-outbox-v2.js';
const excavatorSource = 'СИСТЕМА_MVP/backend/static/js/excavator-field-outbox-v1.js';
const git = (...args) => cp.execFileSync('git', ['-C', root, ...args], {encoding: 'utf8'}).trim();
function workingSha256(file) {
    const bytes = fs.readFileSync(path.join(root, file));
    return crypto.createHash('sha256').update(bytes).digest('hex');
}
assert.equal(git('rev-parse', 'HEAD'), variant.sha);
assert.equal(git('rev-parse', `HEAD:${driverSource}`), variant.driverBlob);
assert.equal(git('rev-parse', `HEAD:${excavatorSource}`), variant.excavatorBlob);

const {createDriverOfflineOutbox, localRepository} = require(path.join(root, driverSource));
const createExcavatorOutbox = require(path.join(root, excavatorSource));

function storage() {
    const values = new Map();
    return {
        getItem: key => values.has(key) ? values.get(key) : null,
        setItem: (key, value) => values.set(key, String(value)),
        removeItem: key => values.delete(key),
    };
}

const driverContext = {
    actorId: 11, accessId: 7, shiftId: 23, equipmentId: 58, deviceId: 'install-uuid-1',
};
function driverRuntime(local, send) {
    return createDriverOfflineOutbox({
        repository: localRepository(local, 7), localStorage: local, accessId: 7,
        context: driverContext, send,
    });
}
async function driverScenario(name, events, firstResult) {
    const local = storage();
    const first = driverRuntime(local, async batch => ({
        results: batch.events.map(event => firstResult(event)),
    }));
    for (const event of events) await first.enqueue(event);
    await first.flush();
    const before = (await first.pending()).map(event => ({
        id: event.event_id, state: event.state, code: event.last_error && event.last_error.code,
    }));
    const sent = [];
    const restarted = driverRuntime(local, async batch => {
        sent.push(...batch.events.map(event => event.event_id));
        return {results: batch.events.map(event => ({event_id: event.event_id, status: 'accepted'}))};
    });
    await restarted.initialize();
    const remaining = (await restarted.pending()).map(event => ({
        id: event.event_id, state: event.state, code: event.last_error && event.last_error.code,
    }));
    return {name, before, sent, remaining};
}

function excavatorEvent(id, sequence, type = 'excavator.downtime.started', dependsOn = []) {
    return {
        event_id: id,
        event_type: type,
        format_version: 1,
        occurred_at: `2026-09-29T00:00:0${sequence}.000Z`,
        sequence,
        depends_on: dependsOn,
        actor_id: 17,
        access_id: 7,
        role_code: 'excavator_operator',
        device_id: 'device-1',
        shift_id: 31,
        equipment_id: 5,
        local_downtime_id: type.endsWith('.ended') ? dependsOn[0] : id,
        payload: type.endsWith('.ended') ? {local_downtime_id: dependsOn[0]} : {reason_id: sequence + 10},
    };
}
async function excavatorScenario(name, events, firstResult) {
    const local = storage();
    const first = createExcavatorOutbox({
        localStorage: local, queueKey: `access-7-${name}`,
        send: async batch => ({ok: true, results: batch.map(event => firstResult(event))}),
    });
    for (const event of events) await first.queue(event);
    await first.flush();
    const before = (await first.pending()).map(event => ({
        id: event.event_id, state: event.sync_state, code: event.last_error_code,
    }));
    const sent = [];
    const restarted = createExcavatorOutbox({
        localStorage: local, queueKey: `access-7-${name}`,
        send: async batch => {
            sent.push(...batch.map(event => event.event_id));
            return {ok: true, results: batch.map(event => ({event_id: event.event_id, status: 'accepted'}))};
        },
    });
    await restarted.ready();
    await restarted.flush();
    const remaining = (await restarted.pending()).map(event => ({
        id: event.event_id, state: event.sync_state, code: event.last_error_code,
    }));
    return {name, before, sent, remaining};
}

(async () => {
    const driverConfirmedParent = await driverScenario(
        'driver-confirmed-parent',
        [
            {event_id: 'p', event_type: 'driver.downtime.started', payload: {reason_id: 9}},
            {event_id: 'c', event_type: 'driver.downtime.ended', local_downtime_id: 'p', depends_on: ['p'], payload: {local_downtime_id: 'p'}},
        ],
        event => event.event_id === 'p'
            ? {event_id: event.event_id, status: 'accepted', server_ids: {downtime_event_id: 123}}
            : {event_id: event.event_id, status: 'conflict', code: 'dependency_rejected'},
    );
    const driverForeignParent = await driverScenario(
        'driver-foreign-parent',
        [
            {event_id: 'foreign-parent', event_type: 'driver.downtime.started', payload: {reason_id: 9}},
            {event_id: 'foreign-child', event_type: 'driver.downtime.started', depends_on: ['foreign-parent'], payload: {reason_id: 10}},
        ],
        event => ({
            event_id: event.event_id,
            status: 'conflict',
            code: event.event_id === 'foreign-parent' ? 'equipment_context_changed' : 'dependency_owner_mismatch',
        }),
    );
    const driverBucketChain = await driverScenario(
        'driver-free-bucket-chain',
        [
            {
                event_id: 'fb-load', event_type: 'driver.trip.loaded',
                occurred_at: '2026-09-28T13:00:00.000Z', local_trip_id: 'fb-load',
                payload: {
                    manual_control: true, truck_id: 58, excavator_id: 7,
                    dump_point_id: 3, rock_type_id: 2, assignment_id: null,
                    free_bucket_acceptance_id: null,
                    free_bucket_acceptance_local_id: 'fb-select-1',
                },
            },
            {
                event_id: 'fb-complete', event_type: 'driver.trip.manual_completed',
                occurred_at: '2026-09-28T13:05:00.000Z', local_trip_id: 'fb-load',
                depends_on: ['fb-load'],
                payload: {manual_control: true, truck_id: 58, excavator_id: 7, dump_point_id: 3},
            },
        ],
        event => ({
            event_id: event.event_id,
            status: 'conflict',
            code: event.event_id === 'fb-load' ? 'free_bucket_not_available' : 'dependency_rejected',
        }),
    );
    const excavatorConfirmedParent = await excavatorScenario(
        'excavator-confirmed-parent',
        [
            excavatorEvent('ep', 1),
            excavatorEvent('ec', 2, 'excavator.downtime.ended', ['ep']),
        ],
        event => event.event_id === 'ep'
            ? {event_id: event.event_id, status: 'accepted', server_ids: {downtime_event_id: 456}}
            : {event_id: event.event_id, status: 'conflict', code: 'dependency_rejected'},
    );
    const excavatorForeignParent = await excavatorScenario(
        'excavator-foreign-parent',
        [
            excavatorEvent('exc-foreign-parent', 1),
            excavatorEvent('exc-foreign-child', 2, 'excavator.downtime.started', ['exc-foreign-parent']),
        ],
        event => ({
            event_id: event.event_id,
            status: 'conflict',
            code: event.event_id === 'exc-foreign-parent' ? 'equipment_context_changed' : 'dependency_owner_mismatch',
        }),
    );

    const observed = {
        driverConfirmedParent: driverConfirmedParent.sent,
        driverForeignParent: driverForeignParent.sent,
        driverBucketChain: driverBucketChain.sent,
        excavatorConfirmedParent: excavatorConfirmedParent.sent,
        excavatorForeignParent: excavatorForeignParent.sent,
    };
    assert.deepEqual(observed, variant.expected);
    console.log(JSON.stringify({
        result: 'PASS_CURRENT_BEHAVIOR',
        label,
        sha: variant.sha,
        node: process.version,
        runtime_blobs: {driver: variant.driverBlob, excavator: variant.excavatorBlob},
        working_sha256: {driver: workingSha256(driverSource), excavator: workingSha256(excavatorSource)},
        observed,
        scenarios: [
            driverConfirmedParent, driverForeignParent, driverBucketChain,
            excavatorConfirmedParent, excavatorForeignParent,
        ],
        target_gaps: label === 'release'
            ? ['confirmed parent is absent from queue so child is not retried', 'foreign/rejected parent keeps child terminal']
            : ['free_bucket_not_available root is not retried while its child is sent alone'],
    }, null, 2));
})().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
