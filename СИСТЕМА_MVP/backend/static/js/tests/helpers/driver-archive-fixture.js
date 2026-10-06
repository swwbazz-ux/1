const copy = value => JSON.parse(JSON.stringify(value));
const identity = {actor_id: 12, access_id: 7, role_code: 'driver', device_id: 'driver-archive-phone'};
const at = '2026-10-05T01:00:00.000Z';
function storage() {
    const values = new Map();
    return {values, getItem: key => values.get(key) || null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key)};
}
function raw(id, sequence, type = 'driver.assignment.accepted') {
    return {...identity, event_id: id, event_type: type, format_version: 1, sequence,
        shift_id: null, local_shift_id: 'open', equipment_id: 9, occurred_at: at, created_at: at,
        depends_on: [], context_snapshot: {}, payload: {local_shift_id: 'open'}};
}
function fixture(count = 1, rich = false) {
    const originals = [raw('open', 1, 'driver.shift.opened'),
        ...Array.from({length: count}, (_, i) => raw('action-' + i, i + 2))];
    if (rich) for (const [id, type, tripId] of [
        ['load', 'driver.trip.loaded', 'trip-1'], ['finish', 'driver.trip.manual_completed', 'trip-1'],
        ['load-cancelled', 'driver.trip.loaded', 'trip-2'], ['cancel', 'driver.trip.loaded.cancelled', 'trip-2'],
        ['stop-start', 'driver.downtime.started'], ['stop-end', 'driver.downtime.ended'],
    ]) originals.push({...raw(id, originals.length + 1, type), local_trip_id: tripId || null});
    originals.push(raw('close', originals.length + 1, 'driver.shift.closed'));
    // Actions after opening ACK refer to the server ID, without the local alias.
    originals.forEach((event, index) => {
        if (index && index % 2 === 0) { event.shift_id = 99; event.local_shift_id = null; event.payload = {}; }
    });
    const trip = {trip_id: 101, truck_id: 9, status: 'completed', loaded_at: at,
        completed_at: '2026-10-05T01:20:00.000Z', cancelled_at: null, is_carryover: false,
        driver_control_shift_id: 99, unloading_shift_id: 99, credited_shift_id: 99,
        excavator_id: 3, excavator: 'ЭКГ-12', dump_point_id: 4, dump_point: 'Дробилка',
        volume_m3: '49.40', load_time_source: 'driver_device', unload_time_source: 'driver_device'};
    const stop = {downtime_id: 501, equipment_id: 9, employee_id: 12, reason_id: 4, reason: 'Обед',
        started_at: at, ended_at: '2026-10-05T01:10:00.000Z', shift_seconds: 600};
    const entries = originals.map((event, index) => {
        const tripFact = event.event_type.startsWith('driver.trip.')
            ? event.local_trip_id === 'trip-1' ? copy(trip) : {...copy(trip), trip_id: 102, status: 'cancelled', completed_at: null, volume_m3: null}
            : null;
        const downtimeFact = event.event_type.startsWith('driver.downtime.') ? copy(stop) : null;
        return {event: {...copy(event), sent_live: true}, status: 'accepted', receipt_id: index + 1,
            fingerprint: (index + 1).toString(16).padStart(64, '0'),
            result: {server_ids: {event_receipt_id: index + 1, shift_id: 99,
                ...(tripFact ? {trip_id: tripFact.trip_id} : {}), ...(downtimeFact ? {downtime_event_id: 501} : {})}},
            trip_fact: tripFact, downtime_fact: downtimeFact};
    });
    const events = originals.map((event, index) => ({...copy(event), state: 'confirmed', server_result: copy(entries[index].result)}));
    const proof = {ok: true, schema_version: 1, snapshot_id: 'a'.repeat(64), generated_at: '2026-10-05T03:00:00.000Z',
        identity: copy(identity), event_count: entries.length, entries,
        shift: {local_shift_id: 'open', open_event_id: 'open', close_event_id: 'close', server_shift_id: 99,
            equipment_id: 9, opened_at: at, closed_at: '2026-10-05T02:00:00.000Z', readings: {end_mileage: '12040.00'}},
        projection: {source_event_ids: originals.map(event => event.event_id), source_trip_ids: rich ? [101, 102] : [],
            source_downtime_ids: rich ? [501] : [], completed_trip_count: rich ? 1 : 0, cancelled_trip_count: rich ? 1 : 0,
            credited_trip_count: rich ? 1 : 0, credited_volume_m3: rich ? '49.40' : '0',
            unknown_volume_trip_count: 0, downtime_seconds: rich ? 600 : 0}};
    return {events, proof, store: storage()};
}
function responder(proof, observe = () => {}) {
    return async (url, options) => {
        const query = new URL(url), offset = Number(query.searchParams.get('offset'));
        observe(query, options);
        const end = Math.min(offset + 100, proof.entries.length);
        return {ok: true, json: async () => ({...copy(proof), offset, entries: copy(proof.entries.slice(offset, end)),
            next_offset: end < proof.entries.length ? end : null})};
    };
}

module.exports = {copy, identity, at, storage, raw, fixture, responder};
