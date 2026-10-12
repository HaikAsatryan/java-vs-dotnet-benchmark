package io.ledgerline.json;

import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.Locale;

/**
 * RFC3339 UTC with EXACTLY 6 fractional digits and a {@code Z} - the frozen format of
 * spec/openapi.yaml and spec/logging.md, and the regex {@code bench/validation/normalize.py}
 * enforces on every {@code created_at} and {@code ts}. {@code Instant.toString()} emits VARIABLE
 * precision and fails the golden vectors on any all-zero fraction, which is common in real seeded
 * data; only an explicit {@code SSSSSS} pattern pads and truncates unconditionally, in the same
 * direction as .NET's {@code ffffff}.
 */
public final class Rfc3339Micros {

    // Locale.ROOT because ofPattern otherwise takes its DecimalStyle from the default FORMAT
    // locale, and a non-ASCII zero digit would emit digits no regex in bench/validation matches.
    private static final DateTimeFormatter FORMATTER =
            DateTimeFormatter.ofPattern("uuuu-MM-dd'T'HH:mm:ss.SSSSSS'Z'", Locale.ROOT)
                    .withZone(ZoneOffset.UTC);

    private Rfc3339Micros() {
    }

    public static String format(Instant value) {
        return FORMATTER.format(value);
    }
}
