import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function profile(name) {
  return Object.fromEntries(
    readFileSync(resolve(root, "profiles", name, "app.properties"), "utf8")
      .split(/\r?\n/)
      .filter((line) => line && !line.startsWith("#"))
      .map((line) => {
        const separator = line.indexOf("=");
        return [line.slice(0, separator), line.slice(separator + 1)];
      })
  );
}

const expectedProfiles = {
  excavator: {
    serverUrl: "https://excavator.driverform.ru/",
    startUrl: "https://excavator.driverform.ru/excavator/work/",
    applicationId: "ru.copperresources.excavator",
    appName: "Экскаваторщик",
    versionCode: "23",
    versionName: "0.1.17",
    splashBackgroundColor: "#02080b",
    splashAccentColor: "#FFD200",
    splashIconResource: "app_icon",
  },
  driver: {
    serverUrl: "https://driver.driverform.ru/",
    startUrl: "https://driver.driverform.ru/driver/",
    applicationId: "ru.copperresources.driver",
    appName: "Водитель",
    versionCode: "10",
    versionName: "0.1.8",
    splashBackgroundColor: "#02080b",
    splashAccentColor: "#8CFF2E",
    splashIconResource: "app_icon",
  },
  excavator_qa: {
    serverUrl: "https://qa-excavator.driverform.ru/",
    startUrl: "https://qa-excavator.driverform.ru/excavator/work/",
    applicationId: "ru.copperresources.excavator.qa",
    appName: "Экскаваторщик QA",
    versionCode: "4",
    versionName: "1.0.3-qa",
    splashBackgroundColor: "#02080b",
    splashAccentColor: "#FFD200",
    splashIconResource: "app_icon",
  },
  excavator_rustore: {
    serverUrl: "https://excavator.driverform.ru/",
    startUrl: "https://excavator.driverform.ru/excavator/work/",
    applicationId: "ru.copperresources.excavator",
    appName: "Экскаваторщик",
    versionCode: "24",
    versionName: "0.1.17",
    splashBackgroundColor: "#02080b",
    splashAccentColor: "#FFD200",
    splashIconResource: "app_icon",
  },
  excavator_rustore_qa: {
    serverUrl: "https://qa-excavator.driverform.ru/",
    startUrl: "https://qa-excavator.driverform.ru/excavator/work/",
    applicationId: "ru.copperresources.excavator",
    appName: "Экскаваторщик",
    versionCode: "23",
    versionName: "0.1.17-rc",
    splashBackgroundColor: "#02080b",
    splashAccentColor: "#FFD200",
    splashIconResource: "app_icon",
  },
};

for (const [profileName, expected] of Object.entries(expectedProfiles)) {
  test(`${profileName} build profile contains every role-specific parameter`, () => {
    const config = profile(profileName);
    assert.equal(config.serverUrl, expected.serverUrl);
    assert.equal(config.startUrl, expected.startUrl);
    assert.ok(config.startUrl.startsWith(config.serverUrl));
    assert.equal(config.applicationId, expected.applicationId);
    assert.equal(config.appName, expected.appName);
    assert.match(config.applicationId, /^[a-z][a-z0-9_.]+$/);
    assert.ok(config.heartbeatUrl.startsWith(config.serverUrl));
    assert.ok(Number(config.heartbeatIntervalSeconds) >= 15);
    if (config.inAppUpdaterEnabled === "false") {
      assert.equal(config.updateManifestUrl, "");
      assert.equal(config.updateApkBaseUrl, "");
    } else {
      assert.match(config.updateManifestUrl, /^https:\/\//);
      assert.match(config.updateApkBaseUrl, /^https:\/\//);
    }
    assert.ok(Number(config.updateCheckIntervalMinutes) >= 5);
    assert.ok(config.alertSoundResource);
    assert.ok(config.syncTokenEnv);
    assert.equal(config.versionCode, expected.versionCode);
    assert.equal(config.versionName, expected.versionName);
    assert.equal(config.splashBackgroundColor, expected.splashBackgroundColor);
    assert.equal(config.splashAccentColor, expected.splashAccentColor);
    assert.equal(config.splashIconResource, expected.splashIconResource);
  });
}

test("profiles remain isolated by URL and application id", () => {
  const excavator = profile("excavator");
  const driver = profile("driver");
  assert.notEqual(excavator.serverUrl, driver.serverUrl);
  assert.notEqual(excavator.applicationId, driver.applicationId);
  assert.notEqual(excavator.foregroundChannelId, driver.foregroundChannelId);
  assert.notEqual(excavator.alertChannelId, driver.alertChannelId);
});

test("QA and RuStore variants keep role identity but disable sideload updates", () => {
  const qa = profile("excavator_qa");
  const rustoreQa = profile("excavator_rustore_qa");
  const rustore = profile("excavator_rustore");
  assert.equal(qa.appProfileId, "excavator");
  assert.equal(rustoreQa.appProfileId, "excavator");
  assert.equal(rustore.appProfileId, "excavator");
  assert.equal(qa.resourceProfile, "excavator");
  assert.equal(rustoreQa.resourceProfile, "excavator");
  assert.equal(rustore.resourceProfile, "excavator");
  assert.equal(qa.inAppUpdaterEnabled, "false");
  assert.equal(rustoreQa.inAppUpdaterEnabled, "false");
  assert.equal(rustore.inAppUpdaterEnabled, "false");
  assert.notEqual(qa.applicationId, rustore.applicationId);
  assert.equal(rustoreQa.applicationId, rustore.applicationId);
  assert.notEqual(rustoreQa.serverUrl, rustore.serverUrl);
  assert.ok(Number(rustore.versionCode) > Number(rustoreQa.versionCode));
  assert.ok(Number(rustore.versionCode) > Number(profile("excavator").versionCode));
});

test("every excavator build embeds the complete loud sound pack", () => {
  const soundNames = [
    "excavator_truck_assigned.wav",
    "excavator_action_ok.wav",
    "excavator_action_error.wav",
    "excavator_connection_lost.wav",
    "excavator_connection_restored.wav",
    "excavator_shift_start.wav",
    "excavator_shift_end.wav",
  ];
  const nativeRaw = resolve(root, "profiles", "excavator", "res", "raw");
  const webAudio = resolve(root, "..", "..", "СИСТЕМА_MVP", "backend", "static", "audio", "excavator");
  for (const soundName of soundNames) {
    const nativeBytes = readFileSync(resolve(nativeRaw, soundName));
    const webBytes = readFileSync(resolve(webAudio, soundName));
    assert.equal(nativeBytes.subarray(0, 4).toString("ascii"), "RIFF");
    assert.equal(nativeBytes.subarray(8, 12).toString("ascii"), "WAVE");
    assert.deepEqual(nativeBytes, webBytes);
  }
  for (const profileName of ["excavator", "excavator_qa", "excavator_rustore_qa", "excavator_rustore"]) {
    const config = profile(profileName);
    assert.equal(config.resourceProfile || profileName, "excavator");
    assert.equal(config.alertSoundResource, "excavator_truck_assigned");
    assert.match(config.alertChannelId, /_v2$/);
  }

  const activity = readFileSync(resolve(root, "android", "app", "src", "main", "java", "ru", "copperresources", "mobile", "MainActivity.java"), "utf8");
  const plugin = readFileSync(resolve(root, "android", "app", "src", "main", "java", "ru", "copperresources", "mobile", "NativeSoundPlugin.java"), "utf8");
  assert.match(activity, /"excavator"\.equals\(BuildConfig\.APP_PROFILE_ID\)[\s\S]*?registerPlugin\(NativeSoundPlugin\.class\)/);
  assert.match(plugin, /@CapacitorPlugin\(name = "NativeSound"\)/);
  assert.match(plugin, /setVolume\(1\.0f, 1\.0f\)/);
  assert.match(plugin, /USAGE_ASSISTANCE_SONIFICATION/);
});

test("native builds expose an explicit keyboard close bridge", () => {
  const javaRoot = resolve(root, "android", "app", "src", "main", "java", "ru", "copperresources", "mobile");
  const activity = readFileSync(resolve(javaRoot, "MainActivity.java"), "utf8");
  const plugin = readFileSync(resolve(javaRoot, "NativeKeyboardPlugin.java"), "utf8");
  const imeWebView = readFileSync(resolve(javaRoot, "NativeImeWebView.java"), "utf8");
  const bridgeLayout = readFileSync(resolve(root, "android", "app", "src", "main", "res", "layout", "capacitor_bridge_layout_main.xml"), "utf8");

  assert.match(activity, /registerPlugin\(NativeKeyboardPlugin\.class\)/);
  assert.match(plugin, /@CapacitorPlugin\(name = "NativeKeyboard"\)/);
  assert.match(plugin, /hideSoftInputFromWindow\(webView\.getWindowToken\(\), 0\)/);
  assert.match(plugin, /setAction\(PluginCall call\)/);
  assert.match(bridgeLayout, /ru\.copperresources\.mobile\.NativeImeWebView/);
  assert.match(bridgeLayout, /android:id="@\+id\/webview"/);
  assert.match(imeWebView, /extends CapacitorWebView/);
  assert.match(imeWebView, /new InputConnectionWrapper\(inputConnection, false\)/);
  assert.match(imeWebView, /performEditorAction\(int actionCode\)/);
  assert.match(imeWebView, /native-ime-action/);
  assert.match(imeWebView, /return true;/);
});

test("rejected native phone handoff remains disabled", () => {
  const gradle = readFileSync(resolve(root, "android", "app", "build.gradle"), "utf8");
  const manifest = readFileSync(resolve(root, "android", "app", "src", "main", "AndroidManifest.xml"), "utf8");
  const javaRoot = resolve(root, "android", "app", "src", "main", "java", "ru", "copperresources", "mobile");
  const activity = readFileSync(resolve(javaRoot, "MainActivity.java"), "utf8");

  assert.match(gradle, /new URI\(rawValue\)/);
  assert.match(gradle, /startUrl must stay inside the exact serverUrl origin/);
  assert.doesNotMatch(gradle, /APP_LINK_|appLinkPath|manifestPlaceholders\.appLink/);
  assert.doesNotMatch(manifest, /android:autoVerify|android\.intent\.action\.VIEW|appLinkPath/);
  assert.doesNotMatch(activity, /NativeAppLink|resolveNativeAppLink|onNewIntent\(Intent intent\)/);
});

test("WebView cookies are accepted and flushed at every persistence boundary", () => {
  const activity = readFileSync(
    resolve(root, "android", "app", "src", "main", "java", "ru", "copperresources", "mobile", "MainActivity.java"),
    "utf8"
  );
  assert.match(activity, /setAcceptCookie\(true\)/);
  assert.match(activity, /setAcceptThirdPartyCookies\(webView, true\)/);
  assert.match(activity, /bridgeBuilder\.addWebViewListener\(new WebViewListener/);
  assert.match(activity, /onPageLoaded\(WebView loadedWebView\)[\s\S]*?CookieManager\.getInstance\(\)\.flush\(\)/);
  assert.match(activity, /onPause\(\)[\s\S]*?CookieManager\.getInstance\(\)\.flush\(\)/);
  assert.match(activity, /onStop\(\)[\s\S]*?CookieManager\.getInstance\(\)\.flush\(\)/);
  assert.doesNotMatch(activity, /remove(All|Session)Cookies|clearCookies/);
});

test("native implementation reads role data only from BuildConfig", () => {
  const javaRoot = resolve(root, "android", "app", "src", "main", "java", "ru", "copperresources", "mobile");
  const sources = ["MainActivity.java", "ConnectivityForegroundService.java", "AppNotifications.java", "AppUpdateManager.java"]
    .map((file) => readFileSync(resolve(javaRoot, file), "utf8"))
    .join("\n");
  assert.doesNotMatch(sources, /https:\/\/(excavator|driver)\.driverform\.ru/);
  assert.match(sources, /BuildConfig\.APP_SERVER_URL/);
  assert.match(sources, /BuildConfig\.APP_START_URL/);
  assert.match(sources, /BuildConfig\.HEARTBEAT_URL/);
  assert.match(sources, /BuildConfig\.SYNC_AUTH_TOKEN/);
  assert.match(sources, /BuildConfig\.UPDATE_MANIFEST_URL/);
});

test("startup splash is profile-driven and waits for stable rendered layout", () => {
  const gradle = readFileSync(resolve(root, "android", "app", "build.gradle"), "utf8");
  const styles = readFileSync(resolve(root, "android", "app", "src", "main", "res", "values", "styles.xml"), "utf8");
  const activity = readFileSync(
    resolve(root, "android", "app", "src", "main", "java", "ru", "copperresources", "mobile", "MainActivity.java"),
    "utf8"
  );
  const overlay = readFileSync(
    resolve(root, "android", "app", "src", "main", "java", "ru", "copperresources", "mobile", "StartupLoadingOverlay.java"),
    "utf8"
  );
  const watchdogBody = overlay.slice(
    overlay.indexOf("private void runStartupWatchdog("),
    overlay.indexOf("private void restartProbeIfReady()")
  );

  assert.match(gradle, /profile\.getProperty\('splashBackgroundColor'/);
  assert.match(gradle, /profile\.getProperty\('splashAccentColor'/);
  assert.match(gradle, /profile\.getProperty\('splashIconResource'/);
  assert.match(gradle, /resValue "drawable", "startup_splash_icon"/);
  assert.match(styles, /windowSplashScreenBackground/);
  assert.match(styles, /windowSplashScreenAnimatedIcon/);
  assert.match(styles, /postSplashScreenTheme/);
  assert.match(activity, /SplashScreen\.installSplashScreen\(this\)[\s\S]*?super\.onCreate/);
  assert.match(activity, /setKeepOnScreenCondition\(\(\) -> !nativeCoverReady\)/);
  assert.match(activity, /onPageStarted\(WebView loadingWebView\)[\s\S]*?startupLoadingOverlay\.onPageStarted/);
  assert.match(activity, /onPageLoaded\(WebView loadedWebView\)[\s\S]*?startupLoadingOverlay\.onPageLoaded/);
  assert.match(activity, /lastObservedPageState = PageState\.STARTED[\s\S]*?startupLoadingOverlay\.onPageStarted\(loadingWebView\)/);
  assert.match(activity, /lastObservedPageState = PageState\.LOADED[\s\S]*?startupLoadingOverlay\.onPageLoaded\(loadedWebView\)/);
  assert.match(activity, /StartupLoadingOverlay\.attach\(this, webView\)[\s\S]*?lastObservedPageState == PageState\.LOADED[\s\S]*?startupLoadingOverlay\.onPageLoaded\(webView\)[\s\S]*?else[\s\S]*?startupLoadingOverlay\.onPageStarted\(webView\)/);
  assert.doesNotMatch(activity, /getProgress\(\)/);
  assert.match(overlay, /BuildConfig\.SPLASH_BACKGROUND_COLOR/);
  assert.match(overlay, /BuildConfig\.SPLASH_ACCENT_COLOR/);
  assert.match(overlay, /BuildConfig\.SPLASH_ICON_RESOURCE/);
  assert.match(overlay, /document\.readyState/);
  assert.match(overlay, /window\.visualViewport/);
  assert.match(overlay, /document\.fonts/);
  assert.match(overlay, /ResizeObserver/);
  assert.match(overlay, /MutationObserver/);
  assert.match(overlay, /data-driver-shell-bound/);
  assert.match(overlay, /data-eo-initialized/);
  assert.match(overlay, /querySelectorAll\('\[data-driver-tab-panel\]\.is-active'\)/);
  assert.match(overlay, /excavatorShell\.dataset\.eoActiveTab/);
  assert.match(overlay, /panel\.dataset\.eoScreen===excavatorActiveName/);
  assert.match(overlay, /node\.getClientRects\(\)\.length>0/);
  assert.match(overlay, /Number\.isFinite\(value\.width\)[\s\S]*?value\.width>0&&value\.height>0/);
  assert.match(overlay, /visibleBox\(driverActivePanel\)/);
  assert.match(overlay, /visibleBox\(excavatorActivePanel\)&&!excavatorActivePanel\.hidden/);
  assert.match(overlay, /REQUIRED_STABLE_FRAMES/);
  assert.match(overlay, /REQUIRED_QUIET_WINDOW_MS/);
  assert.match(overlay, /WindowInsetsCompat\.Type\.ime\(\)/);
  assert.match(overlay, /webView\.setAlpha\(0f\)/);
  assert.doesNotMatch(overlay, /View\.INVISIBLE/);
  assert.match(overlay, /postVisualStateCallback/);
  assert.match(overlay, /postVisualStateCallback[\s\S]*?evaluateJavascript\(READINESS_PROBE/);
  assert.match(overlay, /webView\.setAlpha\(1f\)[\s\S]*?postOnAnimation\(\(\) -> dismiss\(generation\)\)/);
  assert.match(overlay, /waitForImeClose/);
  assert.match(overlay, /waitForImeClose = rootInsets != null && rootInsets\.isVisible\(WindowInsetsCompat\.Type\.ime\(\)\)/);
  assert.match(overlay, /if \(waitForImeClose && !imeVisible\)[\s\S]*?waitForImeClose = false/);
  assert.match(overlay, /now - nativeLastChangeMs >= REQUIRED_NATIVE_QUIET_WINDOW_MS[\s\S]*?&& !waitForImeClose/);
  assert.match(overlay, /generation != pageGeneration/);
  assert.match(overlay, /STARTUP_WATCHDOG_MS = 15_000L/);
  assert.match(overlay, /runStartupWatchdog\(int generation, WebView watchedWebView\)[\s\S]*?evaluateJavascript\(READINESS_PROBE/);
  assert.match(overlay, /enterRecovery\([\s\S]*?DiagnosticReason\.WATCHDOG/);
  assert.doesNotMatch(watchdogBody, /webView\.setAlpha\(1f\)|dismiss\(generation\)/);
  assert.match(overlay, /restartProbeIfReady\(\)[\s\S]*?int generation = \+\+pageGeneration;[\s\S]*?armStartupWatchdog\(generation, STARTUP_WATCHDOG_MS\)/);
  assert.match(overlay, /runStartupWatchdog\(int generation[\s\S]*?generation != pageGeneration[\s\S]*?ensureStartupWatchdogArmed/);
  assert.match(overlay, /armStartupWatchdog\(int generation, long delayMs\)[\s\S]*?!hostResumed[\s\S]*?!windowFocused[\s\S]*?!activity\.hasWindowFocus\(\)/);
  assert.match(overlay, /onHostResumed\(\)[\s\S]*?cancelWatchdogs\(\);[\s\S]*?restartProbeOrArmWatchdog\(\)/);
  assert.match(overlay, /if \(!hostResumed \|\| !windowFocused \|\| !activity\.hasWindowFocus\(\)\) \{\s*return;\s*\}/);
  assert.match(activity, /setPageRevealedListener\([\s\S]*?notifyWebViewPageRevealed\(webView\)/);
  assert.match(activity, /notifyWebViewPageRevealed\(WebView revealedWebView\)[\s\S]*?new Event\('focus'\)[\s\S]*?native-connectivity-resume[\s\S]*?native_startup_revealed/);
  assert.match(overlay, /private Runnable pageRevealedListener;/);
  assert.match(overlay, /setPageRevealedListener\(Runnable listener\)[\s\S]*?pageRevealedListener = listener/);
  assert.match(overlay, /parent\.removeView\(overlay\);[\s\S]*?notifyPageRevealed\(generation\)/);
  assert.match(overlay, /private Runnable startupWatchdog;[\s\S]*?private Runnable recoveryWatchdog;/);
  assert.match(overlay, /armRecoveryWatchdog\(generation, DiagnosticReason\.WATCHDOG\)/);
  assert.match(overlay, /armRecoveryWatchdog\(generation, DiagnosticReason\.ERROR\)[\s\S]*?evaluateJavascript\(READINESS_PROBE/);
  assert.match(overlay, /cancelPendingProbe\(\)[\s\S]*?cancelWatchdogs\(\)/);
  assert.match(overlay, /cancelRecoveryWatchdog\(\)[\s\S]*?removeCallbacks\(recoveryWatchdog\)/);
  assert.match(overlay, /retryButton\.setText\("Повторить"\)/);
  assert.match(overlay, /retryButton\.setOnClickListener\([\s\S]*?retryCurrentPage\(\)/);
  assert.match(overlay, /retryCurrentPage\(\)[\s\S]*?webView\.reload\(\)/);
  assert.match(overlay, /IMPORTANT_FOR_ACCESSIBILITY_NO_HIDE_DESCENDANTS/);
  assert.match(overlay, /restoreWebViewInteraction\(\)[\s\S]*?setImportantForAccessibility\(savedWebViewImportantForAccessibility\)/);
  assert.match(overlay, /retryButton\.requestFocus\(\)[\s\S]*?announceForAccessibility/);
  assert.match(overlay, /new ScrollView\(activity\)/);
  assert.match(overlay, /contentScroll\.setFillViewport\(true\)/);
  assert.match(overlay, /WindowInsetsCompat\.Type\.displayCutout\(\)[\s\S]*?WindowInsetsCompat\.Type\.ime\(\)/);
  assert.match(overlay, /fontScale >= 1\.5f[\s\S]*?availableWidth > availableHeight/);
  assert.match(overlay, /retryButton\.setMinHeight\(dp\(48\)\)/);
  assert.match(overlay, /content\.addView\(retryButton, linearParams\([\s\S]*?ViewGroup\.LayoutParams\.WRAP_CONTENT,[\s\S]*?ViewGroup\.LayoutParams\.WRAP_CONTENT/);
  assert.match(overlay, /NORMAL\("normal"\)[\s\S]*?WATCHDOG\("watchdog"\)[\s\S]*?ERROR\("error"\)/);
  assert.match(overlay, /startup_overlay state=/);
  assert.doesNotMatch(overlay, /Log\.[idwe]\([^\n]*(getUrl|APP_START_URL|APP_SERVER_URL|encodedResult)/);
  assert.match(activity, /onReceivedError\(WebView erroredWebView\)[\s\S]*?lastObservedPageState != PageState\.STARTED[\s\S]*?startupLoadingOverlay\.onPageError\(erroredWebView\)/);
  assert.match(overlay, /private boolean destroyed;/);
  assert.match(overlay, /private boolean visible;/);
  assert.doesNotMatch(overlay, /private boolean dismissed;/);
  assert.match(overlay, /ValueAnimator\.ofFloat\(0f, 360f\)/);
  assert.match(overlay, /animator\.setDuration\(900L\)/);
});

test("release signing uses only an external credentials file", () => {
  const gradle = readFileSync(resolve(root, "android", "app", "build.gradle"), "utf8");
  assert.match(gradle, /COPPER_RELEASE_KEYSTORE_PROPERTIES/);
  assert.match(gradle, /CopperResourcesKeys\/keystore-credentials\.txt/);
  assert.match(gradle, /Release keystore was not found/);
  assert.doesNotMatch(gradle, /storePassword\s+["'][^"']+["']/);
  assert.doesNotMatch(gradle, /keyPassword\s+["'][^"']+["']/);
});

test("native updater is profile-driven, deferrable and verifies the APK", () => {
  const javaRoot = resolve(root, "android", "app", "src", "main", "java", "ru", "copperresources", "mobile");
  const activity = readFileSync(resolve(javaRoot, "MainActivity.java"), "utf8");
  const updater = readFileSync(resolve(javaRoot, "AppUpdateManager.java"), "utf8");
  const manifest = readFileSync(resolve(root, "android", "app", "src", "main", "AndroidManifest.xml"), "utf8");
  const buildScript = readFileSync(resolve(root, "scripts", "build-android.mjs"), "utf8");

  assert.match(activity, /BuildConfig\.IN_APP_UPDATER_ENABLED/);
  assert.match(activity, /new AppUpdateManager\(this\)/);
  assert.match(activity, /appUpdateManager\.onPageLoaded\(loadedWebView\)/);
  assert.match(updater, /BuildConfig\.UPDATE_MANIFEST_URL/);
  assert.match(updater, /setNegativeButton\("Позже"/);
  assert.match(updater, /setPositiveButton\("Обновить сейчас"/);
  assert.match(updater, /DEFERRED_VERSION_CODE/);
  assert.match(updater, /data-native-app-version/);
  assert.match(updater, /Update checksum mismatch/);
  assert.match(updater, /Update signing certificate mismatch/);
  assert.match(updater, /activity\.getPackageName\(\)\.equals\(archive\.packageName\)/);
  assert.match(updater, /!hostResumed \|\| !pageLoaded/);
  assert.match(activity, /onPageStarted\(loadingWebView\)[\s\S]*?appUpdateManager\.onPageStarted\(loadingWebView\)/);
  assert.match(manifest, /android\.permission\.REQUEST_INSTALL_PACKAGES/);
  assert.doesNotMatch(manifest, /android\.permission\.UPDATE_PACKAGES_WITHOUT_USER_ACTION/);
  assert.match(buildScript, /createHash\("sha256"\)/);
  assert.match(buildScript, /updateManifest/);
  assert.match(buildScript, /properties\.inAppUpdaterEnabled !== "false"/);
  assert.match(buildScript, /const publicApkName = `\$\{appProfileId\}-\$\{versionName\}\.apk`/);
  assert.match(buildScript, /apkUrl: `\$\{apkBaseUrl\}\$\{publicApkName\}`/);
  assert.doesNotMatch(buildScript, /apkUrl:[^\n]*versionCode/);
});

test("store variants remove package-installer permission", () => {
  const gradle = readFileSync(resolve(root, "android", "app", "build.gradle"), "utf8");
  for (const profileName of ["excavator_qa", "excavator_rustore_qa", "excavator_rustore"]) {
    const overlay = readFileSync(
      resolve(root, "profiles", profileName, "AndroidManifest.xml"),
      "utf8"
    );
    assert.match(overlay, /REQUEST_INSTALL_PACKAGES/);
    assert.match(overlay, /tools:node="remove"/);
  }
  assert.match(gradle, /manifest\.srcFile\(profileManifest\)/);
});
