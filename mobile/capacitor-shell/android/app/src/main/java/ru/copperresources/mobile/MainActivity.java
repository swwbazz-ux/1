package ru.copperresources.mobile;

import android.Manifest;
import android.annotation.SuppressLint;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.PowerManager;
import android.view.inputmethod.InputMethodManager;
import android.webkit.CookieManager;
import android.webkit.WebView;

import androidx.annotation.NonNull;
import androidx.core.app.ActivityCompat;
import androidx.core.splashscreen.SplashScreen;

import com.getcapacitor.BridgeActivity;
import com.getcapacitor.CapConfig;
import com.getcapacitor.WebViewListener;

public class MainActivity extends BridgeActivity {
    private static final int NOTIFICATION_PERMISSION_REQUEST = 4101;
    private static final String PREFS_NAME = "native_shell";
    private static final String BATTERY_PROMPT_REQUESTED = "battery_prompt_requested";
    private enum PageState { UNKNOWN, STARTED, LOADED }

    private StartupLoadingOverlay startupLoadingOverlay;
    private AppUpdateManager appUpdateManager;
    private boolean nativeCoverReady;
    private WebView lastObservedWebView;
    private PageState lastObservedPageState = PageState.UNKNOWN;
    private boolean lastObservedPageError;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        SplashScreen splashScreen = SplashScreen.installSplashScreen(this);
        splashScreen.setKeepOnScreenCondition(() -> !nativeCoverReady);
        registerPlugin(NativeKeyboardPlugin.class);
        if ("excavator".equals(BuildConfig.APP_PROFILE_ID)) {
            registerPlugin(NativeSoundPlugin.class);
        }
        CookieManager.getInstance().setAcceptCookie(true);
        bridgeBuilder.addWebViewListener(new WebViewListener() {
            @Override
            public void onPageStarted(WebView loadingWebView) {
                runOnUiThread(() -> {
                    if (loadingWebView instanceof NativeImeWebView) {
                        ((NativeImeWebView) loadingWebView).clearNativeImeAction();
                    }
                    lastObservedWebView = loadingWebView;
                    lastObservedPageState = PageState.STARTED;
                    lastObservedPageError = false;
                    if (startupLoadingOverlay != null) {
                        startupLoadingOverlay.onPageStarted(loadingWebView);
                    }
                    if (appUpdateManager != null) {
                        appUpdateManager.onPageStarted(loadingWebView);
                    }
                    hideSoftKeyboard(loadingWebView);
                });
            }

            @Override
            public void onPageLoaded(WebView loadedWebView) {
                // Capacitor's server.url fallback copies Set-Cookie asynchronously.
                // Flushing after every completed page persists the initial CSRF cookie
                // and the rotated cookies from the successful login redirect.
                CookieManager.getInstance().flush();
                runOnUiThread(() -> {
                    lastObservedWebView = loadedWebView;
                    lastObservedPageState = PageState.LOADED;
                    if (startupLoadingOverlay != null) {
                        startupLoadingOverlay.onPageLoaded(loadedWebView);
                    }
                    if (appUpdateManager != null) {
                        appUpdateManager.onPageLoaded(loadedWebView);
                    }
                });
            }

            @Override
            public void onReceivedError(WebView erroredWebView) {
                runOnUiThread(() -> {
                    if (lastObservedPageState != PageState.STARTED) {
                        return;
                    }
                    if (lastObservedWebView != null && lastObservedWebView != erroredWebView) {
                        return;
                    }
                    lastObservedWebView = erroredWebView;
                    lastObservedPageError = true;
                    if (startupLoadingOverlay != null) {
                        /* Capacitor does not expose the failing request here, so this is
                           diagnostic input only. A successful readiness probe may still
                           reveal the page; the watchdog reports error if it never settles. */
                        startupLoadingOverlay.onPageError(erroredWebView);
                    }
                });
            }
        });

        Uri serverUri = Uri.parse(BuildConfig.APP_START_URL);
        String serverHost = serverUri.getHost();
        String[] allowNavigation = serverHost == null ? new String[0] : new String[] { serverHost };

        config = new CapConfig.Builder(this)
            .setServerUrl(BuildConfig.APP_START_URL)
            .setAllowNavigation(allowNavigation)
            .setAppendedUserAgentString(
                " CopperResourcesNative/" + BuildConfig.APP_PROFILE_ID
                    + "/" + BuildConfig.VERSION_NAME
            )
            .setResolveServiceWorkerRequests(true)
            .create();

        super.onCreate(savedInstanceState);
        configurePersistentWebViewCookies();
        if (getBridge() != null && getBridge().getWebView() != null) {
            WebView webView = getBridge().getWebView();
            webView.setBackgroundColor(
                android.graphics.Color.parseColor(BuildConfig.SPLASH_BACKGROUND_COLOR)
            );
            startupLoadingOverlay = StartupLoadingOverlay.attach(this, webView);
            startupLoadingOverlay.setPageRevealedListener(
                () -> notifyWebViewPageRevealed(webView)
            );
            if (BuildConfig.IN_APP_UPDATER_ENABLED) {
                appUpdateManager = new AppUpdateManager(this);
                appUpdateManager.attach(webView);
            }
            /* BridgeWebViewClient may finish a cached document inside
               super.onCreate(), before the overlay exists. Buffering the
               listener state avoids both unreliable WebView progress and a
               permanent spinner after a lost onPageLoaded callback. */
            if (lastObservedWebView == webView && lastObservedPageState == PageState.LOADED) {
                startupLoadingOverlay.onPageLoaded(webView);
            } else {
                startupLoadingOverlay.onPageStarted(webView);
            }
            if (lastObservedWebView == webView && lastObservedPageError) {
                startupLoadingOverlay.onPageError(webView);
            }
            if (appUpdateManager != null) {
                if (lastObservedWebView == webView && lastObservedPageState == PageState.LOADED) {
                    appUpdateManager.onPageLoaded(webView);
                } else {
                    appUpdateManager.onPageStarted(webView);
                }
            }
        }
        nativeCoverReady = true;
        AppNotifications.createChannels(this);
        ConnectivityForegroundService.start(this);
        requestNotificationPermissionThenBatteryExemption();
    }

    private void configurePersistentWebViewCookies() {
        if (getBridge() == null || getBridge().getWebView() == null) {
            return;
        }
        WebView webView = getBridge().getWebView();
        CookieManager cookieManager = CookieManager.getInstance();
        cookieManager.setAcceptCookie(true);
        cookieManager.setAcceptThirdPartyCookies(webView, true);
    }

    private void notifyWebViewPageRevealed(WebView revealedWebView) {
        if (revealedWebView == null) {
            return;
        }
        /* StartupLoadingOverlay temporarily suppresses WebView focus. A WebView
           can reveal its first frame without sending DOM focus, so tell the
           shared realtime client explicitly to make its first safe catch-up. */
        revealedWebView.post(() -> {
            if (isFinishing()
                    || getBridge() == null
                    || getBridge().getWebView() != revealedWebView) {
                return;
            }
            revealedWebView.evaluateJavascript(
                "(function(){" +
                    "window.dispatchEvent(new Event('focus'));" +
                    "window.dispatchEvent(new CustomEvent('native-connectivity-resume'," +
                        "{detail:{source:'startup_overlay'}}));" +
                    "if(window.AppRealtime&&typeof window.AppRealtime.wake==='function'){" +
                        "window.AppRealtime.wake('native_startup_revealed');" +
                    "}" +
                "})();",
                null
            );
        });
    }

    @Override
    public void onResume() {
        super.onResume();
        if (appUpdateManager != null) {
            appUpdateManager.onHostResumed();
        }
        if (startupLoadingOverlay != null) {
            startupLoadingOverlay.onHostResumed();
        }
        if (getBridge() == null || getBridge().getWebView() == null) {
            return;
        }
        getBridge().getWebView().postDelayed(() -> getBridge().getWebView().evaluateJavascript(
            "(function(){" +
                "window.dispatchEvent(new CustomEvent('native-connectivity-resume'));" +
                "if(window.AppRealtime&&typeof window.AppRealtime.wake==='function'){" +
                    "window.AppRealtime.wake('native_foreground_resume');" +
                "}" +
            "})();",
            null
        ), 350L);
    }

    @Override
    public void onStart() {
        super.onStart();
        AppVisibility.setForeground(true);
    }

    @Override
    public void onPause() {
        CookieManager.getInstance().flush();
        if (appUpdateManager != null) {
            appUpdateManager.onHostPaused();
        }
        if (startupLoadingOverlay != null) {
            startupLoadingOverlay.onHostPaused();
        }
        super.onPause();
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (startupLoadingOverlay != null) {
            startupLoadingOverlay.onWindowFocusChanged(hasFocus);
        }
    }

    @Override
    public void onStop() {
        CookieManager.getInstance().flush();
        AppVisibility.setForeground(false);
        super.onStop();
    }

    @Override
    public void onDestroy() {
        if (appUpdateManager != null) {
            appUpdateManager.destroy();
        }
        if (startupLoadingOverlay != null) {
            startupLoadingOverlay.destroy();
        }
        if (!isChangingConfigurations()) {
            ConnectivityForegroundService.stop(this);
        }
        super.onDestroy();
    }

    private void hideSoftKeyboard(WebView webView) {
        if (webView == null) {
            return;
        }
        InputMethodManager inputMethodManager =
            (InputMethodManager) getSystemService(INPUT_METHOD_SERVICE);
        if (inputMethodManager != null && webView.getWindowToken() != null) {
            inputMethodManager.hideSoftInputFromWindow(webView.getWindowToken(), 0);
        }
    }

    private void requestNotificationPermissionThenBatteryExemption() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
                && ActivityCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(
                this,
                new String[] { Manifest.permission.POST_NOTIFICATIONS },
                NOTIFICATION_PERMISSION_REQUEST
            );
            return;
        }
        requestBatteryOptimizationExemptionOnce();
    }

    @Override
    public void onRequestPermissionsResult(
            int requestCode,
            @NonNull String[] permissions,
            @NonNull int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == NOTIFICATION_PERMISSION_REQUEST) {
            requestBatteryOptimizationExemptionOnce();
        }
    }

    @SuppressLint("BatteryLife")
    private void requestBatteryOptimizationExemptionOnce() {
        PowerManager powerManager = (PowerManager) getSystemService(POWER_SERVICE);
        if (powerManager != null && powerManager.isIgnoringBatteryOptimizations(getPackageName())) {
            return;
        }
        SharedPreferences preferences = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
        if (preferences.getBoolean(BATTERY_PROMPT_REQUESTED, false)) {
            return;
        }
        try {
            Intent request = new Intent(
                android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                Uri.parse("package:" + getPackageName())
            );
            preferences.edit().putBoolean(BATTERY_PROMPT_REQUESTED, true).apply();
            startActivity(request);
        } catch (Exception ignored) {
            preferences.edit().remove(BATTERY_PROMPT_REQUESTED).apply();
        }
    }
}
