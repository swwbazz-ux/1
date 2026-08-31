package ru.copperresources.mobile;

import android.animation.ValueAnimator;
import android.app.Activity;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.RectF;
import android.graphics.Typeface;
import android.graphics.drawable.Drawable;
import android.os.SystemClock;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.animation.LinearInterpolator;
import android.webkit.WebView;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.TextView;

import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;

import org.json.JSONTokener;

final class StartupLoadingOverlay {
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
            "var activePanel=roleShell&&roleShell.querySelector('[data-driver-tab-panel].is-active,[data-eo-screen]:not([hidden])');" +
            "var metric=function(value){value=Number(value)||0;return Math.round(value*10)/10;};" +
            "var rect=function(node){var value=node&&node.getBoundingClientRect?node.getBoundingClientRect():null;" +
                "return value?[metric(value.left),metric(value.top),metric(value.width),metric(value.height)].join(','):'0,0,0,0';};" +
            "var width=metric(viewport?viewport.width:window.innerWidth);" +
            "var height=metric(viewport?viewport.height:window.innerHeight);" +
            "var scale=Math.round((Number(viewport?viewport.scale:1)||0)*1000)/1000;" +
            "var rootStyle=root?getComputedStyle(root):null;" +
            "var shellStyle=roleShell?getComputedStyle(roleShell):null;" +
            "var driverHeight=rootStyle?rootStyle.getPropertyValue('--driver-viewport-h').trim():'';" +
            "var excavatorHeight=rootStyle?rootStyle.getPropertyValue('--eo-app-height').trim():'';" +
            "var roleReady=true;" +
            "if(driverShell){roleReady=driverShell.dataset.driverShellBound==='true'&&!!driverHeight" +
                "&&Math.abs((parseFloat(driverHeight)||0)-driverShell.getBoundingClientRect().height)<=2" +
                "&&!window.driverViewportFitFrame&&!window.driverDialLabelFitFrame;}" +
            "if(excavatorShell){roleReady=excavatorShell.dataset.eoInitialized==='1'&&!!excavatorHeight" +
                "&&Math.abs((parseFloat(excavatorHeight)||0)-excavatorShell.getBoundingClientRect().height)<=2;}" +
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
    private final SyncSpinnerView spinner;
    private final View.OnLayoutChangeListener webViewLayoutChangeListener;
    private WebView webView;
    private boolean destroyed;
    private boolean visible;
    private boolean pageLoaded;
    private boolean hostResumed;
    private boolean windowFocused;
    private boolean waitForImeClose;
    private int pageGeneration;
    private String nativeLayoutSnapshot = "";
    private int nativeStableFrames;
    private long nativeLastChangeMs;
    private final Runnable startupWatchdog = this::runStartupWatchdog;

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

        LinearLayout content = new LinearLayout(activity);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setGravity(Gravity.CENTER_HORIZONTAL);

        ImageView icon = new ImageView(activity);
        icon.setImageDrawable(resolveProfileIcon(activity));
        icon.setScaleType(ImageView.ScaleType.FIT_CENTER);
        content.addView(icon, linearParams(128, 128, 0, 0, 0, 24));

        TextView title = new TextView(activity);
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
            24
        ));

        spinner = new SyncSpinnerView(activity, accentColor);
        content.addView(spinner, linearParams(42, 42, 0, 0, 0, 0));

        FrameLayout.LayoutParams contentParams = new FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.WRAP_CONTENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
            Gravity.CENTER
        );
        overlay.addView(content, contentParams);

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
        if (!visible) {
            return;
        }
        pageLoaded = true;
        restartProbeIfReady();
    }

    void onHostResumed() {
        if (destroyed) {
            return;
        }
        hostResumed = true;
        restartProbeIfReady();
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
            restartProbeIfReady();
        } else {
            cancelPendingProbe();
        }
    }

    void destroy() {
        destroyed = true;
        pageGeneration += 1;
        spinner.stop();
        overlay.animate().cancel();
        if (webView != null) {
            webView.removeCallbacks(startupWatchdog);
            webView.removeOnLayoutChangeListener(webViewLayoutChangeListener);
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
            webView.removeOnLayoutChangeListener(webViewLayoutChangeListener);
        }
        webView = nextWebView;
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
        spinner.start();
        webView.setVisibility(View.VISIBLE);
        webView.setAlpha(0f);
        webView.removeCallbacks(startupWatchdog);
        webView.postDelayed(startupWatchdog, STARTUP_WATCHDOG_MS);
    }

    private void runStartupWatchdog() {
        if (destroyed || !visible || webView == null) {
            return;
        }
        if (!hostResumed || !windowFocused || !activity.hasWindowFocus()) {
            webView.postDelayed(startupWatchdog, STARTUP_WATCHDOG_RETRY_MS);
            return;
        }
        int generation = pageGeneration;
        webView.evaluateJavascript("document.readyState", encodedReadyState -> {
            if (destroyed || !visible || generation != pageGeneration || webView == null) {
                return;
            }
            if (!"\"complete\"".equals(encodedReadyState)) {
                webView.postDelayed(startupWatchdog, STARTUP_WATCHDOG_RETRY_MS);
                return;
            }
            /* Аварийный выход срабатывает только для уже готового документа.
               Основной путь по-прежнему ждёт role shell, insets и visual state. */
            pageLoaded = true;
            webView.setAlpha(1f);
            dismiss(generation);
        });
    }

    private void restartProbeIfReady() {
        if (!canProbe()) {
            return;
        }
        int generation = ++pageGeneration;
        resetNativeLayoutStability();
        View parent = webView.getParent() instanceof View ? (View) webView.getParent() : webView;
        ViewCompat.requestApplyInsets(parent);
        webView.requestLayout();
        webView.invalidate();
        webView.postOnAnimation(() -> webView.evaluateJavascript(PREPARE_LAYOUT, ignored -> {
            if (!isCurrentProbe(generation)) {
                return;
            }
            probeOnNextFrame(generation);
        }));
    }

    private void cancelPendingProbe() {
        pageGeneration += 1;
        resetNativeLayoutStability();
    }

    private boolean canProbe() {
        return !destroyed
            && visible
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

    private void probeOnNextFrame(int generation) {
        if (!isCurrentProbe(generation)) {
            return;
        }
        webView.postOnAnimation(() -> {
            if (!isCurrentProbe(generation)) {
                return;
            }
            boolean nativeLayoutReady = sampleNativeLayoutReadiness();
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
        });
    }

    private void awaitCommittedVisualState(int generation) {
        if (!isCurrentProbe(generation)) {
            return;
        }
        webView.postVisualStateCallback(generation, new WebView.VisualStateCallback() {
            @Override
            public void onComplete(long requestId) {
                if (requestId != generation || !isCurrentProbe(generation)) {
                    return;
                }
                webView.postOnAnimation(() -> webView.evaluateJavascript(READINESS_PROBE, encodedResult -> {
                    if (!isCurrentProbe(generation)) {
                        return;
                    }
                    Readiness readiness = parseReadiness(encodedResult);
                    if (!sampleNativeLayoutReadiness() || !readiness.isReady()) {
                        probeOnNextFrame(generation);
                        return;
                    }
                    webView.setAlpha(1f);
                    webView.postOnAnimation(() -> dismiss(generation));
                }));
            }
        });
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
                return Readiness.notReady();
            }
            String[] parts = value.split("\\|", -1);
            if (parts.length != 8) {
                return Readiness.notReady();
            }
            String[] dimensions = parts[1].split("x", -1);
            if (dimensions.length < 5) {
                return Readiness.notReady();
            }
            double viewportScale = Double.parseDouble(dimensions[2]);
            boolean hasViewport = Double.parseDouble(dimensions[0]) > 0
                && Double.parseDouble(dimensions[1]) > 0
                && viewportScale >= 0.98
                && viewportScale <= 1.02;
            return new Readiness(
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
            return Readiness.notReady();
        }
    }

    private void dismiss(int generation) {
        if (!isCurrentProbe(generation)) {
            return;
        }
        visible = false;
        spinner.stop();
        webView.removeCallbacks(startupWatchdog);
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
            })
            .start();
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
            boolean documentComplete,
            boolean hasViewport,
            int stableFrames,
            long quietWindowMs,
            boolean fontsReady,
            boolean imagesReady,
            boolean roleReady,
            boolean pageVisible) {
        static Readiness notReady() {
            return new Readiness(false, false, 0, 0L, false, false, false, false);
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
