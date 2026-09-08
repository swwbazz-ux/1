import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const shellRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const driverTemplate = readFileSync(
  resolve(shellRoot, "..", "..", "СИСТЕМА_MVP", "backend", "templates", "users", "driver_shift.html"),
  "utf8"
);

test("Driver release keeps recorded voice calls wired to operational actions", () => {
  assert.match(driverTemplate, /function driverAppliedActionVoice\(actionKind, freshShell\)/);
  assert.doesNotMatch(driverTemplate, /function driverAppliedActionSound\(actionKind, freshShell\)/);

  for (const voice of [
    "voice_shift_opened",
    "voice_shift_closed",
    "voice_downtime_started",
    "voice_downtime_finished",
    "voice_action_failed",
    "voice_trip_finished",
    "voice_trip_finish_failed",
  ]) {
    assert.match(driverTemplate, new RegExp(`playDriverVoice\\([^)]*${voice}|voice:\\s*"${voice}"`));
  }

  assert.match(driverTemplate, /playDriverVoice\(actionVoice\.cue, actionVoice\.voice\)/);
  assert.match(driverTemplate, /announceEquipment\([\s\S]*?driver_excavator_assigned/);
  assert.match(driverTemplate, /announceDumpPoint\(details\)/);
});
