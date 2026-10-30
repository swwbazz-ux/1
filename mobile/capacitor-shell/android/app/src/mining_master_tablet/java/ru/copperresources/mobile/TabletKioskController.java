package ru.copperresources.mobile;

import android.app.ActivityManager;
import android.app.KeyguardManager;
import android.app.admin.DevicePolicyManager;
import android.content.ComponentName;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.os.UserManager;
import android.provider.Settings;
import android.text.InputType;
import android.view.WindowManager;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;
import androidx.appcompat.app.AlertDialog;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

final class TabletKioskController {
    private static final String LOG_TAG = "TabletKiosk";
    private static final long LOCK_CONFIRM_TIMEOUT_MS = 3_000L;
    private static final long LOCK_CONFIRM_POLL_MS = 100L;
    private static final String[] RESTRICTIONS = {
        UserManager.DISALLOW_SAFE_BOOT, UserManager.DISALLOW_ADD_USER,
        UserManager.DISALLOW_ADD_MANAGED_PROFILE, UserManager.DISALLOW_USER_SWITCH
    };
    private final MiningMasterTabletActivity activity;
    private final DevicePolicyManager dpm;
    private final ComponentName admin;
    private final SharedPreferences preferences;
    private final ExecutorService secrets = Executors.newSingleThreadExecutor();
    private final Handler lockHandler = new Handler(Looper.getMainLooper());
    private TabletLockTaskConfirmation lockConfirmation;
    private Runnable lockPoll;
    private String operationStage = "idle";
    private boolean restoringLock;
    private boolean resumed;
    private AlertDialog dialog;
    private boolean destroyed;

    TabletKioskController(MiningMasterTabletActivity activity) {
        this.activity = activity;
        dpm = activity.getSystemService(DevicePolicyManager.class);
        admin = new ComponentName(activity, MiningMasterDeviceAdminReceiver.class);
        preferences = activity.getSharedPreferences("mining_master_tablet_admin", 0);
    }

    private boolean owner() { return dpm != null && dpm.isDeviceOwnerApp(activity.getPackageName()); }
    private boolean enabled() { return preferences.getBoolean("kiosk_enabled", false); }
    private boolean pinExists() { return !preferences.getString("pin_hash", "").isEmpty(); }

    void offerInitialSetup() {
        if (!pinExists()) showPinSetup(false);
        else if (!enabled()) Toast.makeText(activity,
            "Обслуживание: удерживайте кнопку громкости + в течение 3 секунд", Toast.LENGTH_LONG).show();
    }

    void onResume() {
        resumed = true;
        if (destroyed || lockConfirmation != null) return;
        if (!enabled() && !preferences.getBoolean("kiosk_pending", false)) return;
        if (!owner() || !pinExists() || !preferences.getBoolean("recovery_verified", false)) {
            diagnostic("resume_prerequisites", -1, null);
            pauseKiosk();
            return;
        }
        KeyguardManager keyguard = activity.getSystemService(KeyguardManager.class);
        if (keyguard != null && keyguard.isKeyguardLocked()) return;
        beginLockAttempt(true);
    }

    void onPause() {
        resumed = false;
        // Persisted pending survives recreation, but this Activity must not complete an old callback.
        cancelLockConfirmation();
    }

    void showAdminEntry() {
        if (destroyed || (dialog != null && dialog.isShowing())) return;
        if (!pinExists()) { showPinSetup(false); return; }
        showPinChallenge("Обслуживание планшета",
            "Вход в обслуживание планшета. PIN приложения Горного мастера здесь не используется.",
            "Войти", this::showMenu, () -> {});
    }

    boolean authorizePrinting(Runnable authorized, Runnable cancelled) {
        if (!pinExists()) {
            Toast.makeText(activity, "Сначала задайте служебный PIN через обслуживание планшета.", Toast.LENGTH_LONG).show();
            return false;
        }
        return showPinChallenge("Печать отчёта",
            "После проверки служебного PIN защита будет временно приостановлена и откроется системная печать Android. "
                + "После печати или отмены верните рабочий режим. Отмена ввода PIN сохраняет защиту.",
            "Перейти к печати", authorized, cancelled);
    }

    private boolean showPinChallenge(String title, String explanation, String positive,
                                     Runnable authorized, Runnable cancelled) {
        if (destroyed || !resumed || (dialog != null && dialog.isShowing())) return false;
        long remaining = preferences.getLong("pin_retry_after", 0L) - System.currentTimeMillis();
        if (remaining > 0) {
            Toast.makeText(activity, "Повторный ввод через " + ((remaining + 999) / 1000) + " с", Toast.LENGTH_LONG).show();
            return false;
        }
        EditText pin = pinField("Служебный PIN");
        LinearLayout content = content();
        content.addView(note(explanation));
        content.addView(pin);
        boolean[] approved = {false};
        dialog = new AlertDialog.Builder(activity).setTitle(title)
            .setView(content).setNegativeButton("Отмена", null).setPositiveButton(positive, null).create();
        dialog.setOnDismissListener(unused -> { if (!approved[0]) cancelled.run(); });
        dialog.setOnShowListener(unused -> dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(button -> {
            String candidate = pin.getText().toString();
            pin.setText("");
            AlertDialog activeDialog = dialog;
            activeDialog.getButton(AlertDialog.BUTTON_POSITIVE).setEnabled(false);
            String encoded = preferences.getString("pin_hash", "");
            secrets.execute(() -> {
                boolean success = TabletPinCodec.matches(candidate, encoded);
                activity.runOnUiThread(() -> {
                    if (destroyed || activity.isFinishing() || !activeDialog.isShowing()) return;
                    if (success) {
                        boolean saved = preferences.edit().putInt("pin_failures", 0).putLong("pin_retry_after", 0)
                            .putBoolean("recovery_verified", true).commit();
                        if (!saved) {
                            activeDialog.dismiss();
                            showFailure("Не удалось сохранить проверку служебного входа. Повторите попытку.");
                            return;
                        }
                        approved[0] = true;
                        activeDialog.dismiss();
                        authorized.run();
                    } else {
                        int failures = preferences.getInt("pin_failures", 0) + 1;
                        long retry = failures >= 5 ? System.currentTimeMillis() + 60_000L : 0L;
                        preferences.edit().putInt("pin_failures", failures >= 5 ? 0 : failures)
                            .putLong("pin_retry_after", retry).commit();
                        pin.setError("Неверный служебный PIN");
                        if (retry > 0) {
                            activeDialog.dismiss();
                            Toast.makeText(activity, "Пять неверных попыток. Подождите одну минуту.", Toast.LENGTH_LONG).show();
                        } else activeDialog.getButton(AlertDialog.BUTTON_POSITIVE).setEnabled(true);
                    }
                });
            });
        }));
        showSecure(dialog);
        return true;
    }

    // Called only by the native print controller after a successful PIN challenge.
    boolean pauseForPrinting() { return pauseKiosk(); }

    void showReturnFromPrinting() {
        if (destroyed || !resumed || (dialog != null && dialog.isShowing())) return;
        dialog = new AlertDialog.Builder(activity).setTitle("Вернуть рабочий режим?")
            .setMessage("После системной печати планшет остаётся в обслуживании. "
                + "Верните рабочий режим, чтобы снова ограничить планшет приложением Горного мастера.")
            .setPositiveButton("Вернуть рабочий режим", (unused, which) -> confirmEnable())
            .setNegativeButton("Оставить обслуживание", null).create();
        showSecure(dialog);
    }

    private void showPinSetup(boolean replacement) {
        if (destroyed) return;
        EditText first = pinField("Новый PIN: 6–12 цифр");
        EditText second = pinField("Повторите PIN");
        LinearLayout content = content();
        content.addView(note("Задайте отдельный служебный PIN и сохраните его у ответственного за планшет. "
            + "Выход в обслуживание работает без сети: удерживайте громкость + в течение 3 секунд. "
            + "Перед включением защиты потребуется ещё раз проверить PIN."));
        content.addView(first);
        content.addView(second);
        dialog = new AlertDialog.Builder(activity).setTitle(replacement ? "Новый служебный PIN" : "Подготовка рабочего планшета")
            .setView(content).setNegativeButton("Позже", null).setPositiveButton("Сохранить и проверить", null).create();
        dialog.setOnShowListener(unused -> dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(button -> {
            String value = first.getText().toString();
            if (!TabletPinCodec.valid(value)) { first.setError("Нужно от 6 до 12 цифр"); return; }
            if (!value.equals(second.getText().toString())) { second.setError("PIN не совпадают"); return; }
            AlertDialog activeDialog = dialog;
            first.setText(""); second.setText("");
            activeDialog.getButton(AlertDialog.BUTTON_POSITIVE).setEnabled(false);
            secrets.execute(() -> {
                try {
                    String encoded = TabletPinCodec.create(value);
                    boolean saved = preferences.edit().putString("pin_hash", encoded)
                        .putBoolean("recovery_verified", false).putInt("pin_failures", 0)
                        .putLong("pin_retry_after", 0).commit();
                    activity.runOnUiThread(() -> {
                        if (destroyed) return;
                        activeDialog.dismiss();
                        if (saved) showAdminEntry();
                        else showFailure("Не удалось сохранить служебный PIN. Защита не включена.");
                    });
                } catch (Exception error) {
                    activity.runOnUiThread(() -> {
                        if (destroyed) return;
                        activeDialog.dismiss();
                        showFailure("Не удалось подготовить служебный PIN. Защита не включена.");
                    });
                }
            });
        }));
        showSecure(dialog);
    }

    private void showMenu() {
        ActivityManager manager = activity.getSystemService(ActivityManager.class);
        boolean locked = manager != null && manager.getLockTaskModeState() == ActivityManager.LOCK_TASK_MODE_LOCKED;
        String status = "Управление устройством: " + (owner() ? "Device Owner" : "ещё не назначено")
            + "\nЗащищённый режим: " + (locked ? "включён" : "выключен")
            + "\nСлужебный вход проверен. USB-отладка не заблокирована.";
        String[] choices = {
            "Включить рабочий режим", "Приостановить защиту для обслуживания",
            "Настроить Wi-Fi", "Открыть настройки Android", "Сохранённые отчёты", "Изменить служебный PIN",
            "Снять управление этим планшетом"
        };
        dialog = new AlertDialog.Builder(activity).setTitle("Обслуживание планшета")
            .setMessage(status).setPositiveButton("Действия", (unused, which) -> {
                dialog = new AlertDialog.Builder(activity).setTitle("Служебные действия")
                    .setItems(choices, (selected, index) -> {
                        if (index == 0) confirmEnable();
                        else if (index == 1) pauseKiosk();
                        else if (index == 2) openSettings(Settings.ACTION_WIFI_SETTINGS);
                        else if (index == 3) openSettings(Settings.ACTION_SETTINGS);
                        else if (index == 4) showReports();
                        else if (index == 5) { if (pauseKiosk()) showPinSetup(true); }
                        else confirmDeprovision();
                    }).setNegativeButton("Закрыть", null).create();
                showSecure(dialog);
            }).setNegativeButton("Закрыть", null).create();
        showSecure(dialog);
    }

    private void confirmEnable() {
        if (!owner()) {
            showFailure("Приложение ещё не назначено владельцем устройства. Обычное закрепление экрана не заменяет защиту. "
                + "Ответственный должен завершить подготовку Android; данные автоматически не удаляются.");
            return;
        }
        if (!preferences.getBoolean("recovery_verified", false)) { showAdminEntry(); return; }
        dialog = new AlertDialog.Builder(activity).setTitle("Включить рабочий режим?")
            .setMessage("После включения планшет будет запускать только Горного мастера. "
                + "Выход: удерживать громкость + 3 секунды и ввести проверенный служебный PIN.")
            .setNegativeButton("Отмена", null).setPositiveButton("Включить", (unused, which) -> enableKiosk()).create();
        showSecure(dialog);
    }

    private void applyPolicies() {
        operationStage = "policy_allowlist";
        dpm.setLockTaskPackages(admin, new String[] {activity.getPackageName()});
        operationStage = "policy_features";
        if (Build.VERSION.SDK_INT >= 28) dpm.setLockTaskFeatures(admin, DevicePolicyManager.LOCK_TASK_FEATURE_NONE);
        IntentFilter home = new IntentFilter(Intent.ACTION_MAIN);
        home.addCategory(Intent.CATEGORY_HOME);
        home.addCategory(Intent.CATEGORY_DEFAULT);
        operationStage = "policy_home";
        dpm.addPersistentPreferredActivity(admin, home, new ComponentName(activity, MiningMasterTabletActivity.class));
        for (String restriction : RESTRICTIONS) {
            if (Build.VERSION.SDK_INT < 28 && UserManager.DISALLOW_USER_SWITCH.equals(restriction)) continue;
            operationStage = "policy_" + restriction;
            dpm.addUserRestriction(admin, restriction);
        }
        operationStage = "policy_statusbar";
        if (!dpm.setStatusBarDisabled(admin, true)) {
            throw new IllegalStateException("Android status bar policy was not applied");
        }
        operationStage = "policy_keyguard";
        if (!dpm.setKeyguardDisabled(admin, true)) {
            throw new IllegalStateException("Android keyguard requires administrator preparation");
        }
    }

    private void enableKiosk() {
        if (!owner() || !preferences.getBoolean("recovery_verified", false)) return;
        KeyguardManager keyguard = activity.getSystemService(KeyguardManager.class);
        if (keyguard != null && keyguard.isKeyguardLocked()) { showFailure("Сначала разблокируйте Android."); return; }
        beginLockAttempt(false);
    }

    private void beginLockAttempt(boolean restoring) {
        if (destroyed || !resumed || lockConfirmation != null) return;
        restoringLock = restoring;
        try {
            // Record intent before changing Android policies. A process death retries this path.
            operationStage = "persist_pending";
            if (!preferences.edit().putBoolean("kiosk_pending", true).putBoolean("kiosk_enabled", false).commit()) {
                throw new IllegalStateException();
            }
            applyPolicies();
            operationStage = "check_allowlist";
            if (!dpm.isLockTaskPermitted(activity.getPackageName())) throw new IllegalStateException();
            lockConfirmation = new TabletLockTaskConfirmation(SystemClock.elapsedRealtime(), LOCK_CONFIRM_TIMEOUT_MS);
            TabletLockTaskConfirmation attempt = lockConfirmation;
            operationStage = "start_lock_task";
            activity.startLockTask();
            diagnostic("start_requested", -1, null);
            lockPoll = () -> confirmLockTask(attempt);
            // Android schedules its actual lock-state transition on a system Handler.
            lockHandler.post(lockPoll);
        } catch (RuntimeException failure) {
            failLockAttempt(operationStage, -1, failure);
        }
    }

    private void confirmLockTask(TabletLockTaskConfirmation attempt) {
        if (destroyed || !resumed || lockConfirmation != attempt) return;
        int state = -1;
        try {
            operationStage = "confirm_state";
            ActivityManager manager = activity.getSystemService(ActivityManager.class);
            if (manager == null) throw new IllegalStateException();
            state = manager.getLockTaskModeState();
            TabletLockTaskConfirmation.SystemState observed;
            if (state == ActivityManager.LOCK_TASK_MODE_LOCKED) observed = TabletLockTaskConfirmation.SystemState.LOCKED;
            else if (state == ActivityManager.LOCK_TASK_MODE_PINNED) observed = TabletLockTaskConfirmation.SystemState.PINNED;
            else if (state == ActivityManager.LOCK_TASK_MODE_NONE) observed = TabletLockTaskConfirmation.SystemState.NONE;
            else throw new IllegalStateException();
            TabletLockTaskConfirmation.Outcome result = attempt.observe(observed, SystemClock.elapsedRealtime());
            if (result == TabletLockTaskConfirmation.Outcome.PENDING) {
                lockHandler.postDelayed(lockPoll, LOCK_CONFIRM_POLL_MS);
                return;
            }
            if (result == TabletLockTaskConfirmation.Outcome.LOCKED) {
                operationStage = "persist_confirmed";
                if (!preferences.edit().putBoolean("kiosk_enabled", true).putBoolean("kiosk_pending", false).commit()) {
                    throw new IllegalStateException();
                }
                boolean showEnabled = !restoringLock;
                diagnostic("confirmed_locked", state, null);
                cancelLockConfirmation();
                if (showEnabled) Toast.makeText(activity, "Защищённый рабочий режим включён", Toast.LENGTH_LONG).show();
                return;
            }
            if (result == TabletLockTaskConfirmation.Outcome.PINNED) {
                failLockAttempt("confirm_pinned", state, new IllegalStateException());
            } else if (result == TabletLockTaskConfirmation.Outcome.TIMED_OUT) {
                failLockAttempt("confirm_timeout", state, new java.util.concurrent.TimeoutException());
            }
        } catch (RuntimeException failure) {
            failLockAttempt(operationStage, state, failure);
        }
    }

    private void failLockAttempt(String stage, int state, Throwable failure) {
        boolean wasRestoring = restoringLock;
        diagnostic(stage, state, failure);
        if (pauseKiosk()) {
            showFailure(wasRestoring
                ? "Не удалось восстановить защищённый режим. Откройте обслуживание кнопкой громкости +."
                : "Не удалось включить защищённый режим. Устройство оставлено для обслуживания.");
        }
    }

    private void cancelLockConfirmation() {
        if (lockConfirmation != null) lockConfirmation.cancel();
        lockConfirmation = null;
        if (lockPoll != null) lockHandler.removeCallbacks(lockPoll);
        lockPoll = null;
    }

    private void diagnostic(String stage, int state, Throwable failure) {
        // Never log exception messages/stacks, URL, account, PIN, hash, cookie, or preference data.
        String line = "phase=" + (restoringLock ? "resume" : "enable") + " stage=" + stage
            + " state=" + state + " error=" + (failure == null ? "none" : failure.getClass().getSimpleName());
        if (failure == null) android.util.Log.i(LOG_TAG, line);
        else android.util.Log.w(LOG_TAG, line);
    }

    private boolean pauseKiosk() {
        cancelLockConfirmation();
        // Persist maintenance first so a process restart cannot silently lock the administrator out.
        if (!preferences.edit().putBoolean("kiosk_enabled", false).putBoolean("kiosk_pending", false).commit()) {
            diagnostic("maintenance_persist", -1, new IllegalStateException());
            showFailure("Не удалось сохранить режим обслуживания. Повторите попытку."); return false;
        }
        java.util.List<Runnable> steps = new java.util.ArrayList<>();
        steps.add(() -> {
            ActivityManager manager = activity.getSystemService(ActivityManager.class);
            if (manager != null && manager.getLockTaskModeState() != ActivityManager.LOCK_TASK_MODE_NONE) activity.stopLockTask();
        });
        if (owner()) {
            steps.add(() -> dpm.setLockTaskPackages(admin, new String[0]));
            steps.add(() -> dpm.clearPackagePersistentPreferredActivities(admin, activity.getPackageName()));
            steps.add(() -> {
                if (!dpm.setStatusBarDisabled(admin, false)) throw new IllegalStateException();
            });
            steps.add(() -> {
                if (!dpm.setKeyguardDisabled(admin, false)) throw new IllegalStateException();
            });
            for (String restriction : RESTRICTIONS) {
                if (Build.VERSION.SDK_INT < 28 && UserManager.DISALLOW_USER_SWITCH.equals(restriction)) continue;
                steps.add(() -> dpm.clearUserRestriction(admin, restriction));
            }
        }
        boolean complete = true;
        // Recovery must attempt every policy even when one OEM API refuses a change.
        int stepIndex = 0;
        for (Runnable step : steps) {
            try { step.run(); }
            catch (RuntimeException failure) {
                complete = false;
                diagnostic("maintenance_step_" + stepIndex, -1, failure);
            }
            stepIndex++;
        }
        if (complete) {
            Toast.makeText(activity, "Защита приостановлена. Верните рабочий режим после обслуживания.", Toast.LENGTH_LONG).show();
            return true;
        } else {
            showFailure("Часть ограничений не снята. Сохранён режим обслуживания; используйте доверенный USB-доступ.");
            return false;
        }
    }

    private void openSettings(String action) {
        if (!pauseKiosk()) return;
        try { activity.startActivity(new Intent(action)); }
        catch (RuntimeException error) { showFailure("Настройки недоступны на этой модели Android."); }
    }

    private void showReports() {
        java.io.File directory = new java.io.File(activity.getFilesDir(), "reports");
        java.io.File[] files = directory.listFiles(file -> file.isFile()
            && file.getName().matches("[0-9]+-.+\\.(xlsx|xls|csv|pdf)"));
        if (files == null || files.length == 0) { showFailure("Сохранённых отчётов пока нет. Используйте кнопку Excel в отчёте."); return; }
        java.util.Arrays.sort(files, (first, second) -> Long.compare(second.lastModified(), first.lastModified()));
        String[] labels = new String[files.length];
        for (int i = 0; i < files.length; i++) labels[i] = files[i].getName();
        dialog = new AlertDialog.Builder(activity).setTitle("Сохранённые отчёты")
            .setItems(labels, (unused, index) -> {
                java.io.File file = files[index];
                dialog = new AlertDialog.Builder(activity).setTitle(file.getName())
                    .setMessage("Передача файла приостановит защиту и откроет системный выбор приложения. После передачи включите рабочий режим снова.")
                    .setPositiveButton("Передать", (selected, button) -> shareReport(file))
                    .setNeutralButton("Удалить файл", (selected, button) -> {
                        if (!file.delete()) showFailure("Не удалось удалить файл.");
                        else showReports();
                    }).setNegativeButton("Отмена", null).create();
                showSecure(dialog);
            }).setNegativeButton("Закрыть", null).create();
        showSecure(dialog);
    }

    private void shareReport(java.io.File file) {
        if (!pauseKiosk()) return;
        try {
            android.net.Uri uri = androidx.core.content.FileProvider.getUriForFile(activity,
                activity.getPackageName() + ".fileprovider", file);
            String extension = file.getName().substring(file.getName().lastIndexOf('.') + 1);
            String type = android.webkit.MimeTypeMap.getSingleton().getMimeTypeFromExtension(extension);
            Intent send = new Intent(Intent.ACTION_SEND).setType(type == null ? "application/octet-stream" : type)
                .putExtra(Intent.EXTRA_STREAM, uri).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
            send.setClipData(android.content.ClipData.newRawUri("Отчёт", uri));
            activity.startActivity(Intent.createChooser(send, "Передать отчёт"));
        } catch (RuntimeException error) { showFailure("Передача файла недоступна. Файл сохранён в приложении."); }
    }

    @SuppressWarnings("deprecation")
    private void confirmDeprovision() {
        dialog = new AlertDialog.Builder(activity).setTitle("Снять управление планшетом?")
            .setMessage("Защита и автозапуск будут отключены. Приложение и его данные останутся. "
                + "Повторное назначение владельца потребует подготовки Android.")
            .setNegativeButton("Отмена", null).setPositiveButton("Снять управление", (unused, which) -> {
                if (!pauseKiosk()) return;
                try {
                    if (owner()) dpm.clearDeviceOwnerApp(activity.getPackageName());
                    showFailure("Управление снято. Горный мастер остаётся обычным установленным приложением.");
                } catch (RuntimeException failure) { showFailure("Android не разрешил снять управление. Защита приостановлена."); }
            }).create();
        showSecure(dialog);
    }

    private EditText pinField(String hint) {
        EditText edit = new EditText(activity);
        edit.setHint(hint);
        edit.setSingleLine(true);
        edit.setInputType(InputType.TYPE_CLASS_NUMBER | InputType.TYPE_NUMBER_VARIATION_PASSWORD);
        edit.setFilters(new android.text.InputFilter[] {new android.text.InputFilter.LengthFilter(12)});
        if (Build.VERSION.SDK_INT >= 26) edit.setImportantForAutofill(android.view.View.IMPORTANT_FOR_AUTOFILL_NO);
        return edit;
    }

    private TextView note(String text) { TextView view = new TextView(activity); view.setText(text); return view; }
    private LinearLayout content() {
        LinearLayout layout = new LinearLayout(activity);
        layout.setOrientation(LinearLayout.VERTICAL);
        int padding = (int) (24 * activity.getResources().getDisplayMetrics().density);
        layout.setPadding(padding, padding / 2, padding, padding / 2);
        return layout;
    }
    private void showSecure(AlertDialog next) {
        if (destroyed || activity.isFinishing()) return;
        if (next.getWindow() != null) next.getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
        next.show();
    }
    private void showFailure(String message) {
        if (destroyed || activity.isFinishing()) return;
        dialog = new AlertDialog.Builder(activity).setTitle("Рабочий планшет").setMessage(message)
            .setPositiveButton("Понятно", null).create();
        showSecure(dialog);
    }
    void destroy() {
        destroyed = true;
        cancelLockConfirmation();
        lockHandler.removeCallbacksAndMessages(null);
        secrets.shutdown();
        if (dialog != null) dialog.dismiss();
    }
}
