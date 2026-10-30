package ru.copperresources.mobile;

import java.net.URI;
import java.net.URISyntaxException;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;

final class TabletUrlPolicy {
    static final String ORIGIN = "https://dispatcher.driverform.ru";
    static final String CONTROL_PATH = "/dispatcher/control/";
    static final String START_URL = ORIGIN + CONTROL_PATH;
    // Exact layout routes only. Export endpoints and arbitrary subpaths are not included.
    static final List<String> TABLET_PATHS = Collections.unmodifiableList(Arrays.asList(
        CONTROL_PATH,
        "/dispatcher/reports/",
        "/dispatcher/shift-log/",
        "/dispatcher/management/",
        "/dispatcher/mining-volumes/",
        "/dispatcher/transport/",
        "/dispatcher/downtimes/",
        "/dispatcher/shift-reports/trucks/",
        "/dispatcher/shift-reports/excavation/",
        "/dispatcher/shift-reports/hourly/",
        "/dispatcher/shift-reports/hourly-grid/",
        "/reports/volume/",
        "/reports/customer-daily/",
        "/reports/templates/",
        "/reports/downtimes/"
    ));

    static boolean trusted(String url) {
        try {
            URI uri = new URI(url == null ? "" : url);
            return "https".equalsIgnoreCase(uri.getScheme())
                && "dispatcher.driverform.ru".equalsIgnoreCase(uri.getHost())
                && uri.getRawUserInfo() == null && (uri.getPort() == -1 || uri.getPort() == 443);
        } catch (URISyntaxException ignored) {
            return false;
        }
    }

    static boolean tabletPage(String url) {
        if (!trusted(url)) return false;
        try {
            return TABLET_PATHS.contains(new URI(url).getRawPath());
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
}
