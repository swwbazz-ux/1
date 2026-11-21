package ru.copperresources.mobile;

import android.webkit.CookieManager;
import android.webkit.URLUtil;
import android.widget.Toast;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

/** Authenticated exports stay private; sharing is a PIN-protected maintenance action. */
final class TabletReportDownloads {
    private static final long MAX_BYTES = 50L * 1024 * 1024;
    private final MiningMasterTabletActivity activity;
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final AtomicBoolean busy = new AtomicBoolean(false);
    private volatile boolean destroyed;

    private final TabletOfflineReportBridge offline;
    TabletReportDownloads(MiningMasterTabletActivity activity) {
        this.activity=activity;
        this.offline=new TabletOfflineReportBridge(activity.getFilesDir(),worker);
    }
    void install(android.webkit.WebView view) { offline.install(view); }

    void download(String url, String userAgent, String disposition, String mimeType) {
        if (!TabletUrlPolicy.trusted(url)) { message("Загрузка с внешнего адреса запрещена"); return; }
        if (!busy.compareAndSet(false, true)) { message("Предыдущий отчёт ещё загружается"); return; }
        String cookie = CookieManager.getInstance().getCookie(url);
        message("Загружаем отчёт…");
        worker.execute(() -> {
            long startedAt = System.nanoTime();
            File partial = null;
            HttpURLConnection connection = null;
            try {
                URL current = new URL(url);
                for (int redirect = 0; redirect <= 3; redirect++) {
                    if (!TabletUrlPolicy.trusted(current.toString())) throw new IOException("External redirect");
                    connection = (HttpURLConnection) current.openConnection();
                    connection.setInstanceFollowRedirects(false);
                    connection.setConnectTimeout(15_000);
                    connection.setReadTimeout(30_000);
                    connection.setRequestProperty("Accept-Encoding", "identity");
                    if (cookie != null) connection.setRequestProperty("Cookie", cookie);
                    if (userAgent != null) connection.setRequestProperty("User-Agent", userAgent);
                    int status = connection.getResponseCode();
                    if (status >= 300 && status <= 399) {
                        String location = connection.getHeaderField("Location");
                        if (location == null || redirect == 3) throw new IOException("Redirect limit");
                        URL next = new URL(current, location);
                        // Validate before forwarding session cookies to another request.
                        if (!TabletUrlPolicy.trusted(next.toString())) throw new IOException("External redirect");
                        connection.disconnect();
                        connection = null;
                        current = next;
                        continue;
                    }
                    if (status != 200) throw new IOException("Export failed");
                    break;
                }
                if (connection == null) throw new IOException("No response");
                String contentType = connection.getContentType();
                if (contentType == null || contentType.toLowerCase(Locale.ROOT).contains("text/html")) {
                    throw new IOException("Not an exported document");
                }
                long expectedLength = connection.getContentLengthLong();
                if (expectedLength > MAX_BYTES) throw new IOException("Export too large");
                String responseDisposition = connection.getHeaderField("Content-Disposition");
                String name = URLUtil.guessFileName(current.toString(),
                    responseDisposition == null ? disposition : responseDisposition, contentType);
                name = name.replaceAll("[^\\p{L}\\p{N}._ -]", "_");
                if (name.length() > 100) name = name.substring(name.length() - 100);
                String lower = name.toLowerCase(Locale.ROOT);
                if (!(lower.endsWith(".xlsx") || lower.endsWith(".xls") || lower.endsWith(".csv") || lower.endsWith(".pdf"))) {
                    throw new IOException("Unsupported export type");
                }
                name = name.substring(0, name.lastIndexOf('.')) + lower.substring(lower.lastIndexOf('.'));
                File directory = new File(activity.getFilesDir(), "reports");
                if (!directory.isDirectory() && !directory.mkdirs()) throw new IOException("Cannot create storage");
                long storedBytes = 0;
                File[] stored = directory.listFiles();
                if (stored != null) for (File file : stored) storedBytes += file.length();
                if (storedBytes > 200L * 1024 * 1024 || directory.getUsableSpace() < MAX_BYTES) {
                    throw new IOException("Report storage is full");
                }
                File target = new File(directory, System.currentTimeMillis() + "-" + name);
                partial = new File(directory, target.getName() + ".part");
                long total = 0;
                byte[] prefix = new byte[8];
                int prefixSize = 0;
                try (InputStream input = connection.getInputStream(); FileOutputStream output = new FileOutputStream(partial)) {
                    byte[] buffer = new byte[16_384];
                    int count;
                    while ((count = input.read(buffer)) != -1) {
                        if (destroyed || Thread.currentThread().isInterrupted()) throw new IOException("Download cancelled");
                        if (System.nanoTime() - startedAt > 120_000_000_000L) throw new IOException("Download timed out");
                        total += count;
                        if (total > MAX_BYTES) throw new IOException("Export too large");
                        for (int i = 0; i < count && prefixSize < prefix.length; i++) prefix[prefixSize++] = buffer[i];
                        output.write(buffer, 0, count);
                    }
                    output.getFD().sync();
                }
                if (expectedLength >= 0 && total != expectedLength) throw new IOException("Truncated export");
                if (total < 4 || (lower.endsWith(".xlsx") && !(prefix[0] == 'P' && prefix[1] == 'K'))
                    || (lower.endsWith(".pdf") && !(prefix[0] == '%' && prefix[1] == 'P' && prefix[2] == 'D' && prefix[3] == 'F'))
                    || prefix[0] == '<') throw new IOException("Export content is invalid");
                if (lower.endsWith(".xlsx")) {
                    try (java.util.zip.ZipFile workbook = new java.util.zip.ZipFile(partial)) {
                        if (workbook.getEntry("[Content_Types].xml") == null || workbook.getEntry("xl/workbook.xml") == null) {
                            throw new IOException("Invalid workbook");
                        }
                    }
                }
                if (!partial.renameTo(target)) throw new IOException("Cannot finish export");
                partial = null;
                message("Отчёт сохранён. Передача файла — в служебном меню, раздел «Сохранённые отчёты».");
            } catch (Exception failure) {
                message("Не удалось сохранить отчёт. Проверьте связь и вход в приложение, затем повторите экспорт.");
            } finally {
                if (connection != null) connection.disconnect();
                if (partial != null) partial.delete();
                busy.set(false);
            }
        });
    }

    private void message(String text) {
        activity.runOnUiThread(() -> {
            if (!destroyed && !activity.isFinishing()) Toast.makeText(activity, text, Toast.LENGTH_LONG).show();
        });
    }
    void destroy() { destroyed=true;offline.destroy();worker.shutdown(); }
}
