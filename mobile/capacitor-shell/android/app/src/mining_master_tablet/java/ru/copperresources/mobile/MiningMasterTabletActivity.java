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
    private TabletReportDownloads reportDownloads;
    private TabletWebPrint webPrint;
    private final Handler adminHandler = new Handler(Looper.getMainLooper());
    private final Runnable adminShortcut = () -> {
        if (kiosk != null && !isFinishing()) kiosk.showAdminEntry();
    };
    private String tabletScript = "";

    @Override
    protected boolean usesBackgroundConnectionService() { return false; }

    @Override
    protected String standaloneStartupReadiness(String pageUrl) {
        return TabletStandaloneReadiness.expressionFor(BuildConfig.APP_PROFILE_ID, pageUrl);
    }

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
        reportDownloads = new TabletReportDownloads(this);
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
                    webPrint.pageStarted();
                    super.onPageStarted(webView, url, favicon);
                }
                @Override public void onPageFinished(WebView webView, String url) {
                    super.onPageFinished(webView, url);
                    webPrint.pageFinished(url);
                }
                @Override public void onReceivedError(WebView webView, WebResourceRequest request,
                        android.webkit.WebResourceError error) {
                    if (request.isForMainFrame()) webPrint.pageFailed();
                    super.onReceivedError(webView, request, error);
                }
                @Override public void onReceivedHttpError(WebView webView, WebResourceRequest request,
                        android.webkit.WebResourceResponse response) {
                    if (request.isForMainFrame()) webPrint.pageFailed();
                    super.onReceivedHttpError(webView, request, response);
                }
                @Override public boolean shouldOverrideUrlLoading(WebView webView, WebResourceRequest request) {
                    if (!TabletUrlPolicy.trusted(request.getUrl().toString())) {
                        Toast.makeText(MiningMasterTabletActivity.this,
                            "На рабочем планшете доступны только страницы Горного мастера", Toast.LENGTH_SHORT).show();
                        return true;
                    }
                    return super.shouldOverrideUrlLoading(webView, request);
                }
                @Override public boolean shouldOverrideUrlLoading(WebView webView, String url) {
                    return !TabletUrlPolicy.trusted(url);
                }
            });
            if (WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) {
                WebViewCompat.addDocumentStartJavaScript(view, webPrint.script(),
                    Collections.singleton(TabletUrlPolicy.ORIGIN));
                if (!tabletScript.isEmpty()) WebViewCompat.addDocumentStartJavaScript(view, tabletScript,
                    Collections.singleton(TabletUrlPolicy.ORIGIN));
            }
            // The initial load may have begun before the document-start hook was installed.
            injectTablet(view);
            if (view.getProgress() == 100) webPrint.pageFinished(view.getUrl());
        }
        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override public void handleOnBackPressed() {
                if (getBridge() != null) {
                    WebView webView = getBridge().getWebView();
                    android.webkit.WebBackForwardList history = webView.copyBackForwardList();
                    int previous = history.getCurrentIndex() - 1;
                    if (!TabletUrlPolicy.controlPage(webView.getUrl()) && previous >= 0
                        && TabletUrlPolicy.trusted(history.getItemAtIndex(previous).getUrl())) {
                        webView.goBack();
                        return;
                    }
                    webView.evaluateJavascript(
                        "(function(){var open=document.querySelector('[aria-modal=true]:not([hidden])');"
                        + "if(open){document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));}})();", null);
                }
                // Never finish the HOME activity or leave the workplace via Android Back.
            }
        });
        adminHandler.postDelayed(() -> { if (!isFinishing()) kiosk.offerInitialSetup(); }, 800L);
        immersive();
    }

    private String readTabletScript() {
        try {
            String css = asset("tablet/mining-master-tablet.css");
            String js = asset("tablet/mining-master-tablet.js");
            return "(function(){if(window.top!==window||location.origin!==" + JSONObject.quote(TabletUrlPolicy.ORIGIN)
                + "||" + new JSONArray(TabletUrlPolicy.TABLET_PATHS).toString()
                + ".indexOf(location.pathname)===-1)return;"
                + "function boot(){if(!document.head)return;var s=document.getElementById('cr-mining-master-tablet-css');"
                + "if(!s){s=document.createElement('style');s.id='cr-mining-master-tablet-css';s.textContent="
                + JSONObject.quote(css) + ";document.head.appendChild(s);}"
                + js + "\n}if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',boot,{once:true});}else{boot();}})();";
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
        if (!tabletScript.isEmpty() && TabletUrlPolicy.tabletPage(view.getUrl())) {
            view.evaluateJavascript(tabletScript, null);
        }
    }

    @Override public void onResume() {
        super.onResume();
        immersive();
        if (kiosk != null) kiosk.onResume();
        if (webPrint != null) webPrint.onResume();
    }

    @Override public void onWindowFocusChanged(boolean focused) {
        super.onWindowFocusChanged(focused);
        if (focused) immersive();
    }

    private void immersive() {
        WindowInsetsControllerCompat controller = WindowCompat.getInsetsController(getWindow(), getWindow().getDecorView());
        controller.setSystemBarsBehavior(WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
        controller.hide(WindowInsetsCompat.Type.systemBars());
    }

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
        if (webPrint != null) webPrint.onPause();
        if (kiosk != null) kiosk.onPause();
        super.onPause();
    }

    @Override public void onDestroy() {
        adminHandler.removeCallbacksAndMessages(null);
        if (webPrint != null) webPrint.destroy();
        if (kiosk != null) kiosk.destroy();
        if (reportDownloads != null) reportDownloads.destroy();
        super.onDestroy();
    }
}
