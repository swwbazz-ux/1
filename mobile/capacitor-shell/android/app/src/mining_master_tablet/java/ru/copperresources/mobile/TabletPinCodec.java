package ru.copperresources.mobile;

import java.security.GeneralSecurityException;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.Arrays;
import javax.crypto.SecretKeyFactory;
import javax.crypto.spec.PBEKeySpec;

/** No PIN is stored in the APK, JavaScript, preferences, logs, or intents. */
final class TabletPinCodec {
    private static final int ITERATIONS = 210_000;

    static boolean valid(String pin) {
        return pin != null && pin.matches("[0-9]{6,12}");
    }

    static String create(String pin) throws GeneralSecurityException {
        if (!valid(pin)) throw new IllegalArgumentException("PIN must contain 6 to 12 digits");
        byte[] salt = new byte[16];
        new SecureRandom().nextBytes(salt);
        return "1:" + hex(salt) + ":" + hex(derive(pin, salt));
    }

    static boolean matches(String pin, String encoded) {
        if (!valid(pin) || encoded == null) return false;
        String[] parts = encoded.split(":", -1);
        if (parts.length != 3 || !"1".equals(parts[0])
            || !parts[1].matches("[0-9a-f]{32}") || !parts[2].matches("[0-9a-f]{64}")) return false;
        try {
            return MessageDigest.isEqual(unhex(parts[2]), derive(pin, unhex(parts[1])));
        } catch (GeneralSecurityException exception) {
            return false;
        }
    }

    private static byte[] derive(String pin, byte[] salt) throws GeneralSecurityException {
        char[] value = pin.toCharArray();
        PBEKeySpec spec = new PBEKeySpec(value, salt, ITERATIONS, 256);
        Arrays.fill(value, '\0');
        try {
            // Available on the complete supported Android API 24+ range.
            return SecretKeyFactory.getInstance("PBKDF2WithHmacSHA1").generateSecret(spec).getEncoded();
        } finally {
            spec.clearPassword();
        }
    }

    private static String hex(byte[] bytes) {
        StringBuilder out = new StringBuilder(bytes.length * 2);
        for (byte value : bytes) out.append(String.format(java.util.Locale.ROOT, "%02x", value & 255));
        return out.toString();
    }

    private static byte[] unhex(String value) {
        byte[] out = new byte[value.length() / 2];
        for (int i = 0; i < out.length; i++) out[i] = (byte) Integer.parseInt(value.substring(i * 2, i * 2 + 2), 16);
        return out;
    }
}
