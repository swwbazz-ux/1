package ru.copperresources.mobile;

import java.util.Arrays;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/** Central mapping from legacy event cues to the current operational sound language. */
final class OperationalCueCatalog {
    static final String ASSIGNMENT = "assignment_notice";
    static final String ACTION_SUCCESS = "action_success_notice";
    static final String ASSIGNMENT_REMOVED = "assignment_removed_notice";
    static final String SHIFT = "shift_notice";
    static final String ACTION_FAILED = "action_failed_notice";
    static final String CONNECTION_LOST = "connection_lost_notice";
    static final String CONNECTION_RESTORED = "connection_restored_notice";

    private static final Set<String> SUPPORTED_NAMES = Collections.unmodifiableSet(
        new HashSet<>(Arrays.asList(
            "truck_assigned",
            "action_ok",
            "action_error",
            "connection_lost",
            "connection_restored",
            "shift_start",
            "shift_end",
            ASSIGNMENT,
            ACTION_SUCCESS,
            ASSIGNMENT_REMOVED,
            SHIFT,
            ACTION_FAILED,
            CONNECTION_LOST,
            CONNECTION_RESTORED
        ))
    );

    private OperationalCueCatalog() {}

    static boolean isSupported(String cueName) {
        return cueName != null && SUPPORTED_NAMES.contains(cueName);
    }

    static String forDirectCue(String requestedCue) {
        if (requestedCue == null) {
            return "";
        }
        switch (requestedCue) {
            case "truck_assigned":
                return ASSIGNMENT;
            case "action_ok":
                return ACTION_SUCCESS;
            case "action_error":
                return ACTION_FAILED;
            case "connection_lost":
                return CONNECTION_LOST;
            case "connection_restored":
                return CONNECTION_RESTORED;
            case "shift_start":
            case "shift_end":
                return SHIFT;
            default:
                return requestedCue;
        }
    }

    static String forOperational(String requestedCue, String voiceName) {
        if ("voice_shift_opened".equals(voiceName) || "voice_shift_closed".equals(voiceName)) {
            return SHIFT;
        }
        if ("voice_connection_lost".equals(voiceName)) {
            return CONNECTION_LOST;
        }
        if ("voice_connection_restored".equals(voiceName)) {
            return CONNECTION_RESTORED;
        }
        if ("voice_assignment_removed".equals(voiceName)
                || "voice_truck_removed".equals(voiceName)) {
            return ASSIGNMENT_REMOVED;
        }
        if ("voice_action_failed".equals(voiceName)
                || "voice_trip_finish_failed".equals(voiceName)
                || "voice_truck_send_failed".equals(voiceName)) {
            return ACTION_FAILED;
        }
        if ("voice_trip_finished".equals(voiceName)
                || "voice_downtime_started".equals(voiceName)
                || "voice_downtime_finished".equals(voiceName)
                || "voice_face_settings_saved".equals(voiceName)
                || "voice_truck_sent".equals(voiceName)) {
            return ACTION_SUCCESS;
        }
        if ("voice_excavator_assigned".equals(voiceName)
                || "voice_excavator_changed".equals(voiceName)
                || "voice_truck_assigned".equals(voiceName)) {
            return ASSIGNMENT;
        }
        return forDirectCue(requestedCue);
    }

    static String forEquipment(String requestedCue, String action) {
        if ("excavator_truck_removed".equals(action)) {
            return ASSIGNMENT_REMOVED;
        }
        if ("excavator_truck_sent".equals(action)) {
            return ACTION_SUCCESS;
        }
        if ("excavator_truck_assigned".equals(action)
                || "driver_excavator_assigned".equals(action)) {
            return ASSIGNMENT;
        }
        return forDirectCue(requestedCue);
    }

    static String forEquipmentBatch(String requestedCue, List<String> actions) {
        boolean sawAssignment = false;
        boolean sawRemoval = false;
        boolean sawSuccess = false;
        if (actions != null) {
            for (String action : actions) {
                if ("excavator_truck_removed".equals(action)) {
                    sawRemoval = true;
                } else if ("excavator_truck_sent".equals(action)) {
                    sawSuccess = true;
                } else if ("excavator_truck_assigned".equals(action)
                        || "driver_excavator_assigned".equals(action)) {
                    sawAssignment = true;
                }
            }
        }
        if (sawRemoval && !sawAssignment && !sawSuccess) {
            return ASSIGNMENT_REMOVED;
        }
        if (sawSuccess && !sawAssignment && !sawRemoval) {
            return ACTION_SUCCESS;
        }
        if (sawAssignment || sawRemoval || sawSuccess) {
            // A mixed batch is one indivisible announcement. Use the important generic assignment cue.
            return ASSIGNMENT;
        }
        return forDirectCue(requestedCue);
    }
}
