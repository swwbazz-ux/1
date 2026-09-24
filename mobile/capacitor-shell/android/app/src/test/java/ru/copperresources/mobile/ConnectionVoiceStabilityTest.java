package ru.copperresources.mobile;

import org.junit.Test;
import static org.junit.Assert.*;

public final class ConnectionVoiceStabilityTest {
    @Test public void fastFailuresNeverProduceConnectionSpeech() {
        ConnectionVoiceStability gate = new ConnectionVoiceStability();
        assertEquals(ConnectionVoiceStability.Action.NONE, gate.onFailure(0L, "weak"));
        assertEquals(ConnectionVoiceStability.Action.NONE, gate.onFailure(2_000L, "weak"));
        assertEquals(ConnectionVoiceStability.Action.NONE, gate.onFailure(6_000L, "lost"));
        assertEquals(ConnectionVoiceStability.Action.NONE, gate.onFailure(25_999L, "lost"));
    }

    @Test public void sustainedLossProducesOneReadyAction() {
        ConnectionVoiceStability gate = new ConnectionVoiceStability();
        assertEquals(ConnectionVoiceStability.Action.NONE, gate.onFailure(6_000L, "lost"));
        assertEquals(ConnectionVoiceStability.Action.LOSS_READY, gate.onFailure(36_000L, "lost"));
        gate.markLossHandled();
        assertEquals(ConnectionVoiceStability.Action.NONE, gate.onFailure(96_000L, "lost"));
    }

    @Test public void recoveryRequiresThreeSuccessesAcrossTwentySeconds() {
        ConnectionVoiceStability gate = new ConnectionVoiceStability();
        assertEquals(ConnectionVoiceStability.Action.NONE, gate.onSuccess(100_000L, true));
        assertEquals(ConnectionVoiceStability.Action.NONE, gate.onSuccess(110_000L, true));
        assertEquals(ConnectionVoiceStability.Action.RECOVERY_READY, gate.onSuccess(120_000L, true));
        gate.markRecoveryHandled();
        assertEquals(ConnectionVoiceStability.Action.NONE, gate.onSuccess(130_000L, false));
    }

    @Test public void renewedFailureResetsRecoveryEvidence() {
        ConnectionVoiceStability gate = new ConnectionVoiceStability();
        gate.onSuccess(100_000L, true);
        gate.onSuccess(110_000L, true);
        assertEquals(ConnectionVoiceStability.Action.NONE, gate.onFailure(115_000L, "weak"));
        assertEquals(ConnectionVoiceStability.Action.NONE, gate.onSuccess(120_000L, true));
        assertEquals(ConnectionVoiceStability.Action.NONE, gate.onSuccess(130_000L, true));
        assertEquals(ConnectionVoiceStability.Action.RECOVERY_READY, gate.onSuccess(140_000L, true));
    }
}
