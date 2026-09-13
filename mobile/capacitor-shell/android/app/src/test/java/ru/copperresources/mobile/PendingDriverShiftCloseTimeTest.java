package ru.copperresources.mobile;

import static org.junit.Assert.assertEquals;

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
}
