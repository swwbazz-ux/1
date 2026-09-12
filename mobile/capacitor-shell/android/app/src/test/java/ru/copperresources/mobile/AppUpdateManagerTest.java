package ru.copperresources.mobile;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertSame;
import static org.junit.Assert.assertTrue;

import android.content.SharedPreferences;

import org.junit.Test;

import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;

public final class AppUpdateManagerTest {
    private static final String SHA256 =
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    @Test
    public void failedManifestReadKeepsLatestUpdate() {
        AppUpdateManager.UpdateInfo previous = update(44, "0.1.27");

        AppUpdateManager.UpdateInfo resolved = AppUpdateManager.resolveLatestUpdate(
            previous,
            false,
            null,
            43
        );

        assertSame(previous, resolved);
    }

    @Test
    public void successfulManifestWithoutNewerVersionClearsLatestUpdate() {
        AppUpdateManager.UpdateInfo previous = update(44, "0.1.27");

        AppUpdateManager.UpdateInfo resolved = AppUpdateManager.resolveLatestUpdate(
            previous,
            true,
            update(43, "0.1.26"),
            43
        );

        assertNull(resolved);
    }

    @Test
    public void cachedManifestRestoresFromSharedPreferencesOnlyWhenNewer() {
        InMemoryPreferences preferences = new InMemoryPreferences();
        AppUpdateManager.UpdateInfo expected = update(44, "0.1.27");
        AppUpdateManager.persistCachedUpdate(preferences, expected);

        AppUpdateManager.UpdateInfo restored = AppUpdateManager.restoreCachedUpdate(preferences, 43);

        assertEquals(expected.versionCode, restored.versionCode);
        assertEquals(expected.versionName, restored.versionName);
        assertEquals(expected.apkUrl, restored.apkUrl);
        assertEquals(expected.sha256, restored.sha256);
        assertEquals(expected.releaseNotes, restored.releaseNotes);

        assertNull(AppUpdateManager.restoreCachedUpdate(preferences, 44));
        assertFalse(preferences.contains("cached_version_code"));
    }

    @Test
    public void resumeChecksAndPeriodicRetriesAreThrottledByOutcome() {
        long completedAtMs = 1_000_000L;

        assertFalse(AppUpdateManager.shouldCheckOnResume(
            completedAtMs,
            true,
            completedAtMs + AppUpdateManager.SUCCESS_RESUME_MIN_INTERVAL_MS - 1L
        ));
        assertTrue(AppUpdateManager.shouldCheckOnResume(
            completedAtMs,
            true,
            completedAtMs + AppUpdateManager.SUCCESS_RESUME_MIN_INTERVAL_MS
        ));
        assertFalse(AppUpdateManager.shouldCheckOnResume(
            completedAtMs,
            false,
            completedAtMs + AppUpdateManager.FAILURE_RETRY_INTERVAL_MS - 1L
        ));
        assertTrue(AppUpdateManager.shouldCheckOnResume(
            completedAtMs,
            false,
            completedAtMs + AppUpdateManager.FAILURE_RETRY_INTERVAL_MS
        ));

        long regularIntervalMs = 15L * 60L * 1000L;
        assertEquals(
            regularIntervalMs - 2L * 60L * 1000L,
            AppUpdateManager.nextScheduledDelay(
                completedAtMs,
                true,
                completedAtMs + 2L * 60L * 1000L,
                regularIntervalMs
            )
        );
        assertEquals(
            30_000L,
            AppUpdateManager.nextScheduledDelay(
                completedAtMs,
                false,
                completedAtMs + 30_000L,
                regularIntervalMs
            )
        );
    }

    private static AppUpdateManager.UpdateInfo update(int versionCode, String versionName) {
        return new AppUpdateManager.UpdateInfo(
            versionCode,
            versionName,
            "https://driverform.ru/media/apk/test-" + versionName + ".apk",
            SHA256,
            "Проверка обновления"
        );
    }

    private static final class InMemoryPreferences implements SharedPreferences {
        private final Map<String, Object> values = new HashMap<>();

        @Override
        public Map<String, ?> getAll() {
            return new HashMap<>(values);
        }

        @Override
        public String getString(String key, String defaultValue) {
            Object value = values.get(key);
            return value instanceof String ? (String) value : defaultValue;
        }

        @Override
        @SuppressWarnings("unchecked")
        public Set<String> getStringSet(String key, Set<String> defaultValues) {
            Object value = values.get(key);
            return value instanceof Set ? new HashSet<>((Set<String>) value) : defaultValues;
        }

        @Override
        public int getInt(String key, int defaultValue) {
            Object value = values.get(key);
            return value instanceof Integer ? (Integer) value : defaultValue;
        }

        @Override
        public long getLong(String key, long defaultValue) {
            Object value = values.get(key);
            return value instanceof Long ? (Long) value : defaultValue;
        }

        @Override
        public float getFloat(String key, float defaultValue) {
            Object value = values.get(key);
            return value instanceof Float ? (Float) value : defaultValue;
        }

        @Override
        public boolean getBoolean(String key, boolean defaultValue) {
            Object value = values.get(key);
            return value instanceof Boolean ? (Boolean) value : defaultValue;
        }

        @Override
        public boolean contains(String key) {
            return values.containsKey(key);
        }

        @Override
        public Editor edit() {
            return new InMemoryEditor();
        }

        @Override
        public void registerOnSharedPreferenceChangeListener(OnSharedPreferenceChangeListener listener) {
        }

        @Override
        public void unregisterOnSharedPreferenceChangeListener(OnSharedPreferenceChangeListener listener) {
        }

        private final class InMemoryEditor implements Editor {
            private final Map<String, Object> changes = new HashMap<>();
            private final Set<String> removals = new HashSet<>();
            private boolean clear;

            @Override
            public Editor putString(String key, String value) {
                changes.put(key, value);
                return this;
            }

            @Override
            public Editor putStringSet(String key, Set<String> value) {
                changes.put(key, value == null ? null : new HashSet<>(value));
                return this;
            }

            @Override
            public Editor putInt(String key, int value) {
                changes.put(key, value);
                return this;
            }

            @Override
            public Editor putLong(String key, long value) {
                changes.put(key, value);
                return this;
            }

            @Override
            public Editor putFloat(String key, float value) {
                changes.put(key, value);
                return this;
            }

            @Override
            public Editor putBoolean(String key, boolean value) {
                changes.put(key, value);
                return this;
            }

            @Override
            public Editor remove(String key) {
                removals.add(key);
                return this;
            }

            @Override
            public Editor clear() {
                clear = true;
                return this;
            }

            @Override
            public boolean commit() {
                apply();
                return true;
            }

            @Override
            public void apply() {
                if (clear) {
                    values.clear();
                }
                for (String key : removals) {
                    values.remove(key);
                }
                values.putAll(changes);
            }
        }
    }
}
