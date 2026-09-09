import { existsSync, readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { delimiter, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const profileName = process.argv[2];
const buildType = process.argv[3] || "debug";
const profileFile = join(root, "profiles", profileName || "", "app.properties");

if (!profileName || !existsSync(profileFile)) {
  throw new Error("Usage: node scripts/install-android.mjs <profile> [debug|release]");
}

const profile = Object.fromEntries(
  readFileSync(profileFile, "utf8")
    .split(/\r?\n/)
    .filter((line) => line && !line.startsWith("#"))
    .map((line) => {
      const separator = line.indexOf("=");
      return [line.slice(0, separator), line.slice(separator + 1)];
    })
);

const androidHome = process.env.ANDROID_HOME || process.env.ANDROID_SDK_ROOT;
if (!androidHome) throw new Error("ANDROID_HOME is not set");
const adb = join(androidHome, "platform-tools", process.platform === "win32" ? "adb.exe" : "adb");
const apk = join(root, "dist", `${profileName}-${buildType}.apk`);
if (!existsSync(adb)) throw new Error(`adb not found: ${adb}`);
if (!existsSync(apk)) throw new Error(`APK not found: ${apk}`);

function execute(args, capture = false) {
  const result = spawnSync(adb, args, {
    cwd: root,
    encoding: capture ? "utf8" : undefined,
    stdio: capture ? "pipe" : "inherit",
    env: { ...process.env, PATH: `${dirname(adb)}${delimiter}${process.env.PATH || ""}` },
  });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`adb ${args.join(" ")} exited with code ${result.status}`);
  return capture ? result.stdout : "";
}

const devices = execute(["devices"], true)
  .split(/\r?\n/)
  .slice(1)
  .map((line) => line.trim())
  .filter((line) => line.endsWith("\tdevice"));

if (devices.length !== 1) {
  throw new Error(`Expected exactly one authorized Android device, found ${devices.length}`);
}

execute(["install", "-r", "-t", apk]);
execute(["shell", "am", "force-stop", profile.applicationId]);
execute([
  "shell",
  "am",
  "start",
  "-n",
  `${profile.applicationId}/ru.copperresources.mobile.MainActivity`,
]);

console.log(`Installed ${profile.applicationId} on ${devices[0].split("\t")[0]}`);
console.log("After login, minimize the app and use the foreground notification action 'Тест сигнала'.");
