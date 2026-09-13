package ru.copperresources.mobile;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public final class PendingDriverShiftCloseTimeTest {
    @Test
    public void formatsPersistedCreatedAtAsUtcOccurredAt() {
        assertEquals(
            "2026-09-13T04:05:06.789Z",
            PendingDriverShiftClose.occurredAtIso(1789272306789L)
        );
    }

    @Test
    public void capacitorCreatedAtAcceptsEveryJsonNumberRepresentation() {
        assertEquals(1789272306789L, BackgroundConnectionPlugin.numericLong(1789272306789L));
        assertEquals(1789272306789L, BackgroundConnectionPlugin.numericLong(1789272306789D));
        assertEquals(1789272306789L, BackgroundConnectionPlugin.numericLong("1789272306789"));
        assertEquals(0L, BackgroundConnectionPlugin.numericLong("invalid"));
    }

    @Test
    public void retryableHttpStatusesUseBoundedBackoff() {
        assertTrue(PendingDriverShiftClose.isRetryableHttpStatus(408));
        assertTrue(PendingDriverShiftClose.isRetryableHttpStatus(429));
        assertTrue(PendingDriverShiftClose.isRetryableHttpStatus(503));
        assertFalse(PendingDriverShiftClose.isRetryableHttpStatus(409));
        assertFalse(PendingDriverShiftClose.isRetryableHttpStatus(422));
        assertEquals(2_000L, PendingDriverShiftClose.retryDelayMs(1));
        assertEquals(60_000L, PendingDriverShiftClose.retryDelayMs(99));
    }

    @Test
    public void authenticationOnlyResumesForANewAuthenticatedGeneration() {
        assertFalse(PendingDriverShiftClose.hasFreshAuthentication("auth-1", ""));
        assertFalse(PendingDriverShiftClose.hasFreshAuthentication("auth-1", "auth-1"));
        assertTrue(PendingDriverShiftClose.hasFreshAuthentication("auth-1", "auth-2"));
    }

}
