package ru.copperresources.mobile;

import java.net.URI;
import java.net.URISyntaxException;

/** Pure request/lifecycle boundary; a web message never authorizes a kiosk policy change. */
final class TabletPrintRequestGate {
    static final String REQUEST = "requestPrint";

    static final class Request {
        private final long generation;
        private final String url;

        private Request(long generation, String url) {
            this.generation = generation;
            this.url = url;
        }
    }

    private long generation;
    private boolean loaded;
    private boolean failed;
    private Request pending;

    void pageStarted() { generation++; loaded = false; failed = false; }
    void pageFailed() { failed = true; loaded = false; }
    void pageFinished(String finishedUrl, String currentUrl) {
        loaded = !failed && TabletUrlPolicy.trusted(currentUrl) && currentUrl.equals(finishedUrl);
    }

    Request begin(String message, String origin, boolean mainFrame, String currentUrl) {
        if (pending != null || !loaded || !mainFrame || !REQUEST.equals(message)
            || !trustedOrigin(origin) || !TabletUrlPolicy.trusted(currentUrl)) return null;
        pending = new Request(generation, currentUrl);
        return pending;
    }

    boolean current(Request request, String currentUrl) {
        return pending == request && sameDocument(request, currentUrl);
    }

    // A print adapter remains valid during the expected Activity pause for Android's print UI.
    // It must still stop if the underlying WebView navigates, reloads, or fails.
    boolean sameDocument(Request request, String currentUrl) {
        return request != null && loaded && !failed
            && request.generation == generation && request.url.equals(currentUrl)
            && TabletUrlPolicy.trusted(currentUrl);
    }

    void finish(Request request) { if (pending == request) pending = null; }
    void cancelPending() { pending = null; }

    private static boolean trustedOrigin(String origin) {
        if (!TabletUrlPolicy.trusted(origin)) return false;
        try {
            URI parsed = new URI(origin);
            String path = parsed.getRawPath();
            return (path == null || path.isEmpty() || "/".equals(path))
                && parsed.getRawQuery() == null && parsed.getRawFragment() == null;
        } catch (URISyntaxException ignored) { return false; }
    }
}
