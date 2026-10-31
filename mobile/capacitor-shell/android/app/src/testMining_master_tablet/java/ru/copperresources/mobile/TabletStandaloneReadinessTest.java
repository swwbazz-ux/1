package ru.copperresources.mobile;

import org.junit.Test;
import static org.junit.Assert.*;

public class TabletStandaloneReadinessTest {
    private static final String URL = "https://mining-master.driverform.ru/reports/rating/tv/";

    @Test public void onlyTabletProfileMayRecognizeStandaloneRating() {
        assertFalse(TabletStandaloneReadiness.expressionFor("mining_master_tablet", URL).isEmpty());
        assertEquals("", TabletStandaloneReadiness.expressionFor("driver", URL));
        assertEquals("", TabletStandaloneReadiness.expressionFor("excavator", URL));
        assertEquals("", TabletStandaloneReadiness.expressionFor("dispatcher", URL));
        assertEquals("", TabletStandaloneReadiness.expressionFor(null, URL));
    }

    @Test public void onlyExactHttpsOriginAndRawPathAllowTheAlternateProbe() {
        String[] rejected = {
            "http://mining-master.driverform.ru/reports/rating/tv/",
            "https://driverform.ru/reports/rating/tv/",
            "https://mining-master.driverform.ru.attacker.example/reports/rating/tv/",
            "https://name@mining-master.driverform.ru/reports/rating/tv/",
            "https://mining-master.driverform.ru:8443/reports/rating/tv/",
            "https://mining-master.driverform.ru/reports/rating/%74v/",
            "https://mining-master.driverform.ru/reports/rating/tv",
            "https://mining-master.driverform.ru/reports/rating/tv/error/",
            "https://mining-master.driverform.ru/login/",
            "https://mining-master.driverform.ru/mining-master/tablet/",
            "https://mining-master.driverform.ru/reports/rating/api/",
            null
        };
        for (String url : rejected) assertEquals(String.valueOf(url), "",
            TabletStandaloneReadiness.expressionFor("mining_master_tablet", url));
    }

    @Test public void ratingFiltersDoNotChangeTheNativeScope() {
        assertEquals(TabletStandaloneReadiness.expressionFor("mining_master_tablet", URL),
            TabletStandaloneReadiness.expressionFor("mining_master_tablet", URL + "?layout=three#rating"));
    }
}
