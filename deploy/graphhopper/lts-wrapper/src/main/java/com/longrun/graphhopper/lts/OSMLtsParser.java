package com.longrun.graphhopper.lts;

import com.graphhopper.reader.ReaderWay;
import com.graphhopper.routing.ev.EdgeIntAccess;
import com.graphhopper.routing.ev.IntEncodedValue;
import com.graphhopper.routing.util.parsers.TagParser;
import com.graphhopper.storage.IntsRef;

/**
 * Reads the synthetic OSM tag {@code lts=1..4} off a way and stores it in the {@code lts}
 * encoded value.
 *
 * <p>The tag is not real OSM. It is written into the {@code .pbf} before import by
 * {@code deploy/graphhopper/scripts/add_lts_tags.py}, which is fed from the offline LTS
 * computation (scope 7.1). GraphHopper never needs to know where the number came from.
 *
 * <p>A way with no {@code lts} tag, or an out-of-range one, is left at the encoded value's
 * default of 0. Custom models must therefore treat {@code lts == 0} as "unknown", not as
 * "better than 1".
 */
public final class OSMLtsParser implements TagParser {

    private final IntEncodedValue ltsEnc;

    public OSMLtsParser(IntEncodedValue ltsEnc) {
        this.ltsEnc = ltsEnc;
    }

    @Override
    public void handleWayTags(int edgeId, EdgeIntAccess edgeIntAccess, ReaderWay way, IntsRef relationFlags) {
        String raw = way.getTag(LtsImportRegistry.KEY);
        if (raw == null) return;
        int lts;
        try {
            lts = Integer.parseInt(raw.trim());
        } catch (NumberFormatException ignored) {
            return;
        }
        if (lts < LtsImportRegistry.MIN_LTS || lts > LtsImportRegistry.MAX_LTS) return;
        ltsEnc.setInt(false, edgeId, edgeIntAccess, lts);
    }
}
