package ru.copperresources.mobile;

import org.junit.Test;
import static org.junit.Assert.*;

public class TabletSecurityTest {
    @Test public void pinHashAuthenticatesOnlyMatchingPinAndUsesFreshSalt() throws Exception {
        String first = TabletPinCodec.create("907162");
        String second = TabletPinCodec.create("907162");
        assertNotEquals(first, second);
        assertTrue(first.matches("1:[0-9a-f]{32}:[0-9a-f]{64}"));
        assertTrue(TabletPinCodec.matches("907162", first));
        assertFalse(TabletPinCodec.matches("907163", first));
        assertFalse(TabletPinCodec.matches("", first));
        assertFalse(TabletPinCodec.matches("907162", "1:broken:hash"));
    }

    @Test public void navigationRejectsLookalikeOriginsCredentialsAndExternalSchemes() {
        assertTrue(TabletUrlPolicy.trusted(TabletUrlPolicy.START_URL));
        assertTrue(TabletUrlPolicy.trusted("https://dispatcher.driverform.ru/login/?next=%2Fdispatcher%2Fcontrol%2F"));
        assertTrue(TabletUrlPolicy.trusted("https://dispatcher.driverform.ru/reports/shift/?export=excel"));
        assertTrue(TabletUrlPolicy.trusted("https://dispatcher.driverform.ru/logout/"));
        assertFalse(TabletUrlPolicy.trusted("https://dispatcher.driverform.ru.attacker.example/dispatcher/control/"));
        assertFalse(TabletUrlPolicy.trusted("https://dispatcher.driverform.ru@attacker.example/"));
        assertFalse(TabletUrlPolicy.trusted("https://name@dispatcher.driverform.ru/"));
        assertFalse(TabletUrlPolicy.trusted("http://dispatcher.driverform.ru/"));
        assertFalse(TabletUrlPolicy.trusted("https://dispatcher.driverform.ru:8443/"));
        assertFalse(TabletUrlPolicy.trusted("intent://settings/#Intent;scheme=android;end"));
        assertFalse(TabletUrlPolicy.trusted("javascript:alert(1)"));
        assertFalse(TabletUrlPolicy.trusted(null));
    }

    @Test public void layoutScopeExcludesLoginOtherRolesAndEncodedPaths() {
        assertTrue(TabletUrlPolicy.tabletPage(TabletUrlPolicy.START_URL + "?shift=1#garage"));
        assertFalse(TabletUrlPolicy.tabletPage("https://driverform.ru/dispatcher/control/"));
        assertFalse(TabletUrlPolicy.tabletPage("https://dispatcher.driverform.ru/login/"));
        assertFalse(TabletUrlPolicy.tabletPage("https://dispatcher.driverform.ru/mining-master/assignments/"));
        assertFalse(TabletUrlPolicy.tabletPage("https://dispatcher.driverform.ru/dispatcher/%63ontrol/"));
        assertFalse(TabletUrlPolicy.tabletPage("https://dispatcher.driverform.ru/dispatcher/control/extra"));
    }

    @Test public void relatedLayoutsAllowExactRoutesAndFiltersButNeverExportsOrSubpaths() {
        String[] relatedPaths = {
            "/dispatcher/reports/", "/dispatcher/shift-log/", "/dispatcher/management/",
            "/dispatcher/mining-volumes/", "/dispatcher/transport/", "/dispatcher/downtimes/",
            "/dispatcher/shift-reports/trucks/", "/dispatcher/shift-reports/excavation/",
            "/dispatcher/shift-reports/hourly/", "/dispatcher/shift-reports/hourly-grid/",
            "/reports/volume/", "/reports/customer-daily/", "/reports/templates/", "/reports/downtimes/"
        };
        for (String path : relatedPaths) {
            assertTrue(path, TabletUrlPolicy.tabletPage(TabletUrlPolicy.ORIGIN + path));
            assertTrue(path, TabletUrlPolicy.tabletPage(TabletUrlPolicy.ORIGIN + path + "?date=2026-10-30#table"));
            assertFalse(path, TabletUrlPolicy.tabletPage(TabletUrlPolicy.ORIGIN + path + "export/"));
            assertFalse(path, TabletUrlPolicy.tabletPage(TabletUrlPolicy.ORIGIN + path + "extra/"));
            assertFalse(path, TabletUrlPolicy.tabletPage(TabletUrlPolicy.ORIGIN + path.substring(0, path.length() - 1)));
            assertFalse(path, TabletUrlPolicy.tabletPage("https://driverform.ru" + path));
            assertFalse(path, TabletUrlPolicy.tabletPage("https://dispatcher.driverform.ru.attacker.example" + path));
            assertFalse(path, TabletUrlPolicy.tabletPage("http://dispatcher.driverform.ru" + path));
            assertFalse(path, TabletUrlPolicy.controlPage(TabletUrlPolicy.ORIGIN + path));
        }
        assertFalse(TabletUrlPolicy.tabletPage(TabletUrlPolicy.ORIGIN + "/dispatcher/%72eports/"));
        assertFalse(TabletUrlPolicy.tabletPage(TabletUrlPolicy.ORIGIN + "/dispatcher/shift-reports/"));
        assertFalse(TabletUrlPolicy.tabletPage(TabletUrlPolicy.ORIGIN + "/dispatcher/logout/"));
        assertFalse(TabletUrlPolicy.tabletPage(TabletUrlPolicy.ORIGIN + "/apps/"));
        assertFalse(TabletUrlPolicy.tabletPage(TabletUrlPolicy.ORIGIN + "/reports/%76olume/"));
        assertFalse(TabletUrlPolicy.tabletPage(TabletUrlPolicy.ORIGIN + "/reports/rating/tv/"));
        assertFalse(TabletUrlPolicy.tabletPage(TabletUrlPolicy.ORIGIN + "/reports/other/"));
    }

    @Test public void onlyControlPageSuppressesBackNavigationToKeepHomeActivityOpen() {
        assertTrue(TabletUrlPolicy.controlPage(TabletUrlPolicy.START_URL));
        assertTrue(TabletUrlPolicy.controlPage(TabletUrlPolicy.START_URL + "?shift=1#garage"));
        assertFalse(TabletUrlPolicy.controlPage(TabletUrlPolicy.ORIGIN + "/dispatcher/reports/"));
        assertFalse(TabletUrlPolicy.controlPage(TabletUrlPolicy.ORIGIN + "/dispatcher/control/extra"));
        assertFalse(TabletUrlPolicy.controlPage("https://driverform.ru/dispatcher/control/"));
        assertFalse(TabletUrlPolicy.controlPage(null));
    }
}
