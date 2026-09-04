import { copyFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
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
  run(process.execPath, [join(root, "node_modules", "@capacitor", "cli", "bin", "capacitor"), "sync", "android"]);
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

if (buildType === "release") {
  const properties = Object.fromEntries(
    readFileSync(join(root, "profiles", profile, "app.properties"), "utf8")
      .split(/\r?\n/)
      .filter((line) => line && !line.startsWith("#"))
      .map((line) => {
        const separator = line.indexOf("=");
        return [line.slice(0, separator), line.slice(separator + 1)];
      })
  );
  const updaterEnabled = properties.inAppUpdaterEnabled !== "false";
  if (updaterEnabled) {
    const apkBaseUrl = properties.updateApkBaseUrl;
    if (!apkBaseUrl?.startsWith("https://")) {
      throw new Error(`updateApkBaseUrl must use HTTPS for ${profile}`);
    }
    const versionCode = Number.parseInt(properties.versionCode, 10);
    const versionName = properties.versionName?.trim();
    if (!/^\d+(?:\.\d+){2}(?:-[A-Za-z0-9][A-Za-z0-9._-]*)?$/.test(versionName || "")) {
      throw new Error(`versionName cannot be used in a public APK name for ${profile}`);
    }
    const appProfileId = properties.appProfileId || profile;
    const publicApkName = `${appProfileId}-${versionName}.apk`;
    const publicApk = join(root, "dist", publicApkName);
    copyFileSync(delivery, publicApk);
    console.log(`Public APK: ${publicApk}`);
    const manifest = {
      schemaVersion: 1,
      profile: appProfileId,
      versionCode,
      versionName,
      apkUrl: `${apkBaseUrl}${publicApkName}`,
      sha256: createHash("sha256").update(readFileSync(delivery)).digest("hex"),
      releaseNotes: "Исправления и улучшения рабочего приложения.",
    };
    const updateManifest = join(root, "dist", `${profile}-update.json`);
    writeFileSync(updateManifest, `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
    console.log(`Update manifest: ${updateManifest}`);
  }
}
