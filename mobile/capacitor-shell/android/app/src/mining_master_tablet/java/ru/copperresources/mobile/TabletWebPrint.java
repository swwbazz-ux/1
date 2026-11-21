package ru.copperresources.mobile;

import android.app.ActivityManager;
import android.graphics.Color;
import android.graphics.drawable.ColorDrawable;
import android.graphics.drawable.Drawable;
import android.os.Bundle;
import android.os.CancellationSignal;
import android.os.Handler;
import android.os.Looper;
import android.os.ParcelFileDescriptor;
import android.os.SystemClock;
import android.print.PageRange;
import android.print.PrintAttributes;
import android.print.PrintDocumentAdapter;
import android.print.PrintManager;
import android.webkit.WebView;
import android.widget.Toast;
import androidx.webkit.WebMessageCompat;
import androidx.webkit.WebViewCompat;
import androidx.webkit.WebViewFeature;
import java.util.Collections;
import org.json.JSONObject;

/** Origin-aware print requests only. No web API can authenticate or alter kiosk policy. */
final class TabletWebPrint {
    private static final String BRIDGE = "CopperTabletPrint";
    private final MiningMasterTabletActivity activity;
    private final TabletKioskController kiosk;
    private final WebView webView;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final TabletPrintRequestGate gate = new TabletPrintRequestGate();
    private boolean bridgeInstalled;
    private boolean resumed;
    private boolean destroyed;
    private TabletPrintRequestGate.Request activePrintRequest;
    private boolean returnPromptPending;
    private boolean leftForSystemPrint;
    private Integer printBackgroundColor;
    private TabletPrintRequestGate.Request printBackgroundOwner;

    TabletWebPrint(MiningMasterTabletActivity activity, TabletKioskController kiosk, WebView webView) {
        this.activity = activity;
        this.kiosk = kiosk;
        this.webView = webView;
        if (WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) {
            try {
                WebViewCompat.addWebMessageListener(webView, BRIDGE, Collections.singleton(TabletUrlPolicy.ORIGIN),
                    (view, message, origin, mainFrame, reply) -> {
                        if (destroyed || !resumed || activePrintRequest != null || view != this.webView
                            || message.getType() != WebMessageCompat.TYPE_STRING) return;
                        TabletPrintRequestGate.Request request = gate.begin(message.getData(), origin.toString(),
                            mainFrame, view.getUrl());
                        if (request == null) return;
                        if (!kiosk.authorizePrinting(() -> approved(request), () -> gate.finish(request))) gate.finish(request);
                    });
                bridgeInstalled = true;
            } catch (RuntimeException failure) { diagnostic("install", failure); }
        }
    }

    String script() {
        return "(function(){if(window.top!==window||location.origin!==" + JSONObject.quote(TabletUrlPolicy.ORIGIN)
            + ")return;window.print=function(){if(window.top!==window||location.origin!=="
            + JSONObject.quote(TabletUrlPolicy.ORIGIN) + ")return;var bridge=window." + BRIDGE + ";"
            + "if(!bridge||typeof bridge.postMessage!=='function'){"
            + "window.alert('Системная печать пока недоступна. Повторно откройте страницу; если это не помогло, обратитесь к ответственному за планшет.');return;}"
            + "bridge.postMessage('" + TabletPrintRequestGate.REQUEST + "');};})();";
    }

    void pageStarted() { gate.pageStarted(); }
    void pageFailed() { gate.pageFailed(); }
    void pageFinished(String url) {
        gate.pageFinished(url, webView.getUrl());
        if (!destroyed && TabletUrlPolicy.trusted(webView.getUrl())) webView.evaluateJavascript(script(), null);
    }

    private void approved(TabletPrintRequestGate.Request request) {
        if (!current(request)) {
            gate.finish(request);
            Toast.makeText(activity, "Страница изменилась. Повторно нажмите «Печать» в нужном отчёте.", Toast.LENGTH_LONG).show();
            return;
        }
        if (activity.getSystemService(PrintManager.class) == null) {
            gate.finish(request);
            Toast.makeText(activity, "Служба печати Android недоступна. Защита не изменена.", Toast.LENGTH_LONG).show();
            return;
        }
        if (!kiosk.pauseForPrinting()) { gate.finish(request); return; }
        returnPromptPending = true;
        // stopLockTask is asynchronous too. Do not launch an external Activity until NONE is observed.
        awaitMaintenance(request, SystemClock.elapsedRealtime());
    }

    private boolean current(TabletPrintRequestGate.Request request) {
        return !destroyed && resumed && !activity.isFinishing() && gate.current(request, webView.getUrl());
    }

    private void awaitMaintenance(TabletPrintRequestGate.Request request, long started) {
        if (!current(request)) { failed(request, "page_changed", null); return; }
        try {
            ActivityManager manager = activity.getSystemService(ActivityManager.class);
            if (manager == null) throw new IllegalStateException();
            if (manager.getLockTaskModeState() == ActivityManager.LOCK_TASK_MODE_NONE) {
                print(request);
            } else if (SystemClock.elapsedRealtime() - started >= 3_000L) {
                failed(request, "maintenance_timeout", new java.util.concurrent.TimeoutException());
            } else handler.postDelayed(() -> awaitMaintenance(request, started), 100L);
        } catch (RuntimeException failure) { failed(request, "maintenance_state", failure); }
    }

    private void print(TabletPrintRequestGate.Request request) {
        if (!current(request)) { failed(request, "page_changed", null); return; }
        try {
            PrintManager manager = activity.getSystemService(PrintManager.class);
            if (manager == null) throw new IllegalStateException();
            usePrintBackground(request);
            String name = "Горный мастер — отчёт";
            PrintDocumentAdapter delegate = webView.createPrintDocumentAdapter(name);
            PrintDocumentAdapter adapter = new PrintDocumentAdapter() {
                @Override public void onStart() { delegate.onStart(); }
                @Override public void onLayout(PrintAttributes oldAttributes, PrintAttributes newAttributes,
                        CancellationSignal signal, LayoutResultCallback callback, Bundle extras) {
                    if (destroyed || !gate.sameDocument(request, webView.getUrl())) {
                        restorePrintBackground(request);
                        callback.onLayoutCancelled();
                        return;
                    }
                    delegate.onLayout(oldAttributes, newAttributes, signal, callback, extras);
                }
                @Override public void onWrite(PageRange[] pages, ParcelFileDescriptor destination,
                        CancellationSignal signal, WriteResultCallback callback) {
                    if (destroyed || !gate.sameDocument(request, webView.getUrl())) {
                        restorePrintBackground(request);
                        callback.onWriteCancelled();
                        return;
                    }
                    delegate.onWrite(pages, destination, signal, callback);
                }
                @Override public void onFinish() {
                    try { delegate.onFinish(); }
                    finally {
                        restorePrintBackground(request);
                        if (activePrintRequest == request) activePrintRequest = null;
                        gate.finish(request);
                    }
                }
            };
            activePrintRequest = request;
            if (manager.print(name, adapter, new PrintAttributes.Builder()
                .setMediaSize(PrintAttributes.MediaSize.ISO_A4.asLandscape()).build()) == null) throw new IllegalStateException();
        } catch (RuntimeException failure) { failed(request, "print_start", failure); }
    }

    private void failed(TabletPrintRequestGate.Request request, String stage, Throwable failure) {
        restorePrintBackground(request);
        gate.finish(request);
        if (activePrintRequest == request) activePrintRequest = null;
        diagnostic(stage, failure);
        if (!destroyed && resumed) {
            Toast.makeText(activity, "Печать не открылась. Планшет остаётся в обслуживании.", Toast.LENGTH_LONG).show();
            showReturnPrompt();
        }
    }

    void onResume() {
        resumed = true;
        if (returnPromptPending && leftForSystemPrint) showReturnPrompt();
    }

    private void showReturnPrompt() {
        returnPromptPending = false;
        leftForSystemPrint = false;
        kiosk.showReturnFromPrinting();
    }

    void onPause() {
        resumed = false;
        handler.removeCallbacksAndMessages(null);
        gate.cancelPending();
        if (returnPromptPending) leftForSystemPrint = true;
    }

    void destroy() {
        destroyed = true;
        restorePrintBackground(printBackgroundOwner);
        activePrintRequest = null;
        handler.removeCallbacksAndMessages(null);
        gate.cancelPending();
        if (bridgeInstalled) WebViewCompat.removeWebMessageListener(webView, BRIDGE);
    }

    private void usePrintBackground(TabletPrintRequestGate.Request request) {
        if (printBackgroundOwner != null) {
            if (printBackgroundOwner != request) throw new IllegalStateException();
            return;
        }
        Drawable background = webView.getBackground();
        // setBackgroundColor can mutate the existing ColorDrawable. Save the scalar, not its alias.
        // API 30 WebView sends color directly to its provider, so getBackground() can be null.
        // MainActivity is the sole other native writer and always sets this configured color.
        printBackgroundColor = background instanceof ColorDrawable
            ? ((ColorDrawable) background).getColor()
            : Color.parseColor(BuildConfig.SPLASH_BACKGROUND_COLOR);
        printBackgroundOwner = request;
        webView.setBackgroundColor(Color.WHITE);
        diagnostic(background instanceof ColorDrawable ? "background_white_drawable" : "background_white_configured", null);
    }

    private void restorePrintBackground(TabletPrintRequestGate.Request request) {
        if (printBackgroundOwner != request || printBackgroundColor == null) return;
        int originalColor = printBackgroundColor;
        printBackgroundColor = null;
        printBackgroundOwner = null;
        try {
            webView.setBackgroundColor(originalColor);
            diagnostic("background_restored", null);
        }
        catch (RuntimeException failure) { diagnostic("background_restore", failure); }
    }

    private void diagnostic(String stage, Throwable failure) {
        android.util.Log.w("TabletPrint", "stage=" + stage + " error="
            + (failure == null ? "none" : failure.getClass().getSimpleName()));
    }
}
