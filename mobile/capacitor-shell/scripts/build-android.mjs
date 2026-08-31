import { copyFileSync, existsSync, mkdirSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const profile = process.argv[2];
const buildType = process.argv[3] || "debug";
const assembleOnly = process.argv.includes("--assemble-only");

if (!profile || !/^[a-z][a-z0-9_]*$/.test(profile)) {
  throw new Error("Usage: node scripts/build-android.mjs <profile> [debug|release]");
}
if (!existsSync(join(root, "profiles", profile, "app.properties"))) {
  throw new Error(`Unknown application profile: ${profile}`);
}
if (!new Set(["debug", "release"]).has(buildType)) {
  throw new Error(`Unsupported build type: ${buildType}`);
}

function run(command, args, cwd = root) {
  const result = spawnSync(command, args, { cwd, stdio: "inherit", shell: false });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(`${command} exited with code ${result.status}`);
  }
}

if (!assembleOnly) {
  run(process.execPath, [join(root, "node_modules", "@capacitor", "cli", "dist", "index.js"), "sync", "android"]);
}

const variant = profile[0].toUpperCase() + profile.slice(1) + buildType[0].toUpperCase() + buildType.slice(1);
if (process.platform === "win32") {
  const result = spawnSync("cmd.exe", ["/d", "/s", "/c", `gradlew.bat assemble${variant}`], {
    cwd: join(root, "android"),
    stdio: "inherit",
    shell: false,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`gradlew.bat exited with code ${result.status}`);
} else {
  run("./gradlew", [`assemble${variant}`], join(root, "android"));
}

const apk = join(root, "android", "app", "build", "outputs", "apk", profile, buildType, `app-${profile}-${buildType}.apk`);
if (!existsSync(apk)) {
  throw new Error(`Gradle completed, but APK was not found: ${apk}`);
}
const delivery = join(root, "dist", `${profile}-${buildType}.apk`);
mkdirSync(dirname(delivery), { recursive: true });
copyFileSync(apk, delivery);
console.log(`APK: ${delivery}`);
