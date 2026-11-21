package ru.copperresources.mobile;

import android.os.Parcel;
import android.os.Parcelable;
import java.util.ArrayList;
import java.util.List;

/** Android 11's protected WifiDisplayStatus broadcast, read with public Parcelable APIs.
 * Schema: AOSP android11-release WifiDisplayStatus/WifiDisplay. Session credentials are never read.
 */
final class TabletTvStatus {
    final int feature, activeState;
    final TabletTvGuard.Peer active;
    final List<TabletTvGuard.Peer> peers;
    private TabletTvStatus(int feature, int state, TabletTvGuard.Peer active, List<TabletTvGuard.Peer> peers) {
        this.feature = feature; this.activeState = state; this.active = active; this.peers = peers;
    }
    static TabletTvStatus read(Parcelable value) {
        if (android.os.Build.VERSION.SDK_INT != 30 || value == null) return null;
        Parcel parcel = Parcel.obtain();
        try {
            value.writeToParcel(parcel, 0); parcel.setDataPosition(0);
            int feature = parcel.readInt(); parcel.readInt(); int state = parcel.readInt();
            if (feature < 0 || feature > 3 || state < 0 || state > 2) return null;
            TabletTvGuard.Peer active = parcel.readInt() == 1 ? peer(parcel) : null;
            int count = parcel.readInt(); if (count < 0 || count > 100) return null;
            List<TabletTvGuard.Peer> peers = new ArrayList<>();
            for (int i = 0; i < count; i++) peers.add(peer(parcel));
            // Deliberately stop here: the next parcel field contains Wi-Fi Direct session details.
            return new TabletTvStatus(feature, state, active, peers);
        } catch (RuntimeException invalid) {
            android.util.Log.w("MiningMasterTablet", "tv_route event=status_parse_failed type=" + invalid.getClass().getSimpleName());
            return null;
        }
        finally { parcel.recycle(); }
    }
    private static TabletTvGuard.Peer peer(Parcel parcel) {
        String address = parcel.readString(), name = parcel.readString(), alias = parcel.readString();
        if (address == null || !address.matches("(?i)[0-9a-f]{2}(:[0-9a-f]{2}){5}")) throw new IllegalArgumentException("display");
        return new TabletTvGuard.Peer(address, alias == null || alias.isEmpty() ? name : alias,
            parcel.readInt() != 0, parcel.readInt() != 0, parcel.readInt() != 0);
    }
}
