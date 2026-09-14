package com.longrun.graphhopper.lts;

import com.graphhopper.GraphHopper;
import com.graphhopper.application.GraphHopperServerConfiguration;
import io.dropwizard.core.cli.ConfiguredCommand;
import io.dropwizard.core.setup.Bootstrap;
import net.sourceforge.argparse4j.inf.Namespace;

/**
 * Drop-in replacement for GraphHopper's own {@code import} command, differing in exactly one
 * line: {@link GraphHopper#setImportRegistry} is called before {@code init}, so {@code lts}
 * is a name the importer recognises.
 *
 * <p>Reads the identical Dropwizard YAML the server reads, which matters: {@code
 * GraphHopper.load()} refuses a graph whose stored {@code profiles} string differs from the
 * configured one, so import and serve must be driven from the same file.
 */
public final class LtsImportCommand extends ConfiguredCommand<GraphHopperServerConfiguration> {

    public LtsImportCommand() {
        super("import", "builds the graphhopper graph, with the custom `lts` encoded value");
    }

    @Override
    protected void run(Bootstrap<GraphHopperServerConfiguration> bootstrap,
                       Namespace namespace,
                       GraphHopperServerConfiguration configuration) {
        GraphHopper graphHopper = new GraphHopper();
        graphHopper.setImportRegistry(new LtsImportRegistry());
        graphHopper.init(configuration.getGraphHopperConfiguration());
        graphHopper.importAndClose();
    }
}
