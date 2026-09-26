/* Dispatcher projected haul-assignment state.
   Owns only the version tokens returned by assignment commands; transport,
   drag-and-drop and DOM placement stay in the board module. */
(function (global) {
    "use strict";

    function createDispatcherHaulAssignmentState() {
        function getStateId(node) {
            var value = node && node.dataset ? node.dataset.haulAssignmentStateId : "";
            return /^\d+$/.test(String(value || "")) ? String(value) : "0";
        }

        function collectComplexStates(complexCard) {
            var states = {};
            if (!complexCard) return states;
            complexCard.querySelectorAll(
                "[data-complex-truck='true'][data-equipment-id], " +
                "[data-mm-mobile-home-truck-id]"
            ).forEach(function (truck) {
                var truckId = truck.dataset.equipmentId || truck.dataset.mmMobileHomeTruckId || "";
                if (truckId) states[String(truckId)] = getStateId(truck);
            });
            return states;
        }

        function applyState(response, truckNode) {
            if (!response || !truckNode || response.assignment_state_id === undefined) return;
            truckNode.dataset.haulAssignmentStateId = String(response.assignment_state_id || 0);
        }

        function applyStates(response, root) {
            var states = response && response.assignment_state_ids;
            if (!states || !root) return;
            root.querySelectorAll("[data-equipment-id], [data-mm-mobile-home-truck-id]").forEach(function (node) {
                var truckId = node.dataset.equipmentId || node.dataset.mmMobileHomeTruckId || "";
                if (truckId && Object.prototype.hasOwnProperty.call(states, truckId)) {
                    node.dataset.haulAssignmentStateId = String(states[truckId] || 0);
                }
            });
        }

        return {
            getStateId: getStateId,
            collectComplexStates: collectComplexStates,
            applyState: applyState,
            applyStates: applyStates
        };
    }

    global.createDispatcherHaulAssignmentState = createDispatcherHaulAssignmentState;
})(window);
