package ru.copperresources.mobile;

import java.util.Locale;

/**
 * Привязка серверных точек разгрузки к проверенным записям водителя.
 *
 * ID используется первым: справочник сервера является источником истины.
 * Нормализованное имя остаётся запасным путём для новых точек и локальных
 * разговорных названий до их окончательной привязки в справочнике.
 */
public final class DriverVoiceCatalog {
    private DriverVoiceCatalog() {}

    public static String resourceNameFor(long dumpPointId, String dumpPointName) {
        switch ((int) dumpPointId) {
            case 1:
                return "voice_edem_na_kkd";
            case 2:
                return "voice_na_skdr";
            case 3:
                return "voice_na_otval";
            case 4:
                return "voice_na_sklad_negabaritov";
            case 5:
                return "voice_na_kisluhu";
            case 6:
                return "voice_na_podsypku";
            default:
                break;
        }

        String normalized = normalize(dumpPointName);
        if (normalized.equals("ккд")) {
            return "voice_edem_na_kkd";
        }
        if (normalized.equals("скдр")) {
            return "voice_na_skdr";
        }
        if (normalized.equals("отвал")) {
            return "voice_na_otval";
        }
        if (normalized.equals("свх")) {
            return "voice_edem_na_svh";
        }
        if (normalized.equals("кислуха") || normalized.equals("склад окисленной руды")) {
            return "voice_na_kisluhu";
        }
        if (normalized.equals("склад негабарита") || normalized.equals("склад негабаритов")) {
            return "voice_na_sklad_negabaritov";
        }
        if (normalized.equals("буферный склад")) {
            return "voice_na_bufernyi_sklad";
        }
        if (normalized.equals("подсыпка")) {
            return "voice_na_podsypku";
        }
        return "";
    }

    public static String displayNameFor(long dumpPointId, String dumpPointName) {
        String provided = cleanDisplayName(dumpPointName);
        if (!provided.isEmpty()) {
            return provided;
        }
        switch ((int) dumpPointId) {
            case 1:
                return "ККД";
            case 2:
                return "СКДР";
            case 3:
                return "Отвал";
            case 4:
                return "Склад негабарита";
            case 5:
                return "Склад окисленной руды";
            case 6:
                return "Подсыпка";
            default:
                return "";
        }
    }

    public static String fallbackPhrase(String dumpPointName) {
        String provided = cleanDisplayName(dumpPointName);
        return provided.isEmpty()
            ? "Назначена новая точка разгрузки"
            : "Точка разгрузки: " + provided;
    }

    static String normalize(String value) {
        if (value == null) {
            return "";
        }
        return value
            .trim()
            .toLowerCase(Locale.forLanguageTag("ru"))
            .replace('ё', 'е')
            .replaceAll("[^а-яa-z0-9]+", " ")
            .trim()
            .replaceAll("\\s+", " ");
    }

    private static String cleanDisplayName(String value) {
        return value == null ? "" : value.trim().replaceAll("\\s+", " ");
    }
}
