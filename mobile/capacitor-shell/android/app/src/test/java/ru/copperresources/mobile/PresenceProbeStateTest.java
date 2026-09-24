package ru.copperresources.mobile;

import org.junit.Test;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public final class PresenceProbeStateTest {
    @Test
    public void responseClearsOnlyTheProbeActuallySentByThatRequest() {
        assertTrue(PresenceProbeState.shouldClear("probe-sent-123456", "probe-sent-123456"));
        assertFalse(PresenceProbeState.shouldClear("probe-newer-654321", "probe-sent-123456"));
        assertFalse(PresenceProbeState.shouldClear("probe-newer-654321", ""));
        assertFalse(PresenceProbeState.shouldClear("probe-newer-654321", null));
    }
}
