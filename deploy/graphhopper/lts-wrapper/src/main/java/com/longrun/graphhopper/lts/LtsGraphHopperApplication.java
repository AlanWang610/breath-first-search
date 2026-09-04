package com.longrun.graphhopper.lts;

import com.graphhopper.application.GraphHopperServerConfiguration;
import com.graphhopper.application.resources.RootResource;
import com.graphhopper.http.CORSFilter;
import com.graphhopper.http.GraphHopperBundle;
import io.dropwizard.core.Application;
import io.dropwizard.core.setup.Bootstrap;
import io.dropwizard.core.setup.Environment;
import jakarta.servlet.DispatcherType;

import java.util.EnumSet;

/**
 * Entry point for the LTS-aware GraphHopper.
 *
 * <pre>
 *   java -cp "lts-wrapper.jar;graphhopper-web-11.0.jar" \
 *        com.longrun.graphhopper.lts.LtsGraphHopperApplication import config.yml
 * </pre>
 *
 * <p>{@code GraphHopperBundle} is added purely for its {@code initialize()}, which installs
 * the Jackson modules {@code GraphHopperConfig} needs. Its {@code run()} -- the part that
 * hard-codes {@code new GraphHopperManaged(...)} and gives no seam for the registry -- only
 * fires for the {@code server} command.
 *
 * <p>The {@code server} command here is a convenience so one binary does both jobs. It is not
 * required: because {@code GraphHopper.load()} rebuilds the EncodingManager from the stored
 * graph and never consults an ImportRegistry, the stock {@code graphhopper-web} jar serves an
 * LTS graph unmodified. Only the import needs this wrapper.
 */
public final class LtsGraphHopperApplication extends Application<GraphHopperServerConfiguration> {

    public static void main(String[] args) throws Exception {
        new LtsGraphHopperApplication().run(args);
    }

    @Override
    public void initialize(Bootstrap<GraphHopperServerConfiguration> bootstrap) {
        bootstrap.addBundle(new GraphHopperBundle());
        bootstrap.addCommand(new LtsImportCommand());
    }

    @Override
    public void run(GraphHopperServerConfiguration configuration, Environment environment) {
        environment.jersey().register(new RootResource());
        environment.servlets().addFilter("cors", CORSFilter.class)
                .addMappingForUrlPatterns(EnumSet.allOf(DispatcherType.class), false, "*");
    }
}
