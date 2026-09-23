package ru.copperresources.mobile;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Handler;
import android.os.Looper;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.google.firebase.FirebaseApp;
import com.google.firebase.messaging.FirebaseMessaging;

import java.lang.ref.WeakReference;

@CapacitorPlugin(name = "NativePush")
public class NativePushPlugin extends Plugin {
    private static final String PREFS_NAME = "native_push";
    private static final String FCM_TOKEN = "fcm_token";
    private static volatile WeakReference<NativePushPlugin> activePlugin = new WeakReference<>(null);

    @Override
    public void load() {
        activePlugin = new WeakReference<>(this);
        String token = storedToken(getContext());
        if (!token.isEmpty()) {
            notifyListeners("pushToken", tokenPayload(token), true);
        }
    }

    @Override
    protected void handleOnDestroy() {
        NativePushPlugin plugin = activePlugin.get();
        if (plugin == this) {
            activePlugin.clear();
        }
        super.handleOnDestroy();
    }

    @PluginMethod
    public void getToken(PluginCall call) {
        if (!NativeFieldProfile.supportsPushAndHaptics()) {
            call.resolve(unavailablePayload());
            return;
        }
        String token = storedToken(getContext());
        if (!token.isEmpty()) {
            call.resolve(tokenPayload(token));
            return;
        }
<<<<<<< HEAD
        if (FirebaseApp.getApps(getContext()).isEmpty()) {
            call.reject("FCM is not configured for this application build");
            return;
        }
        FirebaseMessaging.getInstance().getToken().addOnCompleteListener(task -> {
            if (!task.isSuccessful() || task.getResult() == null || task.getResult().trim().isEmpty()) {
                call.reject("FCM token is unavailable");
                return;
            }
            String freshToken = task.getResult().trim();
            storeToken(getContext(), freshToken);
            call.resolve(tokenPayload(freshToken));
        });
=======
        /* Builds without google-services.json (QA and any profile that never ships a
           Firebase config) leave the default FirebaseApp uninitialized. Touching
           FirebaseMessaging there throws on the plugin thread and kills the process,
           so the absent delivery channel is reported as an ordinary empty envelope. */
        if (!firebaseReady()) {
            call.resolve(unavailablePayload());
            return;
        }
        try {
            FirebaseMessaging.getInstance().getToken().addOnCompleteListener(task -> {
                if (!task.isSuccessful() || task.getResult() == null || task.getResult().trim().isEmpty()) {
                    call.resolve(unavailablePayload());
                    return;
                }
                String freshToken = task.getResult().trim();
                storeToken(getContext(), freshToken);
                call.resolve(tokenPayload(freshToken));
            });
        } catch (Throwable error) {
            call.resolve(unavailablePayload());
        }
    }

    private boolean firebaseReady() {
        try {
            return !FirebaseApp.getApps(getContext()).isEmpty();
        } catch (Throwable error) {
            return false;
        }
>>>>>>> 99f3c183 (fix(driver): keep QA push safe without Firebase and stop halving text)
    }

    static void publishToken(Context context, String token) {
        if (!NativeFieldProfile.supportsPushAndHaptics() || token == null || token.trim().isEmpty()) {
            return;
        }
        String normalizedToken = token.trim();
        storeToken(context, normalizedToken);
        new Handler(Looper.getMainLooper()).post(() -> {
            NativePushPlugin plugin = activePlugin.get();
            if (plugin != null) {
                plugin.notifyListeners("pushToken", tokenPayload(normalizedToken), true);
            }
        });
    }

    private static void storeToken(Context context, String token) {
        preferences(context).edit().putString(FCM_TOKEN, token).commit();
    }

    private static String storedToken(Context context) {
        String token = preferences(context).getString(FCM_TOKEN, "");
        return token == null ? "" : token.trim();
    }

    private static SharedPreferences preferences(Context context) {
        return context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
    }

    private static JSObject tokenPayload(String token) {
        return new JSObject()
            .put("provider", "fcm")
            .put("token", token)
            .put("platform", "android")
            .put("appId", BuildConfig.APPLICATION_ID)
            .put("available", true);
    }

    private static JSObject unavailablePayload() {
        return new JSObject()
            .put("provider", "fcm")
            .put("token", "")
            .put("platform", "android")
            .put("appId", BuildConfig.APPLICATION_ID)
            .put("available", false);
    }
}
