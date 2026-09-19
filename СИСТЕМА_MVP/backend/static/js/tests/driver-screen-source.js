/* Экран водителя собран из трёх файлов: разметка в шаблоне, стили и код — рядом
   в static. Тесты-контракты проверяют экран целиком, поэтому читают их как один
   источник. */
const fs = require("node:fs");
const path = require("node:path");

const BACKEND_ROOT = path.resolve(__dirname, "..", "..", "..");

const DRIVER_SCREEN_FILES = [
    path.join(BACKEND_ROOT, "templates", "users", "driver_shift.html"),
    path.join(BACKEND_ROOT, "static", "css", "driver-shift-v1.css"),
    path.join(BACKEND_ROOT, "static", "js", "driver-shift-v1.js"),
];

function driverScreenSource() {
    return DRIVER_SCREEN_FILES.map((file) => fs.readFileSync(file, "utf8")).join("\n");
}

module.exports = {driverScreenSource, DRIVER_SCREEN_FILES};
