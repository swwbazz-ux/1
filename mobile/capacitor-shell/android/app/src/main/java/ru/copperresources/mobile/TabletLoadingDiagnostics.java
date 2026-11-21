package ru.copperresources.mobile;

import java.net.URI;
import java.net.URISyntaxException;

/** Fixed diagnostic enums and booleans only, never document text or URL contents. */
final class TabletLoadingDiagnostics {
    static String pageKind(String url) {
        if (url == null || url.isEmpty()) return "missing";
        try {
            URI uri = new URI(url);
            if ("about".equals(uri.getScheme()) && "blank".equals(uri.getSchemeSpecificPart())) return "blank";
            if ("chrome-error".equals(uri.getScheme())) return "browser_error";
            if (!"https".equalsIgnoreCase(uri.getScheme())
                    || !"mining-master.driverform.ru".equalsIgnoreCase(uri.getHost())
                    || uri.getRawUserInfo() != null || (uri.getPort() != -1 && uri.getPort() != 443)) return "other";
            String path = uri.getRawPath();
            if ("/mining-master/tablet/".equals(path)) return "control";
            if ("/mining-master/tablet/login/".equals(path)) return "login";
            if ("/mining-master/tablet/activate/".equals(path)) return "activate";
            if (path != null && path.startsWith("/mining-master/tablet/")) return "related";
        } catch (URISyntaxException ignored) {
            return "other";
        }
        return "other";
    }

    static String documentFlags(String encodedFlags) {
        if (encodedFlags == null) return "probe=invalid";
        String[] flags = encodedFlags.split("\\|", -1);
        if (flags.length != 12 || !flags[0].matches("missing|blank|browser_error|control|login|activate|related|other")) {
            return "probe=invalid";
        }
        for (int index = 1; index < flags.length; index++) {
            if (!"0".equals(flags[index]) && !"1".equals(flags[index])) return "probe=invalid";
        }
        return "document_route=" + flags[0] + " body=" + flags[1] + " contract=" + flags[2]
            + " login_form=" + flags[3] + " activate_form=" + flags[4] + " board=" + flags[5]
            + " browser_error=" + flags[6] + " root=" + flags[7] + " main=" + flags[8]
            + " main_visible=" + flags[9] + " body_visible=" + flags[10] + " shared_login_form=" + flags[11];
    }
}
