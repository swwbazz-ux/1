package ru.copperresources.mobile;

import org.junit.Test;
import static org.junit.Assert.*;

public final class NativeTransportStateTest {
    @Test public void firstExceptionAfterLongSuspendIsWeak() {
        NativeTransportState state = new NativeTransportState(1_000L);
        state.failure(60_000L, "timeout");
        assertEquals("weak", state.transportState);
        assertEquals(1, state.failureCount);
    }

    @Test public void twoShortFailuresDoNotAnnounceLossAndThirdDoes() {
        NativeTransportState state = new NativeTransportState(1_000L);
        state.failure(2_000L, "timeout");
        state.failure(4_000L, "network_error");
        assertEquals("weak", state.transportState);
        state.failure(8_000L, "http_503");
        assertEquals("lost", state.transportState);
        assertEquals(3, state.failureCount);
    }

    @Test public void sustainedSilenceRequiresPriorWeakEvidence() {
        NativeTransportState state = new NativeTransportState(1_000L);
        state.failure(2_000L, "timeout");
        state.failure(16_999L, "timeout");
        assertEquals("weak", state.transportState);
        NativeTransportState prolonged = new NativeTransportState(1_000L);
        prolonged.failure(2_000L, "timeout");
        prolonged.failure(17_000L, "timeout");
        assertEquals("lost", prolonged.transportState);
    }

    @Test public void firstSuccessClearsLossAndCapturesServerVersion() {
        NativeTransportState state = new NativeTransportState(1_000L);
        for (int n = 1; n <= 3; n++) state.failure(n * 2_000L, "network_error");
        state.success(8_000L, 175L);
        assertEquals("ok", state.transportState);
        assertEquals("success", state.status);
        assertEquals(0, state.failureCount);
        assertEquals(8_000L, state.lastSuccessAtMs);
        assertEquals(175L, state.serverVersion);
        state.failure(9_000L, "timeout");
        assertEquals("weak", state.transportState);
    }

    @Test public void cookieAndAuthenticationFailuresDoNotClaimNetworkFailure() {
        NativeTransportState state = new NativeTransportState(1_000L);
        for (int n = 1; n <= 4; n++) state.localFailure(n * 10_000L, "cookie_timeout");
        assertEquals(0, state.failureCount);
        assertEquals("weak", state.transportState);
        assertEquals(1_000L, state.lastSuccessAtMs);
        state.localFailure(50_000L, "authentication_ended");
        assertEquals("auth_required", state.transportState);
        assertEquals(0, state.failureCount);
    }

    @Test public void malformedVersionCannotBeNormalizedIntoAnAuthenticatedHeartbeatSuccess() {
        assertTrue(NativeTransportState.isValidServerVersion(0L));
        assertTrue(NativeTransportState.isValidServerVersion(17D));
        assertTrue(NativeTransportState.isValidServerVersion(9_007_199_254_740_991L));
        assertFalse(NativeTransportState.isValidServerVersion(-1L));
        assertFalse(NativeTransportState.isValidServerVersion(1.5D));
        assertFalse(NativeTransportState.isValidServerVersion(Double.NaN));
        assertFalse(NativeTransportState.isValidServerVersion(Double.POSITIVE_INFINITY));
        assertFalse(NativeTransportState.isValidServerVersion(9_007_199_254_740_992L));
        assertFalse(NativeTransportState.isValidServerVersion(Long.MAX_VALUE));
        assertFalse(NativeTransportState.isValidServerVersion("17"));
        assertFalse(NativeTransportState.isValidServerVersion(null));
        assertFalse(NativeTransportState.isValidServerVersion(true));
    }

    @Test public void nativeFailureNotificationCannotContradictAHealthyForegroundWebView() {
        NativeTransportState state = new NativeTransportState(1_000L);
        state.failure(2_000L, "network_error");
        assertEquals("Фоновое подключение: повторяем…", state.failureStatusText(true));
        assertEquals("Переподключение…", state.failureStatusText(false));
        state.failure(4_000L, "network_error");
        state.failure(8_000L, "network_error");
        assertEquals("lost", state.transportState);
        assertEquals("Фоновое подключение: повторяем…", state.failureStatusText(true));
        assertEquals("Нет связи с сервером — повторяем подключение", state.failureStatusText(false));
        assertEquals("lost", state.transportState);
        assertEquals(3, state.failureCount);
    }

    @Test public void retriesProbeQuicklyBeforeBoundedOutageBackoff() {
        NativeTransportState state = new NativeTransportState(1_000L);
        long[] expected = {2_000L, 4_000L, 10_000L, 20_000L, 40_000L, 60_000L, 60_000L};
        for (int n = 0; n < expected.length; n++) {
            state.failure((n + 1) * 1_000L, "network_error");
            assertEquals(expected[n], state.retryDelay(10_000L, 60_000L));
        }
        state.success(90_000L, 1L);
        state.failure(91_000L, "network_error");
        assertEquals(2_000L, state.retryDelay(10_000L, 60_000L));
    }
}
