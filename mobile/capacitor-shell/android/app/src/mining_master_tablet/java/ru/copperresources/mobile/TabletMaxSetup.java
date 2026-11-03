package ru.copperresources.mobile;

import android.app.Activity;
import android.app.AlertDialog;
import android.graphics.Bitmap;
import android.view.WindowManager;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;
import com.google.zxing.BarcodeFormat;
import com.google.zxing.EncodeHintType;
import com.google.zxing.MultiFormatWriter;
import com.google.zxing.common.BitMatrix;
import java.util.Collections;
import java.io.File;
import java.io.FileOutputStream;
import android.util.AtomicFile;
import org.json.JSONObject;

/** One-time native QR pairing. Runtime sending never opens this or the MAX application. */
final class TabletMaxSetup {
    private static AlertDialog open;
    static void show(Activity activity, TabletMaxBackground transport) {
        if (open != null && open.isShowing()) return;
        LinearLayout view = new LinearLayout(activity); view.setOrientation(LinearLayout.VERTICAL);
        int pad = Math.round(18 * activity.getResources().getDisplayMetrics().density);
        view.setPadding(pad, pad / 2, pad, pad / 2);
        TextView hint = new TextView(activity); hint.setTextSize(16);
        hint.setText("Разовое подключение служебного аккаунта. В MAX откройте Настройки → Устройства → Войти по QR-коду. После подтверждения отправка работает без переключения экрана.");
        view.addView(hint);
        ImageView image = new ImageView(activity); image.setContentDescription("QR-код подключения служебного MAX");
        int size = Math.round(290 * activity.getResources().getDisplayMetrics().density);
        LinearLayout.LayoutParams picture = new LinearLayout.LayoutParams(size, size); picture.gravity = android.view.Gravity.CENTER_HORIZONTAL;
        view.addView(image, picture);
        TextView status = new TextView(activity); status.setText("Создаём защищённое подключение…"); status.setTextSize(16); view.addView(status);
        File qrDirectory = new File(activity.getCacheDir(), "max-pairing");
        if (!qrDirectory.isDirectory() && !qrDirectory.mkdirs()) throw new IllegalStateException("max_qr_directory");
        File exportedQr = new File(qrDirectory, "current.png");
        android.net.Uri qrUri = androidx.core.content.FileProvider.getUriForFile(activity, activity.getPackageName() + ".fileprovider", exportedQr);
        AlertDialog dialog = new AlertDialog.Builder(activity).setTitle("Подключение служебного MAX").setView(view)
            .setNegativeButton("Отмена", (ignored, which) -> transport.cancelPairing()).create();
        open = dialog; dialog.show();
        dialog.getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_STATE_ALWAYS_HIDDEN);
        dialog.setOnCancelListener(ignored -> transport.cancelPairing());
        dialog.setOnDismissListener(ignored -> {
            if (open == dialog) open = null;
            activity.revokeUriPermission(qrUri, android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION); exportedQr.delete();
        });
        transport.startPairing(new TabletMaxBackground.Result() {
            public void done(JSONObject value) {
                if (!dialog.isShowing()) return;
                if (!value.has("qrLink")) {
                    dialog.dismiss(); Toast.makeText(activity, "MAX подключён. Расстановка отправляется без переключения экрана.", Toast.LENGTH_LONG).show(); return;
                }
                try {
                    BitMatrix matrix = new MultiFormatWriter().encode(value.getString("qrLink"), BarcodeFormat.QR_CODE, 600, 600,
                        Collections.singletonMap(EncodeHintType.MARGIN, 3));
                    Bitmap bitmap = Bitmap.createBitmap(600, 600, Bitmap.Config.RGB_565);
                    for (int y = 0; y < 600; y++) for (int x = 0; x < 600; x++) bitmap.setPixel(x, y, matrix.get(x, y) ? 0xff000000 : 0xffffffff);
                    // A short-lived public pairing challenge, never a session credential.
                    // A grant for this image alone lets the trusted USB shell display it
                    // while the tablet's camera scans the computer screen. No private
                    // session/outbox files are shared. Revoke/delete on dismissal.
                    AtomicFile export = new AtomicFile(exportedQr); FileOutputStream output = export.startWrite();
                    try { bitmap.compress(Bitmap.CompressFormat.PNG, 100, output); export.finishWrite(output); }
                    catch (Exception failure) { export.failWrite(output); throw failure; }
                    activity.grantUriPermission("com.android.shell", qrUri, android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION);
                    image.setImageBitmap(bitmap); status.setText("Ожидаем подтверждение в служебном аккаунте MAX…");
                } catch (Exception failure) { status.setText("Не удалось показать QR-код."); transport.cancelPairing(); }
            }
            public void failed(String message) { if (dialog.isShowing()) status.setText(message); }
        });
    }
    private TabletMaxSetup() {}
}
