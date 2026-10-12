package io.ledgerline.json;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.Instant;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.Locale;
import java.util.TimeZone;
import java.util.regex.Pattern;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;

/** The counterpart of {@code services/dotnet/Ledgerline.Tests/TimestampTests.cs}. */
class Rfc3339MicrosTest {

    /** The exact regex {@code bench/validation/normalize.py} gates every timestamp with. */
    private static final Pattern CONTRACT =
            Pattern.compile("^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{6}Z$");

    @ParameterizedTest
    @CsvSource({
        // The all-zero fraction is the case Instant.toString() drops entirely, and it is common
        // in the seeded data rather than exotic: a microsecond timestamptz whose fraction ends in
        // zeros is one row in a thousand.
        "2026-06-06T00:00:00Z,        2026-06-06T00:00:00.000000Z",
        "2026-06-06T12:34:56.1Z,      2026-06-06T12:34:56.100000Z",
        "2026-06-06T12:34:56.123456Z, 2026-06-06T12:34:56.123456Z",
        // Sub-microsecond input TRUNCATES, matching .NET's ffffff. A rounding formatter would
        // disagree with the other stack on the last microsecond of a second.
        "2026-06-06T12:34:56.123456789Z, 2026-06-06T12:34:56.123456Z",
        "2026-06-06T23:59:59.999999999Z, 2026-06-06T23:59:59.999999Z",
    })
    void formatsTheFrozenShape(String input, String expected) {
        String formatted = Rfc3339Micros.format(Instant.parse(input));

        assertThat(formatted).isEqualTo(expected);
        assertThat(formatted).matches(CONTRACT);
        // Fixed width: normalize.py's regex anchors on exactly six fractional digits.
        assertThat(formatted).hasSize(27);
    }

    @Test
    void isIndependentOfTheDefaultZoneAndLocale() {
        // The default zone is pinned in main() and the default locale is not pinned at all, so
        // neither may reach the output. ofPattern takes its DecimalStyle from the default FORMAT
        // locale, and a locale whose zero digit is not ASCII '0' would emit digits no regex in
        // bench/validation matches.
        TimeZone zone = TimeZone.getDefault();
        Locale locale = Locale.getDefault();
        try {
            TimeZone.setDefault(TimeZone.getTimeZone("Pacific/Kiritimati"));
            Locale.setDefault(Locale.forLanguageTag("th-TH-u-nu-thai"));

            assertThat(Rfc3339Micros.format(Instant.parse("2026-06-06T00:00:00Z")))
                    .isEqualTo("2026-06-06T00:00:00.000000Z");
        } finally {
            TimeZone.setDefault(zone);
            Locale.setDefault(locale);
        }
    }

    @Test
    void convertsAnOffsetToUtcRatherThanRelabellingIt() {
        Instant value = LocalDateTime.of(2026, 6, 6, 2, 0, 0)
                .toInstant(ZoneOffset.ofHours(2));

        assertThat(Rfc3339Micros.format(value)).isEqualTo("2026-06-06T00:00:00.000000Z");
    }

    @Test
    void theJdkDefaultsThisClassExistsToDefeatDoNotSatisfyTheContract() {
        // Both of these are the obvious thing to write in place of the formatter, and both fail
        // the golden vectors on the first all-zero fraction.
        Instant value = Instant.parse("2026-06-06T00:00:00Z");

        assertThat(value.toString()).doesNotMatch(CONTRACT.pattern());
        assertThat(value.atOffset(ZoneOffset.UTC).toString()).doesNotMatch(CONTRACT.pattern());
    }
}
