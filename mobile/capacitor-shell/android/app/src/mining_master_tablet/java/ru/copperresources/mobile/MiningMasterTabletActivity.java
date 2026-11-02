package ru.copperresources.mobile;

import android.content.pm.ActivityInfo;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.KeyEvent;
import android.view.WindowManager;
import android.webkit.WebResourceRequest;
import android.webkit.WebView;
import android.widget.Toast;

import androidx.activity.OnBackPressedCallback;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsCompat;
import androidx.core.view.WindowInsetsControllerCompat;
import androidx.webkit.WebViewCompat;
import androidx.webkit.WebViewFeature;

import com.getcapacitor.BridgeWebViewClient;
import com.getcapacitor.WebViewListener;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.Collections;
import org.json.JSONArray;
import org.json.JSONObject;

/** This class and all device-management code exist only in the tablet flavor. */
public final class MiningMasterTabletActivity extends MainActivity {
    private TabletKioskController kiosk;
    private TabletEdgeGuard edgeGuard;
    private TabletReportDownloads reportDownloads;
    private TabletWebPrint webPrint;
    private final Handler adminHandler = new Handler(Looper.getMainLooper());
    private final Runnable adminShortcut = () -> {
        if (kiosk != null && !isFinishing()) kiosk.showAdminEntry();
    };
    private String tabletScript = "";
    private boolean loadingOfflineCopy;
    private boolean systemInsetsKnown;
    private int systemInsetTopPx;
    private int systemInsetBottomPx;

    @Override
    protected boolean usesBackgroundConnectionService() { return false; }

    @Override
    protected void onCreate(Bundle state) {
        setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE);
        tabletScript = readTabletScript();
        bridgeBuilder.addWebViewListener(new WebViewListener() {
            @Override public void onPageLoaded(WebView view) { injectTablet(view); }
        });
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        kiosk = new TabletKioskController(this);
        edgeGuard = new TabletEdgeGuard(this,
            () -> kiosk != null && kiosk.allowsEdgeGuard(), this::edgeGuardChanged);
        reportDownloads = new TabletReportDownloads(this);
        installSystemInsetsListener();
        if (getBridge() != null && getBridge().getWebView() != null) {
            WebView view = getBridge().getWebView();
            // Explicit release boundary: no DevTools on the delivered signed APK.
            WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG);
            view.getSettings().setAllowFileAccess(false);
            view.getSettings().setAllowContentAccess(false);
            view.getSettings().setSupportMultipleWindows(false);
            view.setDownloadListener((url, userAgent, disposition, mimeType, length) ->
                reportDownloads.download(url, userAgent, disposition, mimeType));
            webPrint = new TabletWebPrint(this, kiosk, view);
            getBridge().setWebViewClient(new BridgeWebViewClient(getBridge()) {
                @Override public void onPageStarted(WebView webView, String url, android.graphics.Bitmap favicon) {
                    if (url == null || !url.contains("#cr-saved-board")) loadingOfflineCopy = false;
                    android.util.Log.i("MiningMasterTablet", "main_frame event=started route="
                        + TabletUrlPolicy.diagnosticRoute(url));
                    webPrint.pageStarted();
                    super.onPageStarted(webView, url, favicon);
                }
                @Override public void onPageFinished(WebView webView, String url) {
                    android.util.Log.i("MiningMasterTablet", "main_frame event=finished route="
                        + TabletUrlPolicy.diagnosticRoute(url));
                    super.onPageFinished(webView, url);
                    webPrint.pageFinished(url);
                }
                @Override public void onReceivedError(WebView webView, WebResourceRequest request,
                        android.webkit.WebResourceError error) {
                    int code = error.getErrorCode();
                    if ((code == ERROR_HOST_LOOKUP || code == ERROR_CONNECT || code == ERROR_TIMEOUT)
                            && showSavedBoard(webView, request)) return;
                    if (request.isForMainFrame()) {
                        android.util.Log.w("MiningMasterTablet", "main_frame event=network_error code="
                            + error.getErrorCode() + " route="
                            + TabletUrlPolicy.diagnosticRoute(request.getUrl().toString()));
                        webPrint.pageFailed();
                    }
                    super.onReceivedError(webView, request, error);
                }
                @Override public void onReceivedHttpError(WebView webView, WebResourceRequest request,
                        android.webkit.WebResourceResponse response) {
                    int status = response.getStatusCode();
                    if ((status == 502 || status == 503 || status == 504) && showSavedBoard(webView, request)) return;
                    if (request.isForMainFrame()) {
                        android.util.Log.w("MiningMasterTablet", "main_frame event=http_error status="
                            + response.getStatusCode() + " route="
                            + TabletUrlPolicy.diagnosticRoute(request.getUrl().toString()));
                        webPrint.pageFailed();
                    }
                    super.onReceivedHttpError(webView, request, response);
                }
                @Override public boolean shouldOverrideUrlLoading(WebView webView, WebResourceRequest request) {
                    if (!TabletUrlPolicy.workplaceNavigation(request.getUrl().toString())) {
                        Toast.makeText(MiningMasterTabletActivity.this,
                            "На рабочем планшете доступны только страницы Горного мастера", Toast.LENGTH_SHORT).show();
                        return true;
                    }
                    return super.shouldOverrideUrlLoading(webView, request);
                }
                @Override public boolean shouldOverrideUrlLoading(WebView webView, String url) {
                    return !TabletUrlPolicy.workplaceNavigation(url);
                }
            });
            if (WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) {
                WebViewCompat.addDocumentStartJavaScript(view, webPrint.script(),
                    Collections.singleton(TabletUrlPolicy.ORIGIN));
                if (!tabletScript.isEmpty()) WebViewCompat.addDocumentStartJavaScript(view, systemInsetsScript() + tabletScript,
                    Collections.singleton(TabletUrlPolicy.ORIGIN));
            }
            // The initial load may have begun before the document-start hook was installed.
            injectTablet(view);
            if (view.getProgress() == 100) webPrint.pageFinished(view.getUrl());
            // Capacitor can start the first HTTP request before this WebViewClient
            // is attached. With no network, explicitly load the saved document.
            android.net.ConnectivityManager networks = getSystemService(android.net.ConnectivityManager.class);
            if (networks != null && networks.getActiveNetwork() == null) loadSavedBoard(view);
        }
        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override public void handleOnBackPressed() {
                if (getBridge() != null) {
                    WebView webView = getBridge().getWebView();
                    android.webkit.WebBackForwardList history = webView.copyBackForwardList();
                    int previous = history.getCurrentIndex() - 1;
                    if (!TabletUrlPolicy.controlPage(webView.getUrl()) && previous >= 0
                        && TabletUrlPolicy.workplaceNavigation(history.getItemAtIndex(previous).getUrl())) {
                        webView.goBack();
                        return;
                    }
                    webView.evaluateJavascript(
                        "(function(){var plan=window.CopperTabletPlanning;if(plan&&plan.isOpen()){plan.close();return;}var open=document.querySelector('[aria-modal=true]:not([hidden])');"
                        + "if(open){document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));}})();", null);
                }
                // Never finish the HOME activity or leave the workplace via Android Back.
            }
        });
        adminHandler.postDelayed(() -> { if (!isFinishing()) kiosk.offerInitialSetup(); }, 800L);
        immersive();
    }

    // Only a failed GET of the original board can display its locally saved copy.
    private boolean showSavedBoard(WebView view, WebResourceRequest request) {
        if (loadingOfflineCopy || !request.isForMainFrame() || !"GET".equals(request.getMethod())
                || !TabletUrlPolicy.controlPage(request.getUrl().toString())) return false;
        loadingOfflineCopy = true;
        view.post(() -> {
            if (!isFinishing() && !isDestroyed() && loadingOfflineCopy) loadSavedBoard(view);
        });
        return true;
    }

    private void loadSavedBoard(WebView view) {
        String url = TabletUrlPolicy.START_URL + "#cr-saved-board";
        loadingOfflineCopy = true;
        try {
            String html = asset("tablet/tablet-planning-offline.html");
            view.stopLoading();
            view.loadDataWithBaseURL(url, html, "text/html", "UTF-8", url);
        } catch (java.io.IOException error) {
            loadingOfflineCopy = false;
            android.util.Log.e("MiningMasterTablet", "Saved board shell missing", error);
        }
    }

    private String readTabletScript() {
        try {
            String css = asset("tablet/mining-master-tablet.css");
            String js = asset("tablet/mining-master-tablet.js");
            String shiftLogin = asset("shared-shift-login.js");
            String dialogScale = asset("tablet/tablet-dialog-scale.css");
            String entryCss = asset("tablet/tablet-entry-fields.css");
            String entryJs = asset("tablet/tablet-entry-fields.js");
            String planning = asset("tablet/tablet-planning-draft.js") + "\n"
                + asset("tablet/tablet-planning-copy.js") + "\n"
                + asset("tablet/tablet-planning-cache.js") + "\n" + asset("tablet/tablet-planning-entry.js");
            String planningCss = asset("tablet/tablet-planning-copy.css");
            return "(function(){if(window.top!==window||location.origin!==" + JSONObject.quote(TabletUrlPolicy.ORIGIN)
                + ")return;var workplace=" + new JSONArray(TabletUrlPolicy.TABLET_PATHS)
                + ".indexOf(location.pathname)!==-1;"
                + "if(!workplace&&" + new JSONArray(TabletUrlPolicy.ENTRY_PATHS) + ".indexOf(location.pathname)===-1)return;"
                // Install focus handling before the server-rendered phone field requests autofocus.
                + entryJs + "\n"
                + "function addStyle(id,css){if(document.getElementById(id))return;"
                + "var s=document.createElement('style');s.id=id;s.textContent=css;document.head.appendChild(s);}"
                + "function boot(){if(!document.head)return;"
                + "addStyle('cr-tablet-dialog-scale-css'," + JSONObject.quote(dialogScale) + ");"
                + "addStyle('cr-tablet-entry-fields-css'," + JSONObject.quote(entryCss) + ");"
                + "if(!workplace){if(document.querySelector('#tablet-phone,[data-tablet-welcome],form[action*=login]')||document.body&&document.body.dataset.appRoleCode==='mining_master'){"
                + "var op=indexedDB.open('cr-tablet-board-cache',1);op.onupgradeneeded=function(){op.result.createObjectStore('board');};"
                // Keep a timestamp after logout: a capture still running in the old
                // document must not put that employee's portrait back in the store.
                + "op.onsuccess=function(){var db=op.result,tx=db.transaction('board','readwrite'),shelf=tx.objectStore('board'),epoch=performance.timeOrigin;"
                + "var previous=shelf.get('latest');previous.onsuccess=function(){if(!previous.result||!(previous.result.documentTime>epoch))"
                + "shelf.put({documentTime:epoch,serverTime:0,cleared:true},'latest');};tx.oncomplete=tx.onabort=function(){db.close();};};}return;}"
                + "addStyle('cr-mining-master-tablet-css'," + JSONObject.quote(css) + ");"
                + "addStyle('cr-tablet-planning-copy-css'," + JSONObject.quote(planningCss) + ");"
                + "if(document.documentElement.hasAttribute('data-cr-saved-board')){" + js + "\n" + planning + "\nreturn;}"
                + "if(typeof window.initSharedShiftLogin!=='function'){" + shiftLogin + "\n}"
                + js + "\n" + planning + "\n}if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',boot,{once:true});}else{boot();}})();";
        } catch (Exception error) {
            android.util.Log.e("MiningMasterTablet", "Tablet layout assets are unavailable");
            return "";
        }
    }

    private String asset(String name) throws java.io.IOException {
        try (InputStream input = getAssets().open(name); ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[8192];
            int count;
            while ((count = input.read(buffer)) != -1) output.write(buffer, 0, count);
            return output.toString(StandardCharsets.UTF_8.name());
        }
    }

    private void injectTablet(WebView view) {
        if (!tabletScript.isEmpty() && (TabletUrlPolicy.tabletPage(view.getUrl()) || TabletUrlPolicy.entryPage(view.getUrl()))) {
            recordSystemInsets(ViewCompat.getRootWindowInsets(getWindow().getDecorView()));
            view.evaluateJavascript(systemInsetsScript() + tabletScript, null);
        }
    }

    private void installSystemInsetsListener() {
        android.view.View decor = getWindow().getDecorView();
        ViewCompat.setOnApplyWindowInsetsListener(decor, (view, insets) -> {
            if (recordSystemInsets(insets)) publishSystemInsets();
            // Preserve normal dispatch to Capacitor, startup overlay and the IME layout.
            return ViewCompat.onApplyWindowInsets(view, insets);
        });
        decor.addOnLayoutChangeListener((view, left, top, right, bottom,
                oldLeft, oldTop, oldRight, oldBottom) -> {
            if (right - left != oldRight - oldLeft || bottom - top != oldBottom - oldTop) {
                refreshSystemInsets();
            }
        });
        refreshSystemInsets();
        ViewCompat.requestApplyInsets(decor);
    }

    private boolean recordSystemInsets(WindowInsetsCompat insets) {
        if (insets == null) return false;
        // Stable insets remain the fallback on unverified devices. The verified native
        // edge guard publishes zero only after Android confirms its policy is active.
        Insets bars = insets.getInsetsIgnoringVisibility(WindowInsetsCompat.Type.systemBars());
        Insets gestures = insets.getInsets(WindowInsetsCompat.Type.systemGestures()
            | WindowInsetsCompat.Type.mandatorySystemGestures());
        int top = Math.max(bars.top, gestures.top);
        int bottom = Math.max(bars.bottom, gestures.bottom);
        if (systemInsetsKnown && systemInsetTopPx == top && systemInsetBottomPx == bottom) return false;
        systemInsetsKnown = true;
        systemInsetTopPx = top;
        systemInsetBottomPx = bottom;
        return true;
    }

    private void refreshSystemInsets() {
        recordSystemInsets(ViewCompat.getRootWindowInsets(getWindow().getDecorView()));
        publishSystemInsets();
    }

    private void publishSystemInsets() {
        if (!systemInsetsKnown || getBridge() == null || getBridge().getWebView() == null) return;
        WebView view = getBridge().getWebView();
        if (TabletUrlPolicy.tabletPage(view.getUrl())) {
            view.evaluateJavascript(systemInsetsScript(), null);
        }
    }

    private String systemInsetsScript() {
        if (!systemInsetsKnown) return "";
        boolean guarded = edgeGuard != null && edgeGuard.active();
        int top = guarded ? 0 : systemInsetTopPx;
        int bottom = guarded ? 0 : systemInsetBottomPx;
        return "(function(){if(window.top!==window||location.origin!==" + JSONObject.quote(TabletUrlPolicy.ORIGIN)
            + "||" + new JSONArray(TabletUrlPolicy.TABLET_PATHS).toString()
            + ".indexOf(location.pathname)===-1)return;function apply(){var root=document.documentElement;"
            + "if(!root)return;var ratio=Number(window.devicePixelRatio);"
            + "if(!Number.isFinite(ratio)||ratio<=0)ratio=1;"
            + "root.setAttribute('data-cr-tablet-edge-guard','" + (guarded ? "active" : "inactive") + "');"
            + "root.style.setProperty('--cr-tablet-system-top',(" + top + "/ratio)+'px');"
            + "root.style.setProperty('--cr-tablet-system-bottom',(" + bottom + "/ratio)+'px');}"
            + "if(document.documentElement)apply();else document.addEventListener('DOMContentLoaded',apply,{once:true});})();";
    }

    @Override public void onResume() {
        super.onResume();
        immersive();
        refreshSystemInsets();
        if (kiosk != null) kiosk.onResume();
        if (edgeGuard != null) edgeGuard.onResume();
        if (webPrint != null) webPrint.onResume();
    }

    @Override public void onWindowFocusChanged(boolean focused) {
        super.onWindowFocusChanged(focused);
        if (focused) {
            immersive();
            refreshSystemInsets();
        }
    }

    private void immersive() {
        WindowInsetsControllerCompat controller = WindowCompat.getInsetsController(getWindow(), getWindow().getDecorView());
        controller.setSystemBarsBehavior(WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
        controller.hide(WindowInsetsCompat.Type.systemBars());
    }

    private void edgeGuardChanged() {
        if (isFinishing()) return;
        immersive();
        refreshSystemInsets();
    }

    boolean releaseEdgeGuard() { return edgeGuard == null || edgeGuard.release(); }

    @Override public boolean dispatchKeyEvent(KeyEvent event) {
        if (event.getKeyCode() == KeyEvent.KEYCODE_VOLUME_UP) {
            if (event.getAction() == KeyEvent.ACTION_DOWN && event.getRepeatCount() == 0) {
                adminHandler.postDelayed(adminShortcut, 2500L);
            } else if (event.getAction() == KeyEvent.ACTION_UP) {
                adminHandler.removeCallbacks(adminShortcut);
            }
        }
        return super.dispatchKeyEvent(event);
    }

    @Override public void onPause() {
        adminHandler.removeCallbacks(adminShortcut);
        if (edgeGuard != null) edgeGuard.onPause();
        if (webPrint != null) webPrint.onPause();
        if (kiosk != null) kiosk.onPause();
        super.onPause();
    }

    @Override public void onDestroy() {
        adminHandler.removeCallbacksAndMessages(null);
        if (edgeGuard != null) edgeGuard.destroy();
        if (webPrint != null) webPrint.destroy();
        if (kiosk != null) kiosk.destroy();
        if (reportDownloads != null) reportDownloads.destroy();
        super.onDestroy();
    }
}
