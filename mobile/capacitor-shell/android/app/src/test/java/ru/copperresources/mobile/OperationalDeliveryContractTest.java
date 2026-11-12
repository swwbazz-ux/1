package ru.copperresources.mobile;

import org.junit.Test;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public final class OperationalDeliveryContractTest {
    @Test public void authoritativeResetVersionAcceptsExactZeroAndPositiveIntegers() {
        assertTrue(BackgroundConnectionPlugin.isNonNegativeInteger(0));
        assertTrue(BackgroundConnectionPlugin.isNonNegativeInteger(0L));
        assertTrue(BackgroundConnectionPlugin.isNonNegativeInteger(7D));
        assertTrue(BackgroundConnectionPlugin.isNonNegativeInteger("12"));
    }

    @Test public void authoritativeResetVersionRejectsMissingNegativeFractionalAndOverflowValues() {
        assertFalse(BackgroundConnectionPlugin.isNonNegativeInteger(null));
        assertFalse(BackgroundConnectionPlugin.isNonNegativeInteger(-1));
        assertFalse(BackgroundConnectionPlugin.isNonNegativeInteger(1.5D));
        assertFalse(BackgroundConnectionPlugin.isNonNegativeInteger(Double.NaN));
        assertFalse(BackgroundConnectionPlugin.isNonNegativeInteger(Double.POSITIVE_INFINITY));
        assertFalse(BackgroundConnectionPlugin.isNonNegativeInteger(""));
        assertFalse(BackgroundConnectionPlugin.isNonNegativeInteger("not-a-version"));
        assertFalse(BackgroundConnectionPlugin.isNonNegativeInteger("9223372036854775808"));
    }
}
