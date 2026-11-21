package ru.copperresources.mobile;

import android.app.job.JobParameters;
import android.app.job.JobService;

/** Android restores pending transport work after process death/reboot and network recovery. */
public final class TabletMaxJobService extends JobService {
    private int generation;
    @Override public boolean onStartJob(JobParameters params) {
        int attempt = ++generation;
        TabletMaxBackground.get(this).runJob(() -> { if (generation == attempt) jobFinished(params, false); });
        return true;
    }
    @Override public boolean onStopJob(JobParameters params) { generation++; return true; }
}
