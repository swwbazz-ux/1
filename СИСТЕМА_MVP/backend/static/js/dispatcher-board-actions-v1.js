/* Dispatcher board response policy.
   Joins authoritative assignment tokens, local DOM mutations and fragment recovery.
   Transport and drag event binding remain outside this module. */
(function (global) {
    "use strict";

    function createDispatcherBoardActions(options) {
        options = options || {};
        var showDispatcherDnDError = options.showError || function () {};
        var refreshDispatcherDesktopBoardFromServer = options.refreshBoardFromServer || function () {
            return Promise.resolve(false);
        };
        var reloadDispatcherBoardAsFallback = options.reloadFallback || function () {};
        var dispatcherHaulAssignmentState = options.assignmentState || {};
        var applyHaulAssignmentState = typeof dispatcherHaulAssignmentState.applyState === "function"
            ? dispatcherHaulAssignmentState.applyState
            : function () {};
        var applyHaulAssignmentStates = typeof dispatcherHaulAssignmentState.applyStates === "function"
            ? dispatcherHaulAssignmentState.applyStates
            : function () {};
        var dispatcherMutations = options.mutations || {};
        var moveDesktopTruckToGarage = dispatcherMutations.moveTruckToGarage || function () { return false; };
        var moveDesktopTruckToComplex = dispatcherMutations.moveTruckToComplex || function () { return false; };
        var releaseDesktopComplexTrucks = dispatcherMutations.releaseComplexTrucks || function () { return false; };

        function refreshDesktopBoardAfterStructuralAction(response, localFallback) {
            if (response && response.queued) {
                if (typeof localFallback === "function") {
                    localFallback();
                }
                return response;
            }
            return refreshDispatcherDesktopBoardFromServer().then(function (applied) {
                if (!applied && typeof localFallback === "function") {
                    localFallback();
                }
                return response;
            }).catch(function (error) {
                if (typeof localFallback === "function") {
                    localFallback();
                } else {
                    throw error;
                }
                return response;
            });
        }

        function handleDesktopOptimisticBoardError(error) {
            showDispatcherDnDError(error);
            return refreshDispatcherDesktopBoardFromServer().catch(function () {
                /* Первичная ошибка уже показана. При отсутствии сети повторный fragment
                   не должен становиться необработанным Promise в drag-and-drop. */
                return null;
            });
        }

        function applyDesktopTruckAction(response, action) {
            if (response && response.queued) return response;
            if (!action || !action.type) return response;
            if (action.truckTile) applyHaulAssignmentState(response, action.truckTile);
            if (action.complexCard) applyHaulAssignmentStates(response, action.complexCard);
            var applied = false;
            if (action.type === "assign") {
                applied = moveDesktopTruckToComplex(action.truckTile, action.complexCard);
            } else if (action.type === "release") {
                applied = moveDesktopTruckToGarage(action.truckTile);
            } else if (action.type === "release_complex") {
                applied = releaseDesktopComplexTrucks(action.complexCard);
            }
            if (!applied) {
                refreshDispatcherDesktopBoardFromServer().catch(reloadDispatcherBoardAsFallback);
            }
            return response;
        }

        return {
            applyTruckAction: applyDesktopTruckAction,
            refreshAfterStructuralAction: refreshDesktopBoardAfterStructuralAction,
            handleOptimisticError: handleDesktopOptimisticBoardError
        };
    }

    global.createDispatcherBoardActions = createDispatcherBoardActions;
})(window);
