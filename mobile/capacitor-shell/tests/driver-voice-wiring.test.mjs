import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const shellRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const backendRoot = resolve(shellRoot, "..", "..", "СИСТЕМА_MVP", "backend");

// Разметка, стили и код экрана водителя разложены по отдельным файлам
// (СИСТЕМА_MVP/backend/static/js/tests/driver-screen-source.js держит их список
// для тестов бэкенда) — здесь собираем тот же набор, чтобы регулярки видели
// код независимо от того, в каком из файлов он сейчас лежит.
const driverTemplate = [
  resolve(backendRoot, "templates", "users", "driver_shift.html"),
  resolve(backendRoot, "static", "js", "driver-shift-fragment-v1.js"),
  resolve(backendRoot, "static", "js", "driver-shift-gestures-v1.js"),
  resolve(backendRoot, "static", "js", "driver-shift-voice-v1.js"),
  resolve(backendRoot, "static", "js", "driver-shift-refresh-v1.js"),
  resolve(backendRoot, "static", "js", "driver-shift-close-v1.js"),
  resolve(backendRoot, "static", "js", "driver-shift-v1.js"),
]
  .map((file) => readFileSync(file, "utf8"))
  .join("\n");

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
  assert.match(driverTemplate, /function playDriverReleaseOfferCue\(assignmentId\)[\s\S]*?playDriverSound\("assignment_removed_notice"\)/);
});
