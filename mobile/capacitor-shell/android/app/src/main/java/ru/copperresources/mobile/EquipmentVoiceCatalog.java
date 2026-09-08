package ru.copperresources.mobile;

import java.util.Locale;

/** Сопоставляет фактические гаражные номера с записанными голосовыми ресурсами. */
public final class EquipmentVoiceCatalog {
    private EquipmentVoiceCatalog() {}

    public static String driverExcavatorAssignmentVoice(String garageNumber) {
        String normalized = normalizedNumber(garageNumber);
        switch (normalized) {
            case "1":
            case "2":
            case "3":
            case "4":
            case "5":
            case "6":
            case "7":
            case "8":
            case "9":
            case "99":
            case "528":
            case "530":
                return "voice_excavator_assignment_" + normalized;
            case "ТВИ4":
                return "voice_excavator_assignment_tvi_4";
            case "ТЕСТЭ99":
                return "voice_excavator_assignment_test_e99";
            default:
                return "";
        }
    }

    public static String truckNumberVoice(String garageNumber) {
        String normalized = normalizedNumber(garageNumber);
        if ("ТЕСТ1".equals(normalized)) {
            return "voice_truck_number_test_1";
        }
        try {
            int number = Integer.parseInt(normalized);
            if ((number >= 10 && number <= 52) || (number >= 54 && number <= 63)) {
                return "voice_truck_number_" + number;
            }
        } catch (NumberFormatException ignored) {}
        return "";
    }

    public static String truckSentDestinationVoice(long dumpPointId, String dumpPointName) {
        switch ((int) dumpPointId) {
            case 1:
                return "voice_truck_sent_kkd";
            case 2:
                return "voice_truck_sent_skdr";
            case 3:
                return "voice_truck_sent_otval";
            case 4:
                return "voice_truck_sent_sklad_negabarita";
            case 5:
                return "voice_truck_sent_sklad_okislennoy_rudy";
            case 6:
                return "voice_truck_sent_podsypka";
            case 10:
                return "voice_truck_sent_bufernyi_sklad";
            case 11:
                return "voice_truck_sent_svh";
            default:
                break;
        }
        String normalized = normalizeWords(dumpPointName);
        if (normalized.equals("ккд")) return "voice_truck_sent_kkd";
        if (normalized.equals("скдр")) return "voice_truck_sent_skdr";
        if (normalized.equals("свх")) return "voice_truck_sent_svh";
        if (normalized.contains("буфер") && normalized.contains("склад")) {
            return "voice_truck_sent_bufernyi_sklad";
        }
        if (normalized.contains("негабар")) return "voice_truck_sent_sklad_negabarita";
        if (normalized.contains("окислен")) return "voice_truck_sent_sklad_okislennoy_rudy";
        if (normalized.contains("подсып")) return "voice_truck_sent_podsypka";
        if (normalized.contains("отвал")) return "voice_truck_sent_otval";
        return "";
    }

    private static String normalizedNumber(String value) {
        return String.valueOf(value == null ? "" : value)
            .trim()
            .toUpperCase(Locale.ROOT)
            .replaceAll("[^0-9A-ZА-ЯЁ]", "");
    }

    private static String normalizeWords(String value) {
        return String.valueOf(value == null ? "" : value)
            .trim()
            .toLowerCase(Locale.ROOT)
            .replace('ё', 'е');
    }
}
