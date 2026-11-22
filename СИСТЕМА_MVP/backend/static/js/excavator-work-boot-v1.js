(function () {
var scheduleExcavatorViewportHeightSync = window.ExcavatorWorkRuntime.scheduleExcavatorViewportHeightSync;
var syncExcavatorAssignmentSnapshot = window.ExcavatorWorkFeedback.syncExcavatorAssignmentSnapshot;
var initExcavatorPwaUpdates = window.ExcavatorWorkPwa.initExcavatorPwaUpdates;

document.addEventListener("DOMContentLoaded", function () {
    scheduleExcavatorViewportHeightSync();
    window.initExcavatorWorkShell();
    syncExcavatorAssignmentSnapshot(document.querySelector("[data-eo-shell]"));
    initExcavatorPwaUpdates();
    scheduleExcavatorViewportHeightSync();
});
})();
