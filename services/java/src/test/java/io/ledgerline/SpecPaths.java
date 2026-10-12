package io.ledgerline;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * Locates the repo's {@code spec/} directory by walking up from the test classes directory,
 * mirroring {@code services/dotnet/Ledgerline.Tests/SpecPaths.cs}. The SQL-parity assertions
 * read the frozen files themselves rather than copies, so a spec edit fails a test instead of
 * going unnoticed.
 */
public final class SpecPaths {

    public static final Path SPEC_DIR = findSpecDir();

    private SpecPaths() {
    }

    public static Path dbAccessSql() {
        return SPEC_DIR.resolve("db-access.sql");
    }

    public static Path schemaSql() {
        return SPEC_DIR.resolve("schema.sql");
    }

    public static Path openapiYaml() {
        return SPEC_DIR.resolve("openapi.yaml");
    }

    public static String read(Path file) {
        try {
            return Files.readString(file);
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }

    private static Path findSpecDir() {
        Path dir = Path.of("").toAbsolutePath();
        while (dir != null) {
            Path candidate = dir.resolve("spec");
            if (Files.exists(candidate.resolve("db-access.sql"))) {
                return candidate;
            }
            dir = dir.getParent();
        }
        throw new IllegalStateException("could not locate spec/ above " + Path.of("").toAbsolutePath());
    }
}
