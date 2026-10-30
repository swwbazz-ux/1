package ru.copperresources.mobile;

import org.junit.Test;
import static org.junit.Assert.*;

public class TabletPrintRequestGateTest {
    private static final String ORIGIN = "https://dispatcher.driverform.ru";
    private static final String REPORT = ORIGIN + "/reports/trucks/?shift=night";

    private TabletPrintRequestGate loaded() {
        TabletPrintRequestGate gate = new TabletPrintRequestGate();
        gate.pageStarted();
        gate.pageFinished(REPORT, REPORT);
        return gate;
    }

    @Test public void acceptsOnlyExactMessageFromTrustedMainFrameAfterPageLoad() {
        TabletPrintRequestGate gate = loaded();
        assertNull(gate.begin("requestPrint", ORIGIN, false, REPORT));
        assertNull(gate.begin("pauseKiosk", ORIGIN, true, REPORT));
        assertNull(gate.begin("requestPrint:" + REPORT, ORIGIN, true, REPORT));
        assertNull(gate.begin(null, ORIGIN, true, REPORT));
        assertNull(gate.begin("requestPrint", "https://dispatcher.driverform.ru.attacker.example", true, REPORT));
        assertNull(gate.begin("requestPrint", "http://dispatcher.driverform.ru", true, REPORT));
        assertNull(gate.begin("requestPrint", "https://name@dispatcher.driverform.ru", true, REPORT));
        assertNull(gate.begin("requestPrint", ORIGIN + ":8443", true, REPORT));
        assertNull(gate.begin("requestPrint", ORIGIN + "/reports/", true, REPORT));
        assertNull(gate.begin("requestPrint", ORIGIN + "?command=print", true, REPORT));
        assertNull(gate.begin("requestPrint", "null", true, REPORT));
        assertNull(gate.begin("requestPrint", ORIGIN, true, "https://other.example/report/"));
        TabletPrintRequestGate.Request request = gate.begin("requestPrint", ORIGIN, true, REPORT);
        assertNotNull(request);
        assertTrue(gate.current(request, REPORT));
    }

    @Test public void duplicateMessagesNeverQueueAnotherPinDialog() {
        TabletPrintRequestGate gate = loaded();
        TabletPrintRequestGate.Request first = gate.begin("requestPrint", ORIGIN, true, REPORT);
        assertNotNull(first);
        assertNull(gate.begin("requestPrint", ORIGIN, true, REPORT));
        gate.finish(first);
        assertFalse(gate.current(first, REPORT));
        assertNotNull(gate.begin("requestPrint", ORIGIN, true, REPORT));
    }

    @Test public void reloadingSameUrlDuringPinInvalidatesTheRequest() {
        TabletPrintRequestGate gate = loaded();
        TabletPrintRequestGate.Request old = gate.begin("requestPrint", ORIGIN, true, REPORT);
        gate.pageStarted();
        gate.pageFinished(REPORT, REPORT);
        assertFalse(gate.current(old, REPORT));
        assertNull(gate.begin("requestPrint", ORIGIN, true, REPORT));
        gate.finish(old);
        assertNotNull(gate.begin("requestPrint", ORIGIN, true, REPORT));
    }

    @Test public void changedUrlOrLifecycleCancellationCannotPrintAnotherPage() {
        TabletPrintRequestGate gate = loaded();
        TabletPrintRequestGate.Request old = gate.begin("requestPrint", ORIGIN, true, REPORT);
        assertFalse(gate.current(old, ORIGIN + "/reports/trucks/?shift=day"));
        gate.cancelPending();
        assertFalse(gate.current(old, REPORT));
        TabletPrintRequestGate.Request fresh = gate.begin("requestPrint", ORIGIN, true, REPORT);
        assertNotNull(fresh);
        gate.finish(old);
        assertTrue(gate.current(fresh, REPORT));
    }

    @Test public void unfinishedFailedOrMismatchedNavigationCannotRequestPrint() {
        TabletPrintRequestGate gate = new TabletPrintRequestGate();
        gate.pageStarted();
        assertNull(gate.begin("requestPrint", ORIGIN, true, REPORT));
        gate.pageFinished(ORIGIN + "/login/", REPORT);
        assertNull(gate.begin("requestPrint", ORIGIN, true, REPORT));
        gate.pageFailed();
        gate.pageFinished(REPORT, REPORT);
        assertNull(gate.begin("requestPrint", ORIGIN, true, REPORT));
        gate.pageStarted();
        gate.pageFinished(REPORT, REPORT);
        assertNotNull(gate.begin("requestPrint", ORIGIN + ":443", true, REPORT));
    }

    @Test public void activeAdapterSurvivesSystemPrintPauseButNeverNavigationOrReload() {
        TabletPrintRequestGate gate = loaded();
        TabletPrintRequestGate.Request request = gate.begin("requestPrint", ORIGIN, true, REPORT);
        gate.cancelPending(); // Expected Activity pause when Android opens its print UI.
        assertFalse(gate.current(request, REPORT));
        assertTrue(gate.sameDocument(request, REPORT));
        assertFalse(gate.sameDocument(request, ORIGIN + "/reports/trucks/?shift=day"));
        gate.pageFailed();
        assertFalse(gate.sameDocument(request, REPORT));
        gate.pageStarted();
        gate.pageFinished(REPORT, REPORT);
        assertFalse(gate.sameDocument(request, REPORT));
    }
}
