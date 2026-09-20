package ru.copperresources.mobile;

final class NativeFieldProfile {
    private NativeFieldProfile() {}

    static boolean supportsPushAndHaptics() {
        String profileId = BuildConfig.APP_PROFILE_ID;
        return "driver".equals(profileId) || "excavator".equals(profileId);
    }
}
