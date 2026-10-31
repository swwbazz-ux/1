package ru.copperresources.mobile;

import android.Manifest;
import android.app.ActivityManager;
import android.app.Service;
import android.app.admin.DevicePolicyManager;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Binder;
import android.os.Build;
import android.os.IBinder;
import android.os.Parcel;
import android.os.Process;
import android.os.RemoteException;
import android.os.SystemClock;
import android.provider.Settings;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/** Owns the OEM setup gate and its journal in a small, private process without a WebView. */
public final class TabletEdgeGuardService extends Service {
    static final String DESCRIPTOR = "ru.copperresources.mobile.TabletEdgeGuard";
    static final int ARM = IBinder.FIRST_CALL_TRANSACTION;
    static final int HEARTBEAT = ARM + 1;
    static final int RELEASE = ARM + 2;
    private static final String SETUP_COMPLETE = "user_setup_complete";
    private static final Object SERIAL = new Object();
    private TabletEdgeGuardState state;
    private IBinder owner;
    private IBinder.DeathRecipient ownerDeath;
    private ScheduledExecutorService watchdog;

    private final Binder endpoint = new Binder() {
        @Override protected boolean onTransact(int code, Parcel data, Parcel reply, int flags) throws RemoteException {
            if (code == INTERFACE_TRANSACTION) { reply.writeString(DESCRIPTOR); return true; }
            if (Binder.getCallingUid() != Process.myUid()) throw new SecurityException("Private edge guard");
            data.enforceInterface(DESCRIPTOR);
            if (code != ARM && code != HEARTBEAT && code != RELEASE) return super.onTransact(code, data, reply, flags);
            IBinder client = data.readStrongBinder();
            boolean result;
            synchronized (SERIAL) {
                boolean wasActive = state.active();
                if (code == ARM) result = arm(client);
                else if (code == HEARTBEAT) result = client != null && client.equals(owner) && state.heartbeat();
                else {
                    // A restore request is always safe from another instance of our own application.
                    detachOwner();
                    result = state.release();
                }
                if (code == RELEASE || wasActive != state.active()) diagnostic(
                    code == ARM ? "arm" : code == RELEASE ? "release" : "heartbeat", result);
            }
            reply.writeNoException();
            reply.writeInt(result ? 1 : 0);
            return true;
        }
    };

    @Override public void onCreate() {
        super.onCreate();
        synchronized (SERIAL) {
            state = createState(this);
            diagnostic("service_start_recovery", state.recover());
        }
        watchdog = Executors.newSingleThreadScheduledExecutor(runnable -> {
            Thread thread = new Thread(runnable, "tablet-edge-lease");
            thread.setDaemon(true);
            return thread;
        });
        watchdog.scheduleWithFixedDelay(() -> {
            synchronized (SERIAL) {
                boolean wasActive = state.active();
                state.checkLease();
                if (wasActive && !state.active()) diagnostic("lease_recovery", state.release());
            }
        }, 1, 1, TimeUnit.SECONDS);
    }

    @Override public IBinder onBind(Intent intent) {
        synchronized (SERIAL) { detachOwner(); diagnostic("bind_recovery", state.recover()); }
        return endpoint;
    }

    @Override public boolean onUnbind(Intent intent) {
        synchronized (SERIAL) { detachOwner(); diagnostic("unbind_recovery", state.release()); }
        return false;
    }

    @Override public void onDestroy() {
        if (watchdog != null) watchdog.shutdownNow();
        synchronized (SERIAL) { detachOwner(); if (state != null) diagnostic("destroy_recovery", state.release()); }
        super.onDestroy();
    }

    private boolean arm(IBinder client) {
        if (client == null || !client.isBinderAlive()) return false;
        if (!client.equals(owner)) {
            detachOwner();
            if (!state.release()) return false;
            try {
                IBinder.DeathRecipient death = () -> {
                    synchronized (SERIAL) {
                        if (client.equals(owner)) { detachOwner(); diagnostic("client_death_recovery", state.release()); }
                    }
                };
                client.linkToDeath(death, 0);
                owner = client;
                ownerDeath = death;
            } catch (RemoteException failure) { return false; }
        }
        return state.arm();
    }

    private void detachOwner() {
        if (owner != null && ownerDeath != null) owner.unlinkToDeath(ownerDeath, 0);
        owner = null;
        ownerDeath = null;
    }

    static boolean supportedFirmware() {
        return Build.VERSION.SDK_INT == 30 && "Tab11_NEU".equals(Build.DEVICE)
            && "Tab11_T1_251101".equals(Build.DISPLAY);
    }

    static boolean mayArm(Context context) {
        if (!supportedFirmware() || context.checkSelfPermission(Manifest.permission.WRITE_SECURE_SETTINGS)
                != PackageManager.PERMISSION_GRANTED) return false;
        DevicePolicyManager policy = context.getSystemService(DevicePolicyManager.class);
        ActivityManager tasks = context.getSystemService(ActivityManager.class);
        return policy != null && policy.isDeviceOwnerApp(context.getPackageName())
            && policy.isLockTaskPermitted(context.getPackageName())
            && tasks != null && tasks.getLockTaskModeState() == ActivityManager.LOCK_TASK_MODE_LOCKED
            && Settings.Global.getInt(context.getContentResolver(), Settings.Global.DEVICE_PROVISIONED, 0) == 1;
    }

    /** Restore-only fallback when the service process dies. Never reads/writes its preferences. */
    static boolean restoreFromClient(Context context, boolean mayOwnChange) {
        try {
            if (Settings.Secure.getInt(context.getContentResolver(), SETUP_COMPLETE, -1) == 1) return true;
            // A new client has no ownership proof. Its first recovery must use the service journal.
            if (!mayOwnChange || !supportedFirmware()) return false;
            if (context.checkSelfPermission(Manifest.permission.WRITE_SECURE_SETTINGS) != PackageManager.PERMISSION_GRANTED)
                return false;
            return Settings.Secure.putInt(context.getContentResolver(), SETUP_COMPLETE, 1)
                && Settings.Secure.getInt(context.getContentResolver(), SETUP_COMPLETE, -1) == 1;
        } catch (RuntimeException failure) { return false; }
    }

    static boolean setupComplete(Context context) {
        return Settings.Secure.getInt(context.getContentResolver(), SETUP_COMPLETE, -1) == 1;
    }

    /** Called only inside :edgeguard, including its direct-boot receiver. */
    static boolean recoverPending(Context context) {
        synchronized (SERIAL) {
            try { return createState(context).recover(); }
            catch (RuntimeException failure) { return false; }
        }
    }

    private static void diagnostic(String reason, boolean success) {
        android.util.Log.i("TabletEdgeGuard", "event=" + reason + " success=" + success);
    }

    private static TabletEdgeGuardState createState(Context context) {
        Context storage = context.createDeviceProtectedStorageContext();
        SharedPreferences journal = storage.getSharedPreferences("tablet_edge_guard_recovery", Context.MODE_PRIVATE);
        return new TabletEdgeGuardState(new TabletEdgeGuardState.Store() {
            @Override public boolean pending() { return journal.getBoolean("pending_restore", false); }
            @Override public boolean savePending(boolean pending) { return journal.edit().putBoolean("pending_restore", pending).commit(); }
            @Override public int setupComplete() { return Settings.Secure.getInt(context.getContentResolver(), SETUP_COMPLETE, -1); }
            @Override public boolean writeSetupComplete(int value) { return Settings.Secure.putInt(context.getContentResolver(), SETUP_COMPLETE, value); }
        }, new TabletEdgeGuardState.Environment() {
            @Override public boolean mayArm() { return TabletEdgeGuardService.mayArm(context); }
            @Override public long now() { return SystemClock.elapsedRealtime(); }
        });
    }
}
