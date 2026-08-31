package ru.copperresources.mobile;

import android.animation.ValueAnimator;
import android.app.Activity;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.RectF;
import android.graphics.Typeface;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.GradientDrawable;
import android.os.SystemClock;
import android.util.Log;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.animation.LinearInterpolator;
import android.webkit.WebView;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;

import org.json.JSONTokener;

final class StartupLoadingOverlay {
    private static final String LOG_TAG = "CopperStartup";
    private static final int REQUIRED_STABLE_FRAMES = 8;
    private static final long REQUIRED_QUIET_WINDOW_MS = 240L;
    private static final int REQUIRED_NATIVE_STABLE_FRAMES = 8;
    private static final long REQUIRED_NATIVE_QUIET_WINDOW_MS = 240L;
    private static final long FADE_DURATION_MS = 160L;
    private static final long STARTUP_WATCHDOG_MS = 15_000L;
    private static final long STARTUP_WATCHDOG_RETRY_MS = 3_000L;

    private static final String PREPARE_LAYOUT =
        "(function(){" +
            "var previous=window.__copperNativeStartupLayout;" +
            "if(previous&&typeof previous.destroy==='function'){previous.destroy();}" +
            "delete window.__copperNativeStartupLayout;" +
            "try{" +
                "window.dispatchEvent(new Event('resize'));" +
                "if(window.visualViewport){window.visualViewport.dispatchEvent(new Event('resize'));}" +
                "if(typeof window.syncExcavatorViewportHeight==='function'){window.syncExcavatorViewportHeight();}" +
                "if(typeof window.driverScheduleViewportFit==='function'){window.driverScheduleViewportFit();}" +
            "}catch(error){}" +
            "return true;" +
        "})()";

    private static final String READINESS_PROBE =
        "(function(){" +
            "var viewport=window.visualViewport;" +
            "var root=document.documentElement;" +
            "var body=document.body;" +
            "var driverShell=document.querySelector('[data-driver-shell]');" +
            "var excavatorShell=document.querySelector('[data-eo-shell]');" +
            "var roleShell=driverShell||excavatorShell||document.querySelector('main')||body;" +
            "var driverPanels=driverShell?driverShell.querySelectorAll('[data-driver-tab-panel].is-active'):null;" +
            "var driverActivePanel=driverPanels&&driverPanels.length===1?driverPanels[0]:null;" +
            "var excavatorActiveName=excavatorShell?String(excavatorShell.dataset.eoActiveTab||''):'';" +
            "var excavatorActivePanel=null;" +
            "if(excavatorShell&&excavatorActiveName){" +
                "Array.prototype.some.call(excavatorShell.querySelectorAll('[data-eo-screen]'),function(panel){" +
                    "if(panel.dataset.eoScreen===excavatorActiveName){excavatorActivePanel=panel;return true;}return false;" +
                "});" +
            "}" +
            "var activePanel=driverShell?driverActivePanel:(excavatorShell?excavatorActivePanel:null);" +
            "var metric=function(value){value=Number(value)||0;return Math.round(value*10)/10;};" +
            "var rect=function(node){var value=node&&node.getBoundingClientRect?node.getBoundingClientRect():null;" +
                "return value?[metric(value.left),metric(value.top),metric(value.width),metric(value.height)].join(','):'0,0,0,0';};" +
            "var visibleBox=function(node){" +
                "if(!node||node.hidden||node.getAttribute('aria-hidden')==='true'||!node.getBoundingClientRect){return false;}" +
                "var style=getComputedStyle(node);var value=node.getBoundingClientRect();" +
                "return style.display!=='none'&&style.visibility!=='hidden'&&node.getClientRects().length>0" +
                    "&&Number.isFinite(value.width)&&Number.isFinite(value.height)&&value.width>0&&value.height>0;" +
            "};" +
            "var width=metric(viewport?viewport.width:window.innerWidth);" +
            "var height=metric(viewport?viewport.height:window.innerHeight);" +
            "var scale=Math.round((Number(viewport?viewport.scale:1)||0)*1000)/1000;" +
            "var rootStyle=root?getComputedStyle(root):null;" +
            "var shellStyle=roleShell?getComputedStyle(roleShell):null;" +
            "var driverHeight=rootStyle?rootStyle.getPropertyValue('--driver-viewport-h').trim():'';" +
            "var excavatorHeight=rootStyle?rootStyle.getPropertyValue('--eo-app-height').trim():'';" +
            "var roleReady=visibleBox(roleShell);" +
            "if(driverShell){roleReady=driverShell.dataset.driverShellBound==='true'&&!!driverHeight" +
                "&&Math.abs((parseFloat(driverHeight)||0)-driverShell.getBoundingClientRect().height)<=2" +
                "&&!window.driverViewportFitFrame&&!window.driverDialLabelFitFrame" +
                "&&visibleBox(driverShell)&&visibleBox(driverActivePanel);}" +
            "if(excavatorShell){roleReady=excavatorShell.dataset.eoInitialized==='1'&&!!excavatorHeight" +
                "&&Math.abs((parseFloat(excavatorHeight)||0)-excavatorShell.getBoundingClientRect().height)<=2" +
                "&&visibleBox(excavatorShell)&&visibleBox(excavatorActivePanel)&&!excavatorActivePanel.hidden;}" +
            "var snapshot=[" +
                "width,height,scale,metric(window.innerWidth),metric(window.innerHeight)," +
                "root?root.clientWidth:0,root?root.clientHeight:0,root?root.scrollWidth:0,root?root.scrollHeight:0," +
                "body?body.clientWidth:0,body?body.clientHeight:0,body?body.scrollWidth:0,body?body.scrollHeight:0," +
                "rect(roleShell),rect(activePanel),driverHeight,excavatorHeight," +
                "driverShell?driverShell.dataset.driverDensity||'':''," +
                "excavatorShell?excavatorShell.dataset.eoActiveTab||'':''," +
                "shellStyle?shellStyle.display:'',shellStyle?shellStyle.visibility:''" +
            "].join('x');" +
            "var now=Date.now();" +
            "var state=window.__copperNativeStartupLayout;" +
            "if(!state){" +
                "state={snapshot:snapshot,stableFrames:0,lastChange:now,destroyed:false};" +
                "window.__copperNativeStartupLayout=state;" +
                "state.markChanged=function(){if(state.destroyed)return;state.lastChange=Date.now();state.stableFrames=0;};" +
                "window.addEventListener('resize',state.markChanged,{passive:true});" +
                "if(viewport){viewport.addEventListener('resize',state.markChanged,{passive:true});}" +
                "if(window.ResizeObserver){" +
                    "state.resizeObserver=new ResizeObserver(state.markChanged);" +
                    "[root,body,roleShell,activePanel].forEach(function(node){if(node){state.resizeObserver.observe(node);}});" +
                "}" +
                "if(window.MutationObserver&&root){" +
                    "state.mutationObserver=new MutationObserver(state.markChanged);" +
                    "state.mutationObserver.observe(root,{subtree:true,childList:true,attributes:true," +
                        "attributeFilter:['class','style','hidden','data-driver-shell-bound','data-driver-density','data-eo-initialized','data-eo-active-tab']});" +
                "}" +
                "state.destroy=function(){" +
                    "state.destroyed=true;window.removeEventListener('resize',state.markChanged);" +
                    "if(viewport){viewport.removeEventListener('resize',state.markChanged);}" +
                    "if(state.resizeObserver){state.resizeObserver.disconnect();}" +
                    "if(state.mutationObserver){state.mutationObserver.disconnect();}" +
                "};" +
            "}" +
            "if(state.snapshot===snapshot){state.stableFrames+=1;}else{" +
                "state.snapshot=snapshot;state.stableFrames=0;state.lastChange=now;" +
            "}" +
            "var fontsReady=!document.fonts||document.fonts.status==='loaded';" +
            "var imagesReady=!document.images||Array.prototype.every.call(document.images,function(image){" +
                "return image.loading==='lazy'||image.complete;});" +
            "var pageVisible=document.visibilityState==='visible';" +
            "return [document.readyState,snapshot,state.stableFrames,now-state.lastChange," +
                "fontsReady?'1':'0',imagesReady?'1':'0',roleReady?'1':'0',pageVisible?'1':'0'].join('|');" +
        "})()";

    private final Activity activity;
    private final FrameLayout overlay;
    private final ScrollView contentScroll;
    private final LinearLayout content;
    private final ImageView icon;
    private final TextView title;
    private final SyncSpinnerView spinner;
    private final TextView recoveryMessage;
    private final Button retryButton;
    private final View.OnLayoutChangeListener webViewLayoutChangeListener;
    private WebView webView;
    private boolean destroyed;
    private boolean visible;
    private boolean pageLoaded;
    private boolean recoveryVisible;
    private boolean pageErrorObserved;
    private boolean watchdogGraceUsed;
    private boolean hostResumed;
    private boolean windowFocused;
    private boolean waitForImeClose;
    private int pageGeneration;
    private String nativeLayoutSnapshot = "";
    private int nativeStableFrames;
    private long nativeLastChangeMs;
    private Runnable startupWatchdog;
    private Runnable recoveryWatchdog;
    private int recoveryWatchdogGeneration = -1;
    private boolean webViewInteractionSuppressed;
    private int savedWebViewImportantForAccessibility;
    private int savedWebViewDescendantFocusability;
    private boolean savedWebViewFocusable;
    private boolean savedWebViewFocusableInTouchMode;
    private Runnable pageRevealedListener;

    private StartupLoadingOverlay(Activity activity) {
        this.activity = activity;
        int backgroundColor = Color.parseColor(BuildConfig.SPLASH_BACKGROUND_COLOR);
        int accentColor = Color.parseColor(BuildConfig.SPLASH_ACCENT_COLOR);

        overlay = new FrameLayout(activity);
        overlay.setBackgroundColor(backgroundColor);
        overlay.setClickable(true);
        overlay.setFocusable(true);
        overlay.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_YES);
        overlay.setContentDescription("Загрузка приложения " + BuildConfig.APP_DISPLAY_NAME);
        overlay.setElevation(dp(32));

        content = new LinearLayout(activity);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setGravity(Gravity.CENTER);

        icon = new ImageView(activity);
        icon.setImageDrawable(resolveProfileIcon(activity));
        icon.setScaleType(ImageView.ScaleType.FIT_CENTER);
        content.addView(icon, linearParams(112, 112, 0, 0, 0, 18));

        title = new TextView(activity);
        title.setText(BuildConfig.APP_DISPLAY_NAME);
        title.setTextColor(Color.rgb(247, 250, 252));
        title.setTextSize(22f);
        title.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        title.setGravity(Gravity.CENTER);
        content.addView(title, linearParams(
            ViewGroup.LayoutParams.WRAP_CONTENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
            0,
            0,
            0,
            18
        ));

        spinner = new SyncSpinnerView(activity, accentColor);
        content.addView(spinner, linearParams(42, 42, 0, 0, 0, 0));

        recoveryMessage = new TextView(activity);
        recoveryMessage.setTextColor(Color.rgb(226, 232, 240));
        recoveryMessage.setTextSize(16f);
        recoveryMessage.setGravity(Gravity.CENTER);
        recoveryMessage.setMaxWidth(dp(300));
        recoveryMessage.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_ASSERTIVE);
        recoveryMessage.setVisibility(View.GONE);
        content.addView(recoveryMessage, linearParams(
            ViewGroup.LayoutParams.WRAP_CONTENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
            0,
            0,
            0,
            20
        ));

        retryButton = new Button(activity);
        retryButton.setText("Повторить");
        retryButton.setTextColor(backgroundColor);
        retryButton.setTextSize(16f);
        retryButton.setAllCaps(false);
        retryButton.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        GradientDrawable retryBackground = new GradientDrawable();
        retryBackground.setColor(accentColor);
        retryBackground.setCornerRadius(dp(12));
        retryButton.setBackground(retryBackground);
        retryButton.setMinHeight(dp(48));
        retryButton.setMinWidth(dp(172));
        retryButton.setFocusable(true);
        retryButton.setVisibility(View.GONE);
        retryButton.setOnClickListener(ignored -> retryCurrentPage());
        content.addView(retryButton, linearParams(
            ViewGroup.LayoutParams.WRAP_CONTENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
            0,
            0,
            0,
            0
        ));

        contentScroll = new ScrollView(activity);
        contentScroll.setFillViewport(true);
        contentScroll.setClipToPadding(false);
        contentScroll.setOverScrollMode(View.OVER_SCROLL_IF_CONTENT_SCROLLS);
        contentScroll.addView(content, new ScrollView.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT
        ));
        overlay.addView(contentScroll, new FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.MATCH_PARENT
        ));

        ViewCompat.setOnApplyWindowInsetsListener(overlay, (view, windowInsets) -> {
            Insets safeInsets = windowInsets.getInsets(
                WindowInsetsCompat.Type.systemBars()
                    | WindowInsetsCompat.Type.displayCutout()
                    | WindowInsetsCompat.Type.ime()
            );
            int horizontalPadding = dp(16);
            int verticalPadding = dp(12);
            int paddingLeft = safeInsets.left + horizontalPadding;
            int paddingTop = safeInsets.top + verticalPadding;
            int paddingRight = safeInsets.right + horizontalPadding;
            int paddingBottom = safeInsets.bottom + verticalPadding;
            if (contentScroll.getPaddingLeft() != paddingLeft
                    || contentScroll.getPaddingTop() != paddingTop
                    || contentScroll.getPaddingRight() != paddingRight
                    || contentScroll.getPaddingBottom() != paddingBottom) {
                contentScroll.setPadding(paddingLeft, paddingTop, paddingRight, paddingBottom);
            }
            updateContentDensity(
                Math.max(0, view.getWidth() - contentScroll.getPaddingLeft() - contentScroll.getPaddingRight()),
                Math.max(0, view.getHeight() - contentScroll.getPaddingTop() - contentScroll.getPaddingBottom())
            );
            return windowInsets;
        });
        overlay.addOnLayoutChangeListener((
                view,
                left,
                top,
                right,
                bottom,
                oldLeft,
                oldTop,
                oldRight,
                oldBottom) -> {
            if (left == oldLeft && top == oldTop && right == oldRight && bottom == oldBottom) {
                return;
            }
            updateContentDensity(
                Math.max(0, right - left - contentScroll.getPaddingLeft() - contentScroll.getPaddingRight()),
                Math.max(0, bottom - top - contentScroll.getPaddingTop() - contentScroll.getPaddingBottom())
            );
        }
        );

        webViewLayoutChangeListener = (
                view,
                left,
                top,
                right,
                bottom,
                oldLeft,
                oldTop,
                oldRight,
                oldBottom) -> {
            if (left != oldLeft || top != oldTop || right != oldRight || bottom != oldBottom) {
                markNativeLayoutChanged();
            }
        };
    }

    static StartupLoadingOverlay attach(Activity activity, WebView webView) {
        StartupLoadingOverlay controller = new StartupLoadingOverlay(activity);
        controller.bindWebView(webView);
        controller.show();
        activity.getWindow().setStatusBarColor(Color.parseColor(BuildConfig.SPLASH_BACKGROUND_COLOR));
        activity.getWindow().setNavigationBarColor(Color.parseColor(BuildConfig.SPLASH_BACKGROUND_COLOR));
        return controller;
    }

    void onPageStarted(WebView loadingWebView) {
        if (destroyed || loadingWebView == null) {
            return;
        }
        pageGeneration += 1;
        pageLoaded = false;
        pageErrorObserved = false;
        watchdogGraceUsed = false;
        recoveryVisible = false;
        bindWebView(loadingWebView);
        WindowInsetsCompat rootInsets = ViewCompat.getRootWindowInsets(loadingWebView);
        waitForImeClose = rootInsets != null && rootInsets.isVisible(WindowInsetsCompat.Type.ime());
        resetNativeLayoutStability();
        show();
    }

    void onPageLoaded(WebView loadedWebView) {
        if (destroyed || loadedWebView == null) {
            return;
        }
        bindWebView(loadedWebView);
        if (!visible || recoveryVisible) {
            return;
        }
        pageLoaded = true;
        restartProbeIfReady();
    }

    void onPageError(WebView erroredWebView) {
        if (destroyed || !visible || recoveryVisible || erroredWebView == null || erroredWebView != webView) {
            return;
        }
        pageErrorObserved = true;
    }

    void onHostResumed() {
        if (destroyed) {
            return;
        }
        hostResumed = true;
        cancelWatchdogs();
        restartProbeOrArmWatchdog();
    }

    void onHostPaused() {
        if (destroyed) {
            return;
        }
        hostResumed = false;
        cancelPendingProbe();
    }

    void onWindowFocusChanged(boolean hasFocus) {
        if (destroyed) {
            return;
        }
        windowFocused = hasFocus;
        if (hasFocus) {
            cancelWatchdogs();
            restartProbeOrArmWatchdog();
        } else {
            cancelPendingProbe();
        }
    }

    void setPageRevealedListener(Runnable listener) {
        pageRevealedListener = listener;
    }

    void destroy() {
        destroyed = true;
        pageGeneration += 1;
        cancelWatchdogs();
        spinner.stop();
        overlay.animate().cancel();
        if (webView != null) {
            webView.removeOnLayoutChangeListener(webViewLayoutChangeListener);
            restoreWebViewInteraction();
            webView.setAlpha(1f);
            webView.setVisibility(View.VISIBLE);
        }
        if (overlay.getParent() instanceof ViewGroup parent) {
            parent.removeView(overlay);
        }
    }

    private void bindWebView(WebView nextWebView) {
        if (webView == nextWebView) {
            return;
        }
        if (webView != null) {
            cancelWatchdogs();
            webView.removeOnLayoutChangeListener(webViewLayoutChangeListener);
            restoreWebViewInteraction();
        }
        webView = nextWebView;
        webViewInteractionSuppressed = false;
        if (webView != null) {
            webView.addOnLayoutChangeListener(webViewLayoutChangeListener);
        }
    }

    private void show() {
        if (destroyed || webView == null) {
            return;
        }
        overlay.animate().cancel();
        overlay.setAlpha(1f);
        if (!(overlay.getParent() instanceof ViewGroup)) {
            ViewGroup root = activity.findViewById(android.R.id.content);
            root.addView(overlay, new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            ));
        }
        overlay.bringToFront();
        visible = true;
        showLoadingState();
        webView.setVisibility(View.VISIBLE);
        suppressWebViewInteraction();
        webView.setAlpha(0f);
        cancelWatchdogs();
        armStartupWatchdog(pageGeneration, STARTUP_WATCHDOG_MS);
        ViewCompat.requestApplyInsets(overlay);
    }

    private void runStartupWatchdog(int generation, WebView watchedWebView) {
        if (destroyed || !visible || recoveryVisible || webView == null || watchedWebView != webView) {
            return;
        }
        if (generation != pageGeneration) {
            ensureStartupWatchdogArmed(STARTUP_WATCHDOG_RETRY_MS);
            return;
        }
        if (!hostResumed || !windowFocused || !activity.hasWindowFocus()) {
            return;
        }
        /* Keep a separate recovery task armed while evaluateJavascript is
           outstanding; some WebView builds can omit its callback on navigation. */
        armRecoveryWatchdog(generation, DiagnosticReason.ERROR);
        try {
            webView.evaluateJavascript(READINESS_PROBE, encodedResult -> {
                cancelRecoveryWatchdog(generation);
                if (!isCurrentVisibleGeneration(generation)) {
                    ensureStartupWatchdogArmed(STARTUP_WATCHDOG_RETRY_MS);
                    return;
                }
                Readiness readiness = parseReadiness(encodedResult);
                if (!readiness.valid()) {
                    enterRecovery(generation, DiagnosticReason.ERROR);
                    return;
                }
                if (!pageLoaded && readiness.documentComplete() && !watchdogGraceUsed) {
                    /* Capacitor can lose onPageLoaded for a cached document. Give the
                       ordinary probe one short, bounded chance instead of revealing it. */
                    pageLoaded = true;
                    watchdogGraceUsed = true;
                    restartProbeIfReady();
                    return;
                }
                if (pageLoaded && sampleNativeLayoutReadiness() && readiness.isReady()) {
                    awaitCommittedVisualState(generation);
                    armRecoveryWatchdog(generation, DiagnosticReason.WATCHDOG);
                    return;
                }
                enterRecovery(
                    generation,
                    pageErrorObserved ? DiagnosticReason.ERROR : DiagnosticReason.WATCHDOG
                );
            });
        } catch (RuntimeException error) {
            enterRecovery(generation, DiagnosticReason.ERROR);
        }
    }

    private void restartProbeIfReady() {
        if (!canProbe()) {
            ensureStartupWatchdogArmed(STARTUP_WATCHDOG_MS);
            return;
        }
        int generation = ++pageGeneration;
        cancelRecoveryWatchdog();
        armStartupWatchdog(generation, STARTUP_WATCHDOG_MS);
        resetNativeLayoutStability();
        View parent = webView.getParent() instanceof View ? (View) webView.getParent() : webView;
        ViewCompat.requestApplyInsets(parent);
        webView.requestLayout();
        webView.invalidate();
        webView.postOnAnimation(() -> {
            if (!isCurrentProbe(generation)) {
                return;
            }
            try {
                webView.evaluateJavascript(PREPARE_LAYOUT, ignored -> {
                    if (!isCurrentProbe(generation)) {
                        return;
                    }
                    probeOnNextFrame(generation);
                });
            } catch (RuntimeException error) {
                enterRecovery(generation, DiagnosticReason.ERROR);
            }
        });
    }

    private void restartProbeOrArmWatchdog() {
        if (canProbe()) {
            restartProbeIfReady();
            return;
        }
        ensureStartupWatchdogArmed(STARTUP_WATCHDOG_MS);
    }

    private void cancelPendingProbe() {
        pageGeneration += 1;
        resetNativeLayoutStability();
        cancelWatchdogs();
    }

    private void armStartupWatchdog(int generation, long delayMs) {
        cancelStartupWatchdog();
        if (destroyed
                || !visible
                || recoveryVisible
                || !hostResumed
                || !windowFocused
                || !activity.hasWindowFocus()
                || webView == null) {
            return;
        }
        WebView watchedWebView = webView;
        Runnable task = new Runnable() {
            @Override
            public void run() {
                if (startupWatchdog != this) {
                    return;
                }
                startupWatchdog = null;
                runStartupWatchdog(generation, watchedWebView);
            }
        };
        startupWatchdog = task;
        watchedWebView.postDelayed(task, Math.max(1L, delayMs));
    }

    private void ensureStartupWatchdogArmed(long delayMs) {
        if (startupWatchdog == null
                && recoveryWatchdog == null
                && !destroyed
                && visible
                && !recoveryVisible
                && hostResumed
                && windowFocused
                && activity.hasWindowFocus()
                && webView != null) {
            armStartupWatchdog(pageGeneration, delayMs);
        }
    }

    private void cancelStartupWatchdog() {
        if (startupWatchdog == null) {
            return;
        }
        if (webView != null) {
            webView.removeCallbacks(startupWatchdog);
        }
        startupWatchdog = null;
    }

    private void armRecoveryWatchdog(int generation, DiagnosticReason reason) {
        cancelStartupWatchdog();
        cancelRecoveryWatchdog();
        if (!isCurrentVisibleGeneration(generation) || webView == null) {
            return;
        }
        WebView watchedWebView = webView;
        Runnable task = new Runnable() {
            @Override
            public void run() {
                if (recoveryWatchdog != this) {
                    return;
                }
                recoveryWatchdog = null;
                recoveryWatchdogGeneration = -1;
                if (watchedWebView == webView) {
                    enterRecovery(generation, reason);
                }
            }
        };
        recoveryWatchdog = task;
        recoveryWatchdogGeneration = generation;
        watchedWebView.postDelayed(task, STARTUP_WATCHDOG_RETRY_MS);
    }

    private void cancelRecoveryWatchdog(int generation) {
        if (recoveryWatchdogGeneration == generation) {
            cancelRecoveryWatchdog();
        }
    }

    private void cancelRecoveryWatchdog() {
        if (recoveryWatchdog == null) {
            return;
        }
        if (webView != null) {
            webView.removeCallbacks(recoveryWatchdog);
        }
        recoveryWatchdog = null;
        recoveryWatchdogGeneration = -1;
    }

    private void cancelWatchdogs() {
        cancelStartupWatchdog();
        cancelRecoveryWatchdog();
    }

    private boolean canProbe() {
        return !destroyed
            && visible
            && !recoveryVisible
            && pageLoaded
            && hostResumed
            && windowFocused
            && activity.hasWindowFocus()
            && webView != null
            && !activity.isFinishing();
    }

    private boolean isCurrentProbe(int generation) {
        return generation == pageGeneration && canProbe();
    }

    private boolean isCurrentVisibleGeneration(int generation) {
        return !destroyed
            && visible
            && !recoveryVisible
            && generation == pageGeneration
            && webView != null
            && !activity.isFinishing();
    }

    private void probeOnNextFrame(int generation) {
        if (!isCurrentProbe(generation)) {
            return;
        }
        webView.postOnAnimation(() -> {
            if (!isCurrentProbe(generation)) {
                return;
            }
            boolean nativeLayoutReady = sampleNativeLayoutReadiness();
            try {
                webView.evaluateJavascript(READINESS_PROBE, encodedResult -> {
                    if (!isCurrentProbe(generation)) {
                        return;
                    }
                    Readiness readiness = parseReadiness(encodedResult);
                    if (nativeLayoutReady && readiness.isReady()) {
                        awaitCommittedVisualState(generation);
                        return;
                    }
                    probeOnNextFrame(generation);
                });
            } catch (RuntimeException error) {
                enterRecovery(generation, DiagnosticReason.ERROR);
            }
        });
    }

    private void awaitCommittedVisualState(int generation) {
        if (!isCurrentProbe(generation)) {
            return;
        }
        try {
            webView.postVisualStateCallback(generation, new WebView.VisualStateCallback() {
                @Override
                public void onComplete(long requestId) {
                    if (requestId != generation || !isCurrentProbe(generation)) {
                        return;
                    }
                    webView.postOnAnimation(() -> {
                        if (!isCurrentProbe(generation)) {
                            return;
                        }
                        try {
                            webView.evaluateJavascript(READINESS_PROBE, encodedResult -> {
                                cancelRecoveryWatchdog(generation);
                                if (!isCurrentProbe(generation)) {
                                    return;
                                }
                                Readiness readiness = parseReadiness(encodedResult);
                                if (!readiness.valid()) {
                                    enterRecovery(generation, DiagnosticReason.ERROR);
                                    return;
                                }
                                if (!sampleNativeLayoutReadiness() || !readiness.isReady()) {
                                    ensureStartupWatchdogArmed(STARTUP_WATCHDOG_RETRY_MS);
                                    probeOnNextFrame(generation);
                                    return;
                                }
                                cancelWatchdogs();
                                recordDiagnostic(DiagnosticReason.NORMAL, "reveal", generation);
                                restoreWebViewInteraction();
                                webView.setAlpha(1f);
                                webView.postOnAnimation(() -> dismiss(generation));
                            });
                        } catch (RuntimeException error) {
                            enterRecovery(generation, DiagnosticReason.ERROR);
                        }
                    });
                }
            });
        } catch (RuntimeException error) {
            enterRecovery(generation, DiagnosticReason.ERROR);
        }
    }

    private boolean sampleNativeLayoutReadiness() {
        if (webView == null
                || !ViewCompat.isAttachedToWindow(webView)
                || !ViewCompat.isLaidOut(webView)
                || webView.getWidth() <= 0
                || webView.getHeight() <= 0) {
            markNativeLayoutChanged();
            return false;
        }

        WindowInsetsCompat rootInsets = ViewCompat.getRootWindowInsets(webView);
        if (rootInsets == null) {
            markNativeLayoutChanged();
            return false;
        }

        Insets systemBars = rootInsets.getInsets(
            WindowInsetsCompat.Type.systemBars() | WindowInsetsCompat.Type.displayCutout()
        );
        Insets ime = rootInsets.getInsets(WindowInsetsCompat.Type.ime());
        boolean imeVisible = rootInsets.isVisible(WindowInsetsCompat.Type.ime());
        /* При переходе из поля телефона к PIN старая IME закрывается
           нативным listener. После первого подтверждённого закрытия нельзя
           вечно ждать !imeVisible: новое поле имеет право открыть свою
           клавиатуру. Изменение её insets всё равно сбросит layout stability. */
        if (waitForImeClose && !imeVisible) {
            waitForImeClose = false;
        }
        View parent = webView.getParent() instanceof View ? (View) webView.getParent() : webView;
        String snapshot = webView.getWidth() + "x" + webView.getHeight()
            + "|" + parent.getWidth() + "x" + parent.getHeight()
            + "|" + parent.getPaddingLeft() + "," + parent.getPaddingTop()
            + "," + parent.getPaddingRight() + "," + parent.getPaddingBottom()
            + "|" + systemBars.left + "," + systemBars.top + "," + systemBars.right + "," + systemBars.bottom
            + "|" + ime.left + "," + ime.top + "," + ime.right + "," + ime.bottom
            + "|" + (imeVisible ? "ime" : "clear");

        long now = SystemClock.uptimeMillis();
        if (snapshot.equals(nativeLayoutSnapshot)) {
            nativeStableFrames += 1;
        } else {
            nativeLayoutSnapshot = snapshot;
            nativeStableFrames = 0;
            nativeLastChangeMs = now;
        }

        return nativeStableFrames >= REQUIRED_NATIVE_STABLE_FRAMES
            && now - nativeLastChangeMs >= REQUIRED_NATIVE_QUIET_WINDOW_MS
            && !waitForImeClose;
    }

    private void markNativeLayoutChanged() {
        nativeStableFrames = 0;
        nativeLastChangeMs = SystemClock.uptimeMillis();
    }

    private void resetNativeLayoutStability() {
        nativeLayoutSnapshot = "";
        nativeStableFrames = 0;
        nativeLastChangeMs = SystemClock.uptimeMillis();
    }

    private Readiness parseReadiness(String encodedResult) {
        try {
            Object decoded = new JSONTokener(encodedResult).nextValue();
            if (!(decoded instanceof String value)) {
                return Readiness.invalid();
            }
            String[] parts = value.split("\\|", -1);
            if (parts.length != 8) {
                return Readiness.invalid();
            }
            String[] dimensions = parts[1].split("x", -1);
            if (dimensions.length < 5) {
                return Readiness.invalid();
            }
            double viewportScale = Double.parseDouble(dimensions[2]);
            boolean hasViewport = Double.parseDouble(dimensions[0]) > 0
                && Double.parseDouble(dimensions[1]) > 0
                && viewportScale >= 0.98
                && viewportScale <= 1.02;
            return new Readiness(
                true,
                "complete".equals(parts[0]),
                hasViewport,
                Integer.parseInt(parts[2]),
                Long.parseLong(parts[3]),
                "1".equals(parts[4]),
                "1".equals(parts[5]),
                "1".equals(parts[6]),
                "1".equals(parts[7])
            );
        } catch (Exception ignored) {
            return Readiness.invalid();
        }
    }

    private void showLoadingState() {
        recoveryVisible = false;
        recoveryMessage.setVisibility(View.GONE);
        retryButton.setEnabled(true);
        retryButton.setVisibility(View.GONE);
        spinner.setVisibility(View.VISIBLE);
        spinner.start();
        overlay.setContentDescription("Загрузка приложения " + BuildConfig.APP_DISPLAY_NAME);
        overlay.requestFocus();
    }

    private void enterRecovery(int generation, DiagnosticReason reason) {
        if (!isCurrentVisibleGeneration(generation)) {
            return;
        }
        pageGeneration += 1;
        recoveryVisible = true;
        cancelWatchdogs();
        spinner.stop();
        spinner.setVisibility(View.GONE);
        webView.setVisibility(View.VISIBLE);
        suppressWebViewInteraction();
        webView.setAlpha(0f);
        recoveryMessage.setText(
            reason == DiagnosticReason.ERROR
                ? "Не удалось загрузить экран приложения. Проверьте соединение и повторите попытку."
                : "Экран не успел подготовиться. Проверьте соединение и повторите загрузку."
        );
        recoveryMessage.setVisibility(View.VISIBLE);
        retryButton.setEnabled(true);
        retryButton.setVisibility(View.VISIBLE);
        overlay.setContentDescription(recoveryMessage.getText());
        overlay.bringToFront();
        recordDiagnostic(reason, "recovery", pageGeneration);
        int recoveryGeneration = pageGeneration;
        retryButton.post(() -> {
            if (destroyed || !recoveryVisible || recoveryGeneration != pageGeneration) {
                return;
            }
            retryButton.requestFocus();
            overlay.announceForAccessibility(recoveryMessage.getText());
        });
    }

    private void retryCurrentPage() {
        if (destroyed || webView == null || !recoveryVisible) {
            return;
        }
        int generation = ++pageGeneration;
        pageLoaded = false;
        pageErrorObserved = false;
        watchdogGraceUsed = false;
        waitForImeClose = false;
        resetNativeLayoutStability();
        showLoadingState();
        webView.setVisibility(View.VISIBLE);
        suppressWebViewInteraction();
        webView.setAlpha(0f);
        cancelWatchdogs();
        armStartupWatchdog(generation, STARTUP_WATCHDOG_MS);
        try {
            webView.reload();
        } catch (RuntimeException error) {
            enterRecovery(generation, DiagnosticReason.ERROR);
        }
    }

    private void recordDiagnostic(DiagnosticReason reason, String state, int generation) {
        String message = "startup_overlay state=" + state
            + " reason=" + reason.value
            + " generation=" + generation;
        if (reason == DiagnosticReason.NORMAL) {
            Log.i(LOG_TAG, message);
        } else {
            Log.w(LOG_TAG, message);
        }
    }

    private void dismiss(int generation) {
        if (!isCurrentProbe(generation)) {
            return;
        }
        visible = false;
        cancelWatchdogs();
        spinner.stop();
        restoreWebViewInteraction();
        overlay.animate().cancel();
        overlay.animate()
            .alpha(0f)
            .setDuration(FADE_DURATION_MS)
            .withEndAction(() -> {
                if (destroyed || visible || generation != pageGeneration) {
                    return;
                }
                if (overlay.getParent() instanceof ViewGroup parent) {
                    parent.removeView(overlay);
                }
                notifyPageRevealed(generation);
            })
            .start();
    }

    private void notifyPageRevealed(int generation) {
        if (destroyed || visible || generation != pageGeneration) {
            return;
        }
        Runnable listener = pageRevealedListener;
        if (listener != null) {
            listener.run();
        }
    }

    private void suppressWebViewInteraction() {
        if (webView == null || webViewInteractionSuppressed) {
            return;
        }
        savedWebViewImportantForAccessibility = webView.getImportantForAccessibility();
        savedWebViewDescendantFocusability = webView.getDescendantFocusability();
        savedWebViewFocusable = webView.isFocusable();
        savedWebViewFocusableInTouchMode = webView.isFocusableInTouchMode();
        webView.clearFocus();
        webView.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO_HIDE_DESCENDANTS);
        webView.setDescendantFocusability(ViewGroup.FOCUS_BLOCK_DESCENDANTS);
        webView.setFocusable(false);
        webView.setFocusableInTouchMode(false);
        webViewInteractionSuppressed = true;
    }

    private void restoreWebViewInteraction() {
        if (webView == null || !webViewInteractionSuppressed) {
            return;
        }
        webView.setImportantForAccessibility(savedWebViewImportantForAccessibility);
        webView.setDescendantFocusability(savedWebViewDescendantFocusability);
        webView.setFocusable(savedWebViewFocusable);
        webView.setFocusableInTouchMode(savedWebViewFocusableInTouchMode);
        webViewInteractionSuppressed = false;
    }

    private void updateContentDensity(int availableWidth, int availableHeight) {
        float fontScale = activity.getResources().getConfiguration().fontScale;
        boolean compact = fontScale >= 1.5f
            || (availableWidth > 0 && availableHeight > 0 && availableWidth > availableHeight)
            || (availableHeight > 0 && availableHeight < dp(520));
        int iconSize = compact ? 64 : 112;
        int iconSpacing = compact ? 10 : 18;
        int titleSpacing = compact ? 12 : 18;
        int messageSpacing = compact ? 12 : 20;
        updateLinearParams(icon, iconSize, iconSize, iconSpacing);
        updateLinearParams(
            title,
            ViewGroup.LayoutParams.WRAP_CONTENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
            titleSpacing
        );
        updateLinearParams(
            recoveryMessage,
            ViewGroup.LayoutParams.WRAP_CONTENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
            messageSpacing
        );
        int messageMaxWidth = availableWidth > 0
            ? Math.max(dp(160), Math.min(availableWidth, dp(360)))
            : dp(300);
        if (recoveryMessage.getMaxWidth() != messageMaxWidth) {
            recoveryMessage.setMaxWidth(messageMaxWidth);
        }
        int contentPadding = dp(compact ? 8 : 20);
        if (content.getPaddingLeft() != contentPadding
                || content.getPaddingTop() != contentPadding
                || content.getPaddingRight() != contentPadding
                || content.getPaddingBottom() != contentPadding) {
            content.setPadding(contentPadding, contentPadding, contentPadding, contentPadding);
        }
    }

    private void updateLinearParams(View view, int widthDp, int heightDp, int bottomDp) {
        LinearLayout.LayoutParams params = (LinearLayout.LayoutParams) view.getLayoutParams();
        int width = widthDp == ViewGroup.LayoutParams.WRAP_CONTENT ? widthDp : dp(widthDp);
        int height = heightDp == ViewGroup.LayoutParams.WRAP_CONTENT ? heightDp : dp(heightDp);
        int bottom = dp(bottomDp);
        if (params.width != width
                || params.height != height
                || params.leftMargin != 0
                || params.topMargin != 0
                || params.rightMargin != 0
                || params.bottomMargin != bottom) {
            params.width = width;
            params.height = height;
            params.setMargins(0, 0, 0, bottom);
            view.setLayoutParams(params);
        }
    }

    private Drawable resolveProfileIcon(Activity activity) {
        int resourceId = activity.getResources().getIdentifier(
            BuildConfig.SPLASH_ICON_RESOURCE,
            "drawable",
            activity.getPackageName()
        );
        if (resourceId != 0) {
            return activity.getDrawable(resourceId);
        }
        return activity.getApplicationInfo().loadIcon(activity.getPackageManager());
    }

    private LinearLayout.LayoutParams linearParams(
            int widthDp,
            int heightDp,
            int leftDp,
            int topDp,
            int rightDp,
            int bottomDp) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
            widthDp == ViewGroup.LayoutParams.WRAP_CONTENT ? widthDp : dp(widthDp),
            heightDp == ViewGroup.LayoutParams.WRAP_CONTENT ? heightDp : dp(heightDp)
        );
        params.setMargins(dp(leftDp), dp(topDp), dp(rightDp), dp(bottomDp));
        return params;
    }

    private int dp(int value) {
        return Math.round(value * activity.getResources().getDisplayMetrics().density);
    }

    private record Readiness(
            boolean valid,
            boolean documentComplete,
            boolean hasViewport,
            int stableFrames,
            long quietWindowMs,
            boolean fontsReady,
            boolean imagesReady,
            boolean roleReady,
            boolean pageVisible) {
        static Readiness invalid() {
            return new Readiness(false, false, false, 0, 0L, false, false, false, false);
        }

        boolean isReady() {
            return documentComplete
                && hasViewport
                && fontsReady
                && imagesReady
                && roleReady
                && pageVisible
                && stableFrames >= REQUIRED_STABLE_FRAMES
                && quietWindowMs >= REQUIRED_QUIET_WINDOW_MS;
        }
    }

    private enum DiagnosticReason {
        NORMAL("normal"),
        WATCHDOG("watchdog"),
        ERROR("error");

        final String value;

        DiagnosticReason(String value) {
            this.value = value;
        }
    }

    private static final class SyncSpinnerView extends View {
        private final Paint trackPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint accentPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final RectF bounds = new RectF();
        private final float strokeWidth;
        private final ValueAnimator animator;
        private float rotation;

        SyncSpinnerView(Activity activity, int accentColor) {
            super(activity);
            strokeWidth = 4f * activity.getResources().getDisplayMetrics().density;
            trackPaint.setStyle(Paint.Style.STROKE);
            trackPaint.setStrokeWidth(strokeWidth);
            trackPaint.setColor(Color.argb(Math.round(255f * 0.34f), 148, 163, 184));
            accentPaint.setStyle(Paint.Style.STROKE);
            accentPaint.setStrokeWidth(strokeWidth);
            accentPaint.setStrokeCap(Paint.Cap.ROUND);
            accentPaint.setColor(accentColor);
            animator = ValueAnimator.ofFloat(0f, 360f);
            animator.setDuration(900L);
            animator.setRepeatCount(ValueAnimator.INFINITE);
            animator.setInterpolator(new LinearInterpolator());
            animator.addUpdateListener(valueAnimator -> {
                rotation = (float) valueAnimator.getAnimatedValue();
                invalidate();
            });
        }

        void start() {
            if (!animator.isStarted()) {
                animator.start();
            }
        }

        void stop() {
            animator.cancel();
        }

        @Override
        protected void onDraw(Canvas canvas) {
            super.onDraw(canvas);
            float inset = strokeWidth / 2f;
            bounds.set(inset, inset, getWidth() - inset, getHeight() - inset);
            canvas.drawOval(bounds, trackPaint);
            canvas.drawArc(bounds, rotation - 90f, 92f, false, accentPaint);
        }
    }
}
