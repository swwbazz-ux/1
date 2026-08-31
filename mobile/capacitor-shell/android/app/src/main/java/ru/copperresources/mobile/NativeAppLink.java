package ru.copperresources.mobile;

import java.net.URI;
import java.net.URISyntaxException;
import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Pure URI validator for the one-time native handoff link.
 *
 * <p>The token deliberately remains in the URL fragment. Android delivers the
 * whole URI to the app, while the WebView's HTTPS request never sends the
 * fragment to the server. The handoff page removes it from browser history and
 * redeems it explicitly.</p>
 */
final class NativeAppLink {
    static final String ACTION_VIEW = "android.intent.action.VIEW";

    private static final Pattern TOKEN_FRAGMENT = Pattern.compile(
        "^token=([A-Za-z0-9_-]{43})$"
    );

    private NativeAppLink() {}

    static String resolve(
            String action,
            String rawUrl,
            String appServerUrl,
            String expectedHost,
            String expectedPath) {
        if (!ACTION_VIEW.equals(action)
                || isBlank(rawUrl)
                || isBlank(appServerUrl)
                || isBlank(expectedHost)
                || !isValidExpectedPath(expectedPath)) {
            return null;
        }

        final URI incoming;
        final URI server;
        try {
            incoming = new URI(rawUrl);
            server = new URI(appServerUrl);
        } catch (URISyntaxException ignored) {
            return null;
        }

        if (!isAllowedHttpsUri(incoming, expectedHost)
                || incoming.getRawQuery() != null
                || !expectedPath.equals(incoming.getRawPath())) {
            return null;
        }

        Matcher fragmentMatcher = TOKEN_FRAGMENT.matcher(
            incoming.getRawFragment() == null ? "" : incoming.getRawFragment()
        );
        if (!fragmentMatcher.matches()) {
            return null;
        }

        if (!isCanonicalServerOrigin(server, expectedHost)) {
            return null;
        }

        String token = fragmentMatcher.group(1);
        try {
            return new URI(
                "https",
                null,
                server.getHost().toLowerCase(Locale.ROOT),
                server.getPort(),
                expectedPath,
                null,
                "token=" + token
            ).toASCIIString();
        } catch (URISyntaxException ignored) {
            return null;
        }
    }

    private static boolean isAllowedHttpsUri(URI uri, String expectedHost) {
        return uri.isAbsolute()
            && !uri.isOpaque()
            && "https".equalsIgnoreCase(uri.getScheme())
            && uri.getHost() != null
            && expectedHost.equalsIgnoreCase(uri.getHost())
            && (uri.getPort() == -1 || uri.getPort() == 443)
            && uri.getRawUserInfo() == null;
    }

    private static boolean isCanonicalServerOrigin(URI server, String expectedHost) {
        String rawPath = server.getRawPath();
        return isAllowedHttpsUri(server, expectedHost)
            && server.getRawQuery() == null
            && server.getRawFragment() == null
            && (rawPath == null || rawPath.isEmpty() || "/".equals(rawPath))
            && server.normalize().equals(server);
    }

    private static boolean isValidExpectedPath(String path) {
        if (isBlank(path)
                || !path.startsWith("/")
                || !path.endsWith("/")
                || path.contains("//")
                || path.contains("/./")
                || path.contains("/../")
                || path.indexOf('?') >= 0
                || path.indexOf('#') >= 0
                || path.indexOf('%') >= 0) {
            return false;
        }
        try {
            URI pathUri = new URI(null, null, path, null);
            return path.equals(pathUri.getRawPath()) && pathUri.normalize().equals(pathUri);
        } catch (URISyntaxException ignored) {
            return false;
        }
    }

    private static boolean isBlank(String value) {
        return value == null || value.trim().isEmpty();
    }
}
