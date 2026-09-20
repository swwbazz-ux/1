/* Экран водителя собран из нескольких файлов: разметка в шаблоне, стили и код —
   рядом в static. Тесты-контракты проверяют экран целиком, поэтому читают их как
   один источник. Порядок здесь тот же, в каком файлы подключает шаблон. */
const fs = require("node:fs");
const path = require("node:path");

const BACKEND_ROOT = path.resolve(__dirname, "..", "..", "..");

const DRIVER_SCREEN_SCRIPTS = [
    "driver-haptics-v1.js",
    "driver-native-push-v1.js",
    "driver-shift-fragment-v1.js",
    "driver-shift-gestures-v1.js",
    "driver-shift-voice-v1.js",
    "driver-shift-refresh-v1.js",
    "driver-shift-close-v1.js",
    "driver-shift-v1.js",
    "driver-self-heal-v1.js",
];

const DRIVER_SCREEN_FILES = [
    path.join(BACKEND_ROOT, "templates", "users", "driver_shift.html"),
    path.join(BACKEND_ROOT, "static", "css", "driver-shift-v1.css"),
    ...DRIVER_SCREEN_SCRIPTS.map((name) => path.join(BACKEND_ROOT, "static", "js", name)),
];

function driverScreenSource() {
    return DRIVER_SCREEN_FILES.map((file) => fs.readFileSync(file, "utf8")).join("\n");
}

module.exports = {driverScreenSource, DRIVER_SCREEN_FILES, DRIVER_SCREEN_SCRIPTS};
