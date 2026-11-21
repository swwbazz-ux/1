package ru.copperresources.mobile;

import java.util.List;

/** A reconnect may select one explicitly enrolled, currently available display, once. */
final class TabletTvGuard {
    static final class Peer {
        final String address, name;
        final boolean available, connectable, remembered;
        Peer(String address, String name, boolean available, boolean connectable, boolean remembered) {
            this.address = address; this.name = name; this.available = available;
            this.connectable = connectable; this.remembered = remembered;
        }
    }
    private final String address;
    private final long deadline;
    private boolean consumed;
    TabletTvGuard(String address, long deadline) { this.address = address; this.deadline = deadline; }
    String label(List<Peer> peers, long now) {
        if (consumed || now >= deadline || address == null || address.isEmpty()) return null;
        Peer target = null; int sameAddress = 0;
        for (Peer peer : peers) if (address.equals(peer.address)) { target = peer; sameAddress++; }
        if (sameAddress != 1 || target == null || !target.available || !target.connectable
                || !target.remembered || target.name == null || target.name.trim().isEmpty()) return null;
        int names = 0;
        for (Peer peer : peers) if (target.name.equals(peer.name)) names++;
        return names == 1 ? target.name : null;
    }
    boolean consume(String label, int visibleMatches, List<Peer> peers, long now) {
        String expected = label(peers, now);
        if (visibleMatches != 1 || expected == null || !expected.equals(label)) return false;
        consumed = true; return true;
    }
}
