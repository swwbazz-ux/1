package ru.copperresources.mobile;

import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.ServiceConnection;
import android.os.Binder;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.Parcel;
import android.os.RemoteException;
import java.util.function.BooleanSupplier;

/** UI-thread client. Only the remote service may arm; this process may always request recovery. */
final class TabletEdgeGuard {
    private static final int MAX_REBIND_ATTEMPTS = 3;
    private final Context context;
    private final BooleanSupplier allowed;
    private final Runnable changed;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final IBinder token = new Binder();
    private IBinder remote;
    private boolean bound;
    private boolean resumed;
    private boolean active;
    private boolean recoveryReady;
    private boolean mayOwnChange;
    private boolean destroyed;
    private int rebindAttempts;
    private ServiceConnection connection;

    private final Runnable connectionTimeout = () -> {
        if (destroyed || !bound || remote != null) return;
        android.util.Log.i("TabletEdgeGuard", "event=client_bind_timeout");
        unbind();
        scheduleRebind();
    };

    private final Runnable rebind = () -> {
        if (destroyed || !resumed || bound || rebindAttempts >= MAX_REBIND_ATTEMPTS) return;
        rebindAttempts++;
        android.util.Log.i("TabletEdgeGuard", "event=client_rebind attempt=" + rebindAttempts);
        bind();
    };

    private final Runnable tick = new Runnable() {
        @Override public void run() {
            if (destroyed || !resumed) return;
            try {
                if (!recoveryReady) recoveryReady = release();
                if (!recoveryReady || !allowed.getAsBoolean() || !TabletEdgeGuardService.mayArm(context)) {
                    if (active) release();
                } else if (remote != null) {
                    setActive(call(active ? TabletEdgeGuardService.HEARTBEAT : TabletEdgeGuardService.ARM));
                }
            } catch (RuntimeException | RemoteException failure) {
                release();
            }
            handler.postDelayed(this, 1_000L);
        }
    };

    private ServiceConnection newConnection() { return new ServiceConnection() {
        @Override public void onServiceConnected(ComponentName name, IBinder service) {
            if (destroyed || connection != this) return;
            handler.removeCallbacks(connectionTimeout);
            remote = service;
            // Recovery is acknowledged before considering the current work-mode predicate.
            recoveryReady = release();
            scheduleTick();
        }
        @Override public void onServiceDisconnected(ComponentName name) {
            if (connection != this) return;
            remote = null;
            recoveryReady = TabletEdgeGuardService.restoreFromClient(context, mayOwnChange);
            if (recoveryReady) mayOwnChange = false;
            setActive(false);
            android.util.Log.i("TabletEdgeGuard", "event=client_disconnect_recovery success=" + recoveryReady);
            // This OEM can discard a crashed service binding without issuing onBindingDied.
            // Do not leave a stale bound=true waiting forever for an automatic reconnection.
            unbind();
            scheduleRebind();
        }
        @Override public void onBindingDied(ComponentName name) {
            onServiceDisconnected(name);
        }
        @Override public void onNullBinding(ComponentName name) {
            onServiceDisconnected(name);
        }
    }; }

    TabletEdgeGuard(Context context, BooleanSupplier allowed, Runnable changed) {
        this.context = context.getApplicationContext();
        this.allowed = allowed;
        this.changed = changed;
        // The service recovers a previous process's journal before accepting our first ARM.
    }

    void onResume() {
        if (destroyed) return;
        resumed = true;
        rebindAttempts = 0;
        handler.removeCallbacks(rebind);
        bind();
        scheduleTick();
    }

    void onPause() {
        resumed = false;
        handler.removeCallbacks(tick);
        handler.removeCallbacks(rebind);
        release();
    }

    boolean active() { return active; }

    /** Synchronous readback is required before opening Android settings or ending lock task. */
    boolean release() {
        boolean restored = false;
        try {
            if (remote != null) restored = call(TabletEdgeGuardService.RELEASE);
        } catch (RuntimeException | RemoteException failure) { /* Restore-only fallback below. */ }
        if (!restored) restored = TabletEdgeGuardService.restoreFromClient(context, mayOwnChange);
        if (restored) mayOwnChange = false;
        recoveryReady = restored;
        setActive(false);
        return restored;
    }

    void destroy() {
        destroyed = true;
        resumed = false;
        handler.removeCallbacks(tick);
        handler.removeCallbacks(rebind);
        release();
        unbind();
    }

    private void bind() {
        if (bound || destroyed || !resumed) return;
        connection = newConnection();
        try {
            bound = context.bindService(new Intent(context, TabletEdgeGuardService.class), connection, Context.BIND_AUTO_CREATE);
        } catch (RuntimeException failure) {
            recoveryReady = TabletEdgeGuardService.restoreFromClient(context, mayOwnChange);
            if (recoveryReady) mayOwnChange = false;
        }
        if (!bound) { connection = null; scheduleRebind(); }
        else handler.postDelayed(connectionTimeout, 3_000L);
    }

    private void unbind() {
        handler.removeCallbacks(connectionTimeout);
        remote = null;
        ServiceConnection previous = connection;
        connection = null;
        if (!bound || previous == null) return;
        bound = false;
        try { context.unbindService(previous); } catch (RuntimeException ignored) { }
    }

    private void scheduleRebind() {
        handler.removeCallbacks(rebind);
        if (!destroyed && resumed && !bound && rebindAttempts < MAX_REBIND_ATTEMPTS)
            handler.postDelayed(rebind, 1_000L);
    }

    private void scheduleTick() {
        handler.removeCallbacks(tick);
        if (resumed && !destroyed) handler.post(tick);
    }

    private boolean call(int operation) throws RemoteException {
        IBinder service = remote;
        if (service == null) return false;
        Parcel request = Parcel.obtain();
        Parcel response = Parcel.obtain();
        try {
            request.writeInterfaceToken(TabletEdgeGuardService.DESCRIPTOR);
            request.writeStrongBinder(token);
            // Record local ownership before IPC: the service can write 0 and die before replying.
            if (operation == TabletEdgeGuardService.ARM) {
                if (!mayOwnChange && !TabletEdgeGuardService.setupComplete(context)) return false;
                mayOwnChange = true;
            }
            if (!service.transact(operation, request, response, 0)) return false;
            response.readException();
            return response.readInt() == 1;
        } finally {
            request.recycle();
            response.recycle();
        }
    }

    private void setActive(boolean value) {
        if (active == value) return;
        active = value;
        changed.run();
    }
}
