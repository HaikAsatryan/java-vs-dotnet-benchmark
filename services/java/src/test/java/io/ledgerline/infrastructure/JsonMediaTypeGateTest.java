package io.ledgerline.infrastructure;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;

/**
 * The frozen grammar vector table, identical to the one
 * {@code services/dotnet/Ledgerline.Tests/ValidationBodyCaptureTests.cs} pins for
 * {@code MediaTypeGateMiddleware.IsJsonMediaType}. Both stacks accept and reject the same set by
 * running the same trivial rules, not by agreeing on a media-type parser.
 */
class JsonMediaTypeGateTest {

    @ParameterizedTest
    @CsvSource(nullValues = "NULL", value = {
        "application/json,            true",
        "application/json; charset=utf-8, true",
        "application/problem+json,    true",
        "APPLICATION/JSON,            true",
        "' application/json ',        true",
        // Parameters are never validated, so a valueless parameter is still accepted. Spring's own
        // MediaType parser raises InvalidMediaTypeException here, which is the reason this gate
        // runs before content negotiation rather than instead of nothing.
        "application/json; charset,   true",
        "+json,                       false",
        "text/json,                   false",
        "text/plain,                  false",
        "'',                          false",
        "NULL,                        false",
    })
    void acceptsExactlyTheFrozenSet(String contentType, boolean accepted) {
        assertThat(JsonMediaTypeGate.isJsonMediaType(contentType)).isEqualTo(accepted);
    }
}
