package ru.copperresources.mobile;

import java.net.URI;
import java.net.URISyntaxException;

/** Recognizes the real standalone rating shell; never changes DOM contract attributes. */
final class TabletStandaloneReadiness {
    private static final String RATING_PATH = "/reports/rating/tv/";
    private static final String RATING_EXPRESSION =
        "(function(){"
        + "if(window.top!==window||location.origin!=='https://mining-master.driverform.ru'"
        + "||location.pathname!=='/reports/rating/tv/'||!document.body"
        + "||!document.body.classList.contains('rating-tv-body'))return false;"
        + "var shell=document.querySelector('main[data-rating-tv]');"
        + "var runtime=window.RatingTvScreen;"
        + "if(!shell||shell.hidden||shell.getAttribute('aria-hidden')==='true'"
        + "||!runtime||typeof runtime.layoutGrid!=='function'||typeof runtime.loadRating!=='function'"
        + "||!runtime.state||(shell.dataset.ratingLayout!=='four'&&shell.dataset.ratingLayout!=='three-reserve'))return false;"
        + "var style=getComputedStyle(shell);var rect=shell.getBoundingClientRect();"
        + "return style.display!=='none'&&style.visibility!=='hidden'&&shell.getClientRects().length>0"
        + "&&Number.isFinite(rect.width)&&Number.isFinite(rect.height)&&rect.width>0&&rect.height>0;"
        + "})()";

    static String expressionFor(String profileId, String pageUrl) {
        if (!"mining_master_tablet".equals(profileId) || !TabletUrlPolicy.trusted(pageUrl)) return "";
        try {
            return RATING_PATH.equals(new URI(pageUrl).getRawPath()) ? RATING_EXPRESSION : "";
        } catch (URISyntaxException ignored) { return ""; }
    }
}
