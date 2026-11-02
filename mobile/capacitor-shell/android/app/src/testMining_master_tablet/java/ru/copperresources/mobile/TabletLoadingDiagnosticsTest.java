package ru.copperresources.mobile;

import org.junit.Test;
import static org.junit.Assert.*;

public class TabletLoadingDiagnosticsTest {
    @Test public void pageKindNeverReturnsUrlContents() {
        assertEquals("missing", TabletLoadingDiagnostics.pageKind(null));
        assertEquals("blank", TabletLoadingDiagnostics.pageKind("about:blank"));
        assertEquals("browser_error", TabletLoadingDiagnostics.pageKind("chrome-error://chromewebdata/"));
        assertEquals("control", TabletLoadingDiagnostics.pageKind("https://mining-master.driverform.ru/mining-master/tablet/?token=secret"));
        assertEquals("login", TabletLoadingDiagnostics.pageKind("https://mining-master.driverform.ru/mining-master/tablet/login/?phone=secret"));
        assertEquals("activate", TabletLoadingDiagnostics.pageKind("https://mining-master.driverform.ru/mining-master/tablet/activate/#secret"));
        assertEquals("related", TabletLoadingDiagnostics.pageKind("https://mining-master.driverform.ru/mining-master/tablet/reports/secret/"));
        assertEquals("other", TabletLoadingDiagnostics.pageKind("https://name:secret@mining-master.driverform.ru/mining-master/tablet/login/"));
        assertEquals("other", TabletLoadingDiagnostics.pageKind("https://other.example/mining-master/tablet/login/"));
    }

    @Test public void documentFlagsAcceptOnlyFixedKindsAndBooleanBits() {
        assertEquals("document_route=blank body=1 contract=0 login_form=0 activate_form=0 board=0 browser_error=0 root=1 main=0 main_visible=0 body_visible=1 shared_login_form=0",
            TabletLoadingDiagnostics.documentFlags("blank|1|0|0|0|0|0|1|0|0|1|0"));
        assertEquals("probe=invalid", TabletLoadingDiagnostics.documentFlags(null));
        assertEquals("probe=invalid", TabletLoadingDiagnostics.documentFlags("secret|1|0|0|0|0|0|1|0|0|1|0"));
        assertEquals("probe=invalid", TabletLoadingDiagnostics.documentFlags("control|1|secret|0|0|0|0|1|0|0|1|0"));
        assertEquals("probe=invalid", TabletLoadingDiagnostics.documentFlags("control|1|0"));
    }
}
