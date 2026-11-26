window.settlementPersonnelNumber = function (number, {label = true} = {}) {
    const value = String(number || "").trim();
    const known = value && !/^не указан[оа]?$/i.test(value) && value !== "—";
    const badge = document.createElement("span");
    badge.className = known ? "settlement-personnel-number" : "settlement-personnel-number is-missing";
    badge.textContent = (label ? "Таб. № " : "") + (known ? value : (label ? "не указан" : "Не указан"));
    return badge;
};
