package ru.copperresources.mobile;

import java.net.URI;
import java.net.URISyntaxException;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;

final class TabletUrlPolicy {
    static final String ORIGIN = "https://mining-master.driverform.ru";
    static final String CONTROL_PATH = "/mining-master/tablet/";
    static final String START_URL = ORIGIN + CONTROL_PATH;
    static final List<String> ENTRY_PATHS = Collections.unmodifiableList(Arrays.asList(
        CONTROL_PATH + "login/", CONTROL_PATH + "activate/"
    ));
    // Exact layout routes only. Export endpoints and arbitrary subpaths are not included.
    static final List<String> TABLET_PATHS = Collections.unmodifiableList(Arrays.asList(
        CONTROL_PATH,
        "/mining-master/tablet/dispatcher/reports/",
        "/mining-master/tablet/dispatcher/shift-log/",
        "/mining-master/tablet/dispatcher/management/",
        "/mining-master/tablet/dispatcher/mining-volumes/",
        "/mining-master/tablet/dispatcher/transport/",
        "/mining-master/tablet/dispatcher/downtimes/",
        "/mining-master/tablet/dispatcher/shift-reports/trucks/",
        "/mining-master/tablet/dispatcher/shift-reports/excavation/",
        "/mining-master/tablet/dispatcher/shift-reports/hourly/",
        "/mining-master/tablet/dispatcher/shift-reports/hourly-grid/",
        "/mining-master/tablet/reports/shift-analytics/",
        "/mining-master/tablet/reports/volume/",
        "/mining-master/tablet/reports/customer-daily/",
        "/mining-master/tablet/reports/templates/",
        "/mining-master/tablet/reports/downtimes/"
    ));

    static boolean trusted(String url) {
        try {
            URI uri = new URI(url == null ? "" : url);
            return "https".equalsIgnoreCase(uri.getScheme())
                && "mining-master.driverform.ru".equalsIgnoreCase(uri.getHost())
                && uri.getRawUserInfo() == null && (uri.getPort() == -1 || uri.getPort() == 443);
        } catch (URISyntaxException ignored) {
            return false;
        }
    }

    static boolean workplaceNavigation(String url) {
        if (!trusted(url)) return false;
        try {
            URI uri = new URI(url);
            String path = uri.getRawPath();
            return path != null && path.startsWith(CONTROL_PATH)
                && !path.contains("%") && !path.contains("\\")
                && uri.normalize().getRawPath().equals(path);
        } catch (URISyntaxException ignored) {
            return false;
        }
    }

    static boolean tabletPage(String url) {
        return matchesPath(url, TABLET_PATHS);
    }

    static boolean entryPage(String url) {
        return matchesPath(url, ENTRY_PATHS);
    }

    private static boolean matchesPath(String url, List<String> paths) {
        if (!trusted(url)) return false;
        try {
            return paths.contains(new URI(url).getRawPath());
        } catch (URISyntaxException ignored) {
            return false;
        }
    }

    static boolean controlPage(String url) {
        if (!trusted(url)) return false;
        try {
            return CONTROL_PATH.equals(new URI(url).getRawPath());
        } catch (URISyntaxException ignored) {
            return false;
        }
    }

    static String diagnosticRoute(String url) {
        if (!trusted(url)) return "other";
        try {
            String path = new URI(url).getRawPath();
            if (CONTROL_PATH.equals(path)) return "control";
            if ((CONTROL_PATH + "login/").equals(path)) return "login";
            if ((CONTROL_PATH + "activate/").equals(path)) return "activate";
        } catch (URISyntaxException ignored) {
            return "other";
        }
        return "other";
    }
}
