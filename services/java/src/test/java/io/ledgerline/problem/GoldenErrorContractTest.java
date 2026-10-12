package io.ledgerline.problem;

import static org.assertj.core.api.Assertions.assertThat;

import io.ledgerline.GoldenVectors;
import io.ledgerline.SpecPaths;
import java.io.IOException;
import java.io.Reader;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.yaml.snakeyaml.Yaml;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.PropertyNamingStrategies;
import tools.jackson.databind.json.JsonMapper;

/**
 * The frozen strings, checked against the two files that own them: the committed vectors under
 * {@code bench/validation/golden/errors/} and {@code spec/openapi.yaml} itself. A typo in any of
 * the seventeen envelope strings or twelve messages fails here rather than at G4, where the
 * symptom is a 400 body that differs from another 400 body by one character.
 *
 * <p>No Spring context: a Problem is a record and the envelope is a value, so the whole error
 * vocabulary is assertable as data.
 */
class GoldenErrorContractTest {

    private final JsonMapper json = JsonMapper.builder()
            .propertyNamingStrategy(PropertyNamingStrategies.SNAKE_CASE)
            .build();

    @Test
    void theTwelveValidationMessagesAreTheGoldenOnes() {
        Map<String, String> golden = new LinkedHashMap<>();
        GoldenVectors.load("errors/validation_messages.json").properties().forEach(entry -> {
            if (!entry.getKey().startsWith("_")) {
                golden.put(entry.getKey(), entry.getValue().stringValue());
            }
        });

        assertThat(messagesByGoldenKey()).containsExactlyInAnyOrderEntriesOf(golden);
    }

    @Test
    void theThreeCommittedEnvelopesSerializeByteForByte() {
        assertEnvelope(Problem.validation(null), "errors/validation_envelope.json");
        assertEnvelope(Problem.notFound(), "errors/not_found.json");
        assertEnvelope(Problem.internal(), "errors/internal.json");
    }

    @Test
    void allFiveEnvelopesAreTheFrozenSpecStrings() {
        // 413 and 415 have no committed vector, so they are checked against x-error-strings in
        // spec/openapi.yaml directly - the file the vectors themselves were derived from.
        Map<String, Map<String, Object>> frozen = specErrorStrings();

        assertEnvelopeMatchesSpec(Problem.validation(null), frozen.get("validation"));
        assertEnvelopeMatchesSpec(Problem.notFound(), frozen.get("not_found"));
        assertEnvelopeMatchesSpec(Problem.internal(), frozen.get("internal"));
        assertEnvelopeMatchesSpec(Problem.unsupportedMediaType(),
                frozen.get("unsupported_media_type"));
        assertEnvelopeMatchesSpec(Problem.payloadTooLarge(), frozen.get("payload_too_large"));
    }

    @Test
    void errorsIsOmittedWhenAbsentAndOrderedLikeTheDotnetWriter() {
        // @JsonInclude(NON_NULL) on the record only. As a global inclusion it would also delete
        // the present-but-null fields spec/runtime-stats.md requires, and shapecheck.py checks
        // required keys are PRESENT.
        assertThat(json.writeValueAsString(Problem.notFound()))
                .isEqualTo("{\"type\":\"https://ledgerline.test/problems/not-found\","
                        + "\"title\":\"Not found\",\"status\":404,"
                        + "\"detail\":\"The requested resource does not exist.\"}");

        // Component order is the write order of ProblemWriter.cs, so the two 400 bodies are the
        // same bytes and not merely equal after normalize.py sorts keys.
        assertThat(json.writeValueAsString(
                Problem.validation(Map.of("id", List.of(ValidationStrings.ID_POSITIVE)))))
                .isEqualTo("{\"type\":\"https://ledgerline.test/problems/validation\","
                        + "\"title\":\"Validation failed\",\"status\":400,"
                        + "\"detail\":\"The request payload failed validation.\","
                        + "\"errors\":{\"id\":[\"must be a positive integer\"]}}");
    }

    @Test
    void everyStatusWithoutItsOwnEnvelopeFallsBackToTheInternalOne() {
        // The default arm of ProblemDetailsConfig.cs's switch, and the reason a framework 405 or
        // 500 cannot put a framework-shaped body on the wire.
        assertThat(Problem.forStatus(500).type()).isEqualTo(ProblemStrings.INTERNAL_TYPE);
        assertThat(Problem.forStatus(405).type()).isEqualTo(ProblemStrings.INTERNAL_TYPE);
        assertThat(Problem.forStatus(405).status()).isEqualTo(405);
        assertThat(Problem.forStatus(400).errors()).isNull();
    }

    private void assertEnvelope(Problem problem, String goldenPath) {
        assertThat(canonical(json.valueToTree(problem)))
                .as(goldenPath)
                .isEqualTo(canonical(GoldenVectors.load(goldenPath)));
    }

    private String canonical(JsonNode node) {
        return json.writeValueAsString(GoldenVectors.normalize(node));
    }

    private static void assertEnvelopeMatchesSpec(Problem problem, Map<String, Object> frozen) {
        assertThat(problem.status()).isEqualTo(frozen.get("status"));
        assertThat(problem.type()).isEqualTo(frozen.get("type"));
        assertThat(problem.title()).isEqualTo(frozen.get("title"));
        assertThat(problem.detail()).isEqualTo(frozen.get("detail"));
    }

    private static Map<String, String> messagesByGoldenKey() {
        Map<String, String> messages = new LinkedHashMap<>();
        messages.put("required", ValidationStrings.REQUIRED);
        messages.put("sku_pattern", ValidationStrings.SKU_PATTERN);
        messages.put("currency_pattern", ValidationStrings.CURRENCY_PATTERN);
        messages.put("status_enum", ValidationStrings.STATUS_ENUM);
        messages.put("qty_range", ValidationStrings.QTY_RANGE);
        messages.put("unit_price_range", ValidationStrings.UNIT_PRICE_RANGE);
        messages.put("items_create_count", ValidationStrings.ITEMS_CREATE_COUNT);
        messages.put("items_quote_count", ValidationStrings.ITEMS_QUOTE_COUNT);
        messages.put("description_length", ValidationStrings.DESCRIPTION_LENGTH);
        messages.put("page_min", ValidationStrings.PAGE_MIN);
        messages.put("page_max", ValidationStrings.PAGE_MAX);
        messages.put("id_positive", ValidationStrings.ID_POSITIVE);
        return messages;
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Map<String, Object>> specErrorStrings() {
        try (Reader reader = Files.newBufferedReader(SpecPaths.openapiYaml())) {
            Map<String, Object> root = new Yaml().load(reader);
            return (Map<String, Map<String, Object>>) root.get("x-error-strings");
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }
}
