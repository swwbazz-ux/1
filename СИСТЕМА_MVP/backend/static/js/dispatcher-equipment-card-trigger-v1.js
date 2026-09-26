/* Dispatcher equipment-card trigger.
   Owns only mouse and keyboard activation of an already rendered equipment card. */
(function (global) {
    "use strict";

    function createDispatcherEquipmentCardTrigger(options) {
        options = options || {};
        var openEquipmentCard = options.openEquipmentCard || function () { return false; };

        function bind(node) {
            if (!node || node.dataset.cardBound === "true") return;
            node.dataset.cardBound = "true";

            function isTapBlocked(event) {
                var control = event.target && event.target.closest
                    ? event.target.closest("button, a, input, form")
                    : null;
                return node.classList.contains("is-placeholder") || Boolean(control && control !== node);
            }

            function openBoundCard(event) {
                if (!openEquipmentCard(node.dataset.equipmentCardId, node)) return false;
                if (event) {
                    event.preventDefault();
                    event.stopPropagation();
                }
                return true;
            }

            node.addEventListener("click", function (event) {
                if (isTapBlocked(event)) return;
                if (node.dataset.complexTruck === "true") event.stopPropagation();
                openBoundCard(event);
            });
            node.addEventListener("keydown", function (event) {
                if (event.key !== "Enter" && event.key !== " ") return;
                if (openEquipmentCard(node.dataset.equipmentCardId, node)) {
                    event.preventDefault();
                }
            });
        }

        return {bind: bind};
    }

    global.createDispatcherEquipmentCardTrigger = createDispatcherEquipmentCardTrigger;
})(window);
