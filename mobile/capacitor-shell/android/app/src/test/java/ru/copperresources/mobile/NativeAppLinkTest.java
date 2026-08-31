package ru.copperresources.mobile;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;

import org.junit.Test;

public class NativeAppLinkTest {
    private static final String HOST = "driver.driverform.ru";
    private static final String PATH = "/native-handoff/open/";
    private static final String SERVER_URL = "https://driver.driverform.ru/";
    private static final String TOKEN = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";

    private static String resolve(String rawUrl) {
        return NativeAppLink.resolve(
            NativeAppLink.ACTION_VIEW,
            rawUrl,
            SERVER_URL,
            HOST,
            PATH
        );
    }

    @Test
    public void acceptsExactVerifiedLinkAndKeepsTokenInFragment() {
        assertEquals(
            SERVER_URL + "native-handoff/open/#token=" + TOKEN,
            resolve(SERVER_URL + "native-handoff/open/#token=" + TOKEN)
        );
    }

    @Test
    public void acceptsExplicitHttpsPortButCanonicalizesFromServerUrl() {
        assertEquals(
            SERVER_URL + "native-handoff/open/#token=" + TOKEN,
            resolve("https://driver.driverform.ru:443/native-handoff/open/#token=" + TOKEN)
        );
    }

    @Test
    public void acceptsUrlSafeTokenAlphabet() {
        String token = "abcdefghijklmnopqrstuvwxyz0123456789ABCDE-_";
        assertEquals(43, token.length());
        assertEquals(
            SERVER_URL + "native-handoff/open/#token=" + token,
            resolve(SERVER_URL + "native-handoff/open/#token=" + token)
        );
    }

    @Test
    public void rejectsNonViewAction() {
        assertNull(NativeAppLink.resolve(
            "android.intent.action.SEND",
            SERVER_URL + "native-handoff/open/#token=" + TOKEN,
            SERVER_URL,
            HOST,
            PATH
        ));
    }

    @Test
    public void rejectsInsecureOrForeignOrigin() {
        assertNull(resolve("http://driver.driverform.ru/native-handoff/open/#token=" + TOKEN));
        assertNull(resolve("https://driver.driverform.ru.evil.test/native-handoff/open/#token=" + TOKEN));
        assertNull(resolve("https://excavator.driverform.ru/native-handoff/open/#token=" + TOKEN));
        assertNull(resolve("https://driver.driverform.ru:444/native-handoff/open/#token=" + TOKEN));
        assertNull(resolve("https://user@driver.driverform.ru/native-handoff/open/#token=" + TOKEN));
    }

    @Test
    public void rejectsAnythingOutsideExactPath() {
        assertNull(resolve(SERVER_URL + "native-handoff/open#token=" + TOKEN));
        assertNull(resolve(SERVER_URL + "native-handoff/open/extra/#token=" + TOKEN));
        assertNull(resolve(SERVER_URL + "native-handoff/%6Fpen/#token=" + TOKEN));
    }

    @Test
    public void rejectsEveryQueryIncludingEmptyQuery() {
        assertNull(resolve(SERVER_URL + "native-handoff/open/?phone=79990000000#token=" + TOKEN));
        assertNull(resolve(SERVER_URL + "native-handoff/open/?next=https://evil.test/#token=" + TOKEN));
        assertNull(resolve(SERVER_URL + "native-handoff/open/?#token=" + TOKEN));
    }

    @Test
    public void rejectsMissingMalformedOrEncodedFragment() {
        assertNull(resolve(SERVER_URL + "native-handoff/open/"));
        assertNull(resolve(SERVER_URL + "native-handoff/open/#other=" + TOKEN));
        assertNull(resolve(SERVER_URL + "native-handoff/open/#token=" + TOKEN.substring(1)));
        assertNull(resolve(SERVER_URL + "native-handoff/open/#token=" + TOKEN + "A"));
        assertNull(resolve(SERVER_URL + "native-handoff/open/#token=" + TOKEN.substring(1) + "="));
        assertNull(resolve(SERVER_URL + "native-handoff/open/#token=%41" + TOKEN.substring(2)));
        assertNull(resolve(SERVER_URL + "native-handoff/open/#token=" + TOKEN + "&token=" + TOKEN));
    }

    @Test
    public void rejectsMalformedOrUntrustedServerConfiguration() {
        String incoming = SERVER_URL + "native-handoff/open/#token=" + TOKEN;
        assertNull(NativeAppLink.resolve(
            NativeAppLink.ACTION_VIEW,
            incoming,
            "http://driver.driverform.ru/",
            HOST,
            PATH
        ));
        assertNull(NativeAppLink.resolve(
            NativeAppLink.ACTION_VIEW,
            incoming,
            "https://driver.driverform.ru/base/",
            HOST,
            PATH
        ));
        assertNull(NativeAppLink.resolve(
            NativeAppLink.ACTION_VIEW,
            incoming,
            "https://driver.driverform.ru/?next=bad",
            HOST,
            PATH
        ));
    }
}
