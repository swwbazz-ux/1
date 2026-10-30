package ru.copperresources.mobile;

import org.junit.Test;
import static org.junit.Assert.*;

public class TabletStandaloneReadinessTest {
    private static final String URL = "https://dispatcher.driverform.ru/reports/rating/tv/";

    @Test public void onlyTabletProfileMayRecognizeStandaloneRating() {
        assertFalse(TabletStandaloneReadiness.expressionFor("mining_master_tablet", URL).isEmpty());
        assertEquals("", TabletStandaloneReadiness.expressionFor("driver", URL));
        assertEquals("", TabletStandaloneReadiness.expressionFor("excavator", URL));
        assertEquals("", TabletStandaloneReadiness.expressionFor("dispatcher", URL));
        assertEquals("", TabletStandaloneReadiness.expressionFor(null, URL));
    }

    @Test public void onlyExactHttpsOriginAndRawPathAllowTheAlternateProbe() {
        String[] rejected = {
            "http://dispatcher.driverform.ru/reports/rating/tv/",
            "https://driverform.ru/reports/rating/tv/",
            "https://dispatcher.driverform.ru.attacker.example/reports/rating/tv/",
            "https://name@dispatcher.driverform.ru/reports/rating/tv/",
            "https://dispatcher.driverform.ru:8443/reports/rating/tv/",
            "https://dispatcher.driverform.ru/reports/rating/%74v/",
            "https://dispatcher.driverform.ru/reports/rating/tv",
            "https://dispatcher.driverform.ru/reports/rating/tv/error/",
            "https://dispatcher.driverform.ru/login/",
            "https://dispatcher.driverform.ru/dispatcher/control/",
            "https://dispatcher.driverform.ru/reports/rating/api/",
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
