package ru.copperresources.mobile;

import org.junit.Test;
import static org.junit.Assert.*;

public class TabletSecurityTest {
    @Test public void diagnosticsExposeOnlyFixedRouteKinds() {
        assertEquals("control", TabletUrlPolicy.diagnosticRoute(TabletUrlPolicy.START_URL));
        assertEquals("login", TabletUrlPolicy.diagnosticRoute(TabletUrlPolicy.START_URL + "login/?phone=70000000000&pin=123456"));
        assertEquals("activate", TabletUrlPolicy.diagnosticRoute(TabletUrlPolicy.START_URL + "activate/#secret"));
        assertEquals("other", TabletUrlPolicy.diagnosticRoute(TabletUrlPolicy.START_URL + "employee/123456/"));
        assertEquals("other", TabletUrlPolicy.diagnosticRoute("https://name:secret@mining-master.driverform.ru/mining-master/tablet/login/"));
        assertEquals("other", TabletUrlPolicy.diagnosticRoute("https://other.example/mining-master/tablet/login/"));
        assertEquals("other", TabletUrlPolicy.diagnosticRoute(null));
    }

    @Test public void workplaceCannotNavigateIntoLegacyLoginOrPersonalMasterRoute() {
        assertTrue(TabletUrlPolicy.workplaceNavigation(TabletUrlPolicy.START_URL));
        assertTrue(TabletUrlPolicy.workplaceNavigation(TabletUrlPolicy.START_URL + "login/"));
        assertTrue(TabletUrlPolicy.workplaceNavigation(TabletUrlPolicy.START_URL + "activate/"));
        assertFalse(TabletUrlPolicy.workplaceNavigation(TabletUrlPolicy.ORIGIN + "/login/"));
        assertFalse(TabletUrlPolicy.workplaceNavigation(TabletUrlPolicy.ORIGIN + "/mining-master/assignments/"));
        assertFalse(TabletUrlPolicy.workplaceNavigation(TabletUrlPolicy.START_URL + "../assignments/"));
        assertFalse(TabletUrlPolicy.workplaceNavigation(TabletUrlPolicy.START_URL + "%2e%2e/assignments/"));
    }
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
        assertTrue(TabletUrlPolicy.trusted("https://mining-master.driverform.ru/login/?next=%2Fdispatcher%2Fcontrol%2F"));
        assertTrue(TabletUrlPolicy.trusted("https://mining-master.driverform.ru/reports/shift/?export=excel"));
        assertTrue(TabletUrlPolicy.trusted("https://mining-master.driverform.ru/logout/"));
        assertFalse(TabletUrlPolicy.trusted("https://mining-master.driverform.ru.attacker.example/mining-master/tablet/"));
        assertFalse(TabletUrlPolicy.trusted("https://mining-master.driverform.ru@attacker.example/"));
        assertFalse(TabletUrlPolicy.trusted("https://name@mining-master.driverform.ru/"));
        assertFalse(TabletUrlPolicy.trusted("http://mining-master.driverform.ru/"));
        assertFalse(TabletUrlPolicy.trusted("https://mining-master.driverform.ru:8443/"));
        assertFalse(TabletUrlPolicy.trusted("intent://settings/#Intent;scheme=android;end"));
        assertFalse(TabletUrlPolicy.trusted("javascript:alert(1)"));
        assertFalse(TabletUrlPolicy.trusted(null));
    }

    @Test public void layoutScopeExcludesLoginOtherRolesAndEncodedPaths() {
        assertTrue(TabletUrlPolicy.tabletPage(TabletUrlPolicy.START_URL + "?shift=1#garage"));
        assertFalse(TabletUrlPolicy.tabletPage("https://driverform.ru/mining-master/tablet/"));
        assertFalse(TabletUrlPolicy.tabletPage("https://mining-master.driverform.ru/login/"));
        assertFalse(TabletUrlPolicy.tabletPage("https://mining-master.driverform.ru/mining-master/assignments/"));
        assertFalse(TabletUrlPolicy.tabletPage("https://mining-master.driverform.ru/dispatcher/%63ontrol/"));
        assertFalse(TabletUrlPolicy.tabletPage("https://mining-master.driverform.ru/mining-master/tablet/extra"));
    }

    @Test public void entryFormsUseTheirOwnExactRoutesOnTheTrustedHost() {
        String login = TabletUrlPolicy.START_URL + "login/?intent=start_shift";
        assertTrue(TabletUrlPolicy.entryPage(login));
        assertTrue(TabletUrlPolicy.entryPage(TabletUrlPolicy.START_URL + "activate/"));
        assertFalse(TabletUrlPolicy.tabletPage(login));
        assertFalse(TabletUrlPolicy.entryPage(TabletUrlPolicy.START_URL + "login/export/"));
        assertFalse(TabletUrlPolicy.entryPage(TabletUrlPolicy.START_URL + "%6cogin/"));
        assertFalse(TabletUrlPolicy.entryPage("https://driverform.ru/mining-master/tablet/login/"));
    }

    @Test public void relatedLayoutsAllowExactRoutesAndFiltersButNeverExportsOrSubpaths() {
        String[] relatedPaths = {
            "/mining-master/tablet/dispatcher/reports/", "/mining-master/tablet/dispatcher/shift-log/", "/mining-master/tablet/dispatcher/management/",
            "/mining-master/tablet/dispatcher/mining-volumes/", "/mining-master/tablet/dispatcher/transport/", "/mining-master/tablet/dispatcher/downtimes/",
            "/mining-master/tablet/dispatcher/shift-reports/trucks/", "/mining-master/tablet/dispatcher/shift-reports/excavation/",
            "/mining-master/tablet/dispatcher/shift-reports/hourly/", "/mining-master/tablet/dispatcher/shift-reports/hourly-grid/",
            "/mining-master/tablet/reports/shift-analytics/", "/mining-master/tablet/reports/volume/", "/mining-master/tablet/reports/customer-daily/", "/mining-master/tablet/reports/templates/", "/mining-master/tablet/reports/downtimes/"
        };
        for (String path : relatedPaths) {
            assertTrue(path, TabletUrlPolicy.tabletPage(TabletUrlPolicy.ORIGIN + path));
            assertTrue(path, TabletUrlPolicy.tabletPage(TabletUrlPolicy.ORIGIN + path + "?date=2026-10-30#table"));
            assertFalse(path, TabletUrlPolicy.tabletPage(TabletUrlPolicy.ORIGIN + path + "export/"));
            assertFalse(path, TabletUrlPolicy.tabletPage(TabletUrlPolicy.ORIGIN + path + "extra/"));
            assertFalse(path, TabletUrlPolicy.tabletPage(TabletUrlPolicy.ORIGIN + path.substring(0, path.length() - 1)));
            assertFalse(path, TabletUrlPolicy.tabletPage("https://driverform.ru" + path));
            assertFalse(path, TabletUrlPolicy.tabletPage("https://mining-master.driverform.ru.attacker.example" + path));
            assertFalse(path, TabletUrlPolicy.tabletPage("http://mining-master.driverform.ru" + path));
            assertFalse(path, TabletUrlPolicy.controlPage(TabletUrlPolicy.ORIGIN + path));
        }
        assertFalse(TabletUrlPolicy.tabletPage(TabletUrlPolicy.ORIGIN + "/mining-master/tablet/dispatcher/%72eports/"));
        assertFalse(TabletUrlPolicy.tabletPage(TabletUrlPolicy.ORIGIN + "/mining-master/tablet/dispatcher/shift-reports/"));
        assertFalse(TabletUrlPolicy.tabletPage(TabletUrlPolicy.ORIGIN + "/mining-master/tablet/dispatcher/logout/"));
        assertFalse(TabletUrlPolicy.tabletPage(TabletUrlPolicy.ORIGIN + "/apps/"));
        assertFalse(TabletUrlPolicy.tabletPage(TabletUrlPolicy.ORIGIN + "/reports/%76olume/"));
        assertFalse(TabletUrlPolicy.tabletPage(TabletUrlPolicy.ORIGIN + "/reports/rating/tv/"));
        assertFalse(TabletUrlPolicy.tabletPage(TabletUrlPolicy.ORIGIN + "/reports/other/"));
    }

    @Test public void onlyControlPageSuppressesBackNavigationToKeepHomeActivityOpen() {
        assertTrue(TabletUrlPolicy.controlPage(TabletUrlPolicy.START_URL));
        assertTrue(TabletUrlPolicy.controlPage(TabletUrlPolicy.START_URL + "?shift=1#garage"));
        assertFalse(TabletUrlPolicy.controlPage(TabletUrlPolicy.ORIGIN + "/mining-master/tablet/dispatcher/reports/"));
        assertFalse(TabletUrlPolicy.controlPage(TabletUrlPolicy.ORIGIN + "/mining-master/tablet/extra"));
        assertFalse(TabletUrlPolicy.controlPage("https://driverform.ru/mining-master/tablet/"));
        assertFalse(TabletUrlPolicy.controlPage(null));
    }
}
