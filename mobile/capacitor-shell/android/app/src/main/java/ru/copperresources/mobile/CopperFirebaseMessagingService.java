package ru.copperresources.mobile;

import androidx.annotation.NonNull;

import com.google.firebase.messaging.FirebaseMessagingService;
import com.google.firebase.messaging.RemoteMessage;

public class CopperFirebaseMessagingService extends FirebaseMessagingService {
    @Override
    public void onNewToken(@NonNull String token) {
        super.onNewToken(token);
        NativePushPlugin.publishToken(this, token);
    }

    @Override
    public void onMessageReceived(@NonNull RemoteMessage message) {
        super.onMessageReceived(message);
        if (!NativeFieldProfile.supportsPushAndHaptics()) {
            return;
        }
        ConnectivityForegroundService.reconcileFromForeground(this);
    }
}
