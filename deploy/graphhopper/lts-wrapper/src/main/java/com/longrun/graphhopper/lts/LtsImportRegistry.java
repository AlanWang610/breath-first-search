package com.longrun.graphhopper.lts;

import com.graphhopper.routing.ev.DefaultImportRegistry;
import com.graphhopper.routing.ev.ImportRegistry;
import com.graphhopper.routing.ev.ImportUnit;
import com.graphhopper.routing.ev.IntEncodedValueImpl;

/**
 * An {@link ImportRegistry} that knows one extra encoded value, {@code lts}, and delegates
 * every other name to {@link DefaultImportRegistry}.
 *
 * <p>Two things make this work without forking GraphHopper:
 *
 * <ol>
 *   <li>{@code ImportRegistry} is a single-method interface and
 *       {@code GraphHopper.setImportRegistry} is public, so the registry is replaceable from
 *       outside. It is consulted only by {@code GraphHopper.prepareImport()}, i.e. only on the
 *       import path.
 *   <li>The encoded value is a stock {@link IntEncodedValueImpl}, not a custom subclass. The
 *       built graph serialises it into {@code properties} and
 *       {@code EncodingManager.fromProperties} reconstructs it on load with no registry
 *       involved -- so an unmodified {@code graphhopper-web} jar can serve a graph that
 *       contains {@code lts}.
 * </ol>
 *
 * <p>3 bits store 0..7. 0 means "no lts tag on this way"; 1..4 are the Furth levels.
 */
public final class LtsImportRegistry implements ImportRegistry {

    public static final String KEY = "lts";
    public static final int MIN_LTS = 1;
    public static final int MAX_LTS = 4;
    private static final int BITS = 3;

    private final ImportRegistry delegate;

    public LtsImportRegistry() {
        this(new DefaultImportRegistry());
    }

    public LtsImportRegistry(ImportRegistry delegate) {
        this.delegate = delegate;
    }

    @Override
    public ImportUnit createImportUnit(String name) {
        if (KEY.equals(name)) {
            return ImportUnit.create(
                    KEY,
                    props -> new IntEncodedValueImpl(KEY, BITS, false),
                    (lookup, props) -> new OSMLtsParser(lookup.getIntEncodedValue(KEY)));
        }
        return delegate.createImportUnit(name);
    }
}
