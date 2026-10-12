package io.ledgerline.json;

import static org.assertj.core.api.Assertions.assertThat;

import io.ledgerline.GoldenVectors;
import io.ledgerline.SpecPaths;
import io.ledgerline.dto.HealthResponse;
import io.ledgerline.dto.InvoiceItemResponse;
import io.ledgerline.dto.InvoiceListPageResponse;
import io.ledgerline.dto.InvoiceResponse;
import io.ledgerline.dto.InvoiceSummaryResponse;
import io.ledgerline.dto.PdfStubResponse;
import io.ledgerline.dto.QuoteLineResponse;
import io.ledgerline.dto.QuoteResponse;
import io.ledgerline.problem.Problem;
import io.ledgerline.web.MockWebTest;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.io.Reader;
import java.nio.file.Files;
import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.yaml.snakeyaml.Yaml;
import tools.jackson.databind.JacksonModule;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

/**
 * What the frozen wire actually looks like, asserted through the mapper the running service uses -
 * not through a hand-built one, because every property in {@code application.yaml} that shapes the
 * wire is applied by Boot's auto-configuration and a locally built mapper would assert nothing
 * about the deployed artifact.
 *
 * <p>The expected property names are read out of {@code spec/openapi.yaml} itself rather than
 * copied into this file, so a DTO that drifts from the frozen contract fails here and a spec edit
 * cannot go unnoticed. Same principle as {@code SqlParityTest} reading {@code spec/db-access.sql}.
 */
@MockWebTest
class WireContractTest {

    @Autowired
    private JsonMapper json;

    @Test
    void everyResponseTypeCarriesExactlyTheFrozenPropertyNames() {
        Map<String, Object> schemas = openapiSchemas();

        samples().forEach((schemaName, sample) -> {
            JsonNode body = json.valueToTree(sample);
            assertThat(names(body))
                    .as("%s: spec/openapi.yaml components.schemas.%s.properties", schemaName,
                            schemaName)
                    .containsExactlyInAnyOrderElementsOf(propertyNames(schemas, schemaName));
        });
    }

    @Test
    void oneGlobalNamingStrategyProducesEveryFrozenName() {
        // .NET spells each of these with [JsonPropertyName]; Java sets one SNAKE_CASE strategy and
        // nothing else, so the MECHANISM is asserted alongside the result: the same objects
        // through a stock mapper come out camelCase, which is what the frozen wire would carry if
        // the property were dropped and no per-member rename exists to fall back on. 39 names
        // across the eight response schemas, counted off the mapper rather than off the YAML.
        JsonMapper stock = JsonMapper.builder().build();
        List<String> emitted = new ArrayList<>();
        List<String> unstyled = new ArrayList<>();
        samples().values().forEach(sample -> {
            emitted.addAll(names(json.valueToTree(sample)));
            unstyled.addAll(names(stock.valueToTree(sample)));
        });

        assertThat(emitted).hasSize(39);
        // The splits that are not obvious, plus the ones a per-member rename would have to spell.
        assertThat(emitted).contains("unit_price_minor", "category_surcharge_minor", "page_size",
                "line_total_minor", "qty_discount_minor", "order_discount_minor",
                "subtotal_minor", "bytes_written", "invoice_id", "customer_id", "total_minor",
                "created_at");
        assertThat(unstyled).contains("unitPriceMinor", "categorySurchargeMinor", "pageSize");
        assertThat(emitted).doesNotContainAnyElementsOf(
                List.of("unitPriceMinor", "categorySurchargeMinor", "pageSize"));
    }

    @Test
    void recordDeclarationOrderIsWireOrder() {
        // Jackson 3 flips SORT_PROPERTIES_ALPHABETICALLY on but also sets
        // SORT_CREATOR_PROPERTIES_FIRST, and every record component is a creator property - so
        // declaration order survives. That is what makes this body byte-identical to the one
        // services/dotnet/Dtos/InvoiceDtos.cs emits rather than merely equal after normalize.py
        // sorts keys, which in turn is what makes the serialize-then-cache payloads of
        // spec/cache.md the same bytes on both sides.
        String body = json.writeValueAsString(new InvoiceResponse(1, 2, "draft", "USD", 300,
                Instant.parse("2026-06-06T00:00:00Z"),
                List.of(new InvoiceItemResponse(4, "EL-0001", "widget", 3, 100, 300))));

        assertThat(body).isEqualTo("{\"id\":1,\"customer_id\":2,\"status\":\"draft\","
                + "\"currency\":\"USD\",\"total_minor\":300,"
                + "\"created_at\":\"2026-06-06T00:00:00.000000Z\","
                + "\"items\":[{\"id\":4,\"sku\":\"EL-0001\",\"description\":\"widget\",\"qty\":3,"
                + "\"unit_price_minor\":100,\"line_total_minor\":300}]}");
    }

    @Test
    void moneyAndIdsAreBareJsonIntegersAtTheEdgeOfInt64() {
        // spec/openapi.yaml: "no floats in money paths"; normalize.py R5 rejects a money value
        // that is not an integer. WRITE_NUMBERS_AS_STRINGS would quote every one of these and
        // normalize.py would report it as a type mismatch, not as a JSON error.
        String body = json.writeValueAsString(new QuoteLineResponse("EL-0001", "EL", 1,
                Long.MAX_VALUE, Long.MIN_VALUE, 0, Long.MAX_VALUE));

        assertThat(body).contains("\"unit_price_minor\":9223372036854775807");
        assertThat(body).contains("\"category_surcharge_minor\":-9223372036854775808");
        assertThat(body).doesNotContain("\"9223372036854775807\"");
    }

    @Test
    void everyBodySatisfiesItsGoldenShapeDescriptor() {
        // bench/validation/golden/shapes.json, applied by the same rules shapecheck.py applies.
        assertThat(GoldenVectors.checkShape(json.valueToTree(invoice()), "Invoice")).isEmpty();
        assertThat(GoldenVectors.checkShape(json.valueToTree(listPage()), "InvoiceListPage"))
                .isEmpty();
        assertThat(GoldenVectors.checkShape(json.valueToTree(summary()), "InvoiceSummary"))
                .isEmpty();
        assertThat(GoldenVectors.checkShape(json.valueToTree(pdfStub()), "PdfStub")).isEmpty();
    }

    @Test
    void everyBodySurvivesTheCrossServiceNormalizer() {
        // normalize.py raises on a float money value and on a timestamp that is not exactly six
        // fractional digits with a Z, so running it here is the same gate the equivalence suite
        // applies - just without a live service on either side.
        samples().values().forEach(sample -> GoldenVectors.normalize(json.valueToTree(sample)));
    }

    @Test
    void healthzIsItsGoldenVectorByteForByte() {
        assertThat(json.writeValueAsString(HealthResponse.OK)).isEqualTo("{\"status\":\"ok\"}");
        assertThat(canonical(json.valueToTree(HealthResponse.OK)))
                .isEqualTo(canonical(GoldenVectors.load("healthz/ok.json")));
    }

    @Test
    void theQuoteWireVectorsRoundTripThroughTheResponseRecord() {
        // The committed 200 bodies from bench/validation/golden are read INTO the Java record and
        // written back out, so the property names, the array order, the integer typing and the
        // component order are all asserted against the vector the cross-service suite compares to.
        for (String vector : List.of("quote_a_wire.json", "quote_b_wire.json", "quote_c_wire.json")) {
            JsonNode golden = GoldenVectors.load("pricing_quote/" + vector);
            QuoteResponse parsed = json.treeToValue(golden, QuoteResponse.class);

            // Compared as canonical TEXT, not as trees: Jackson's JsonNode equality is
            // numeric-node-type sensitive, so a golden 1500 read as an IntNode would never equal
            // the LongNode a `long` component writes even though the two emit the same bytes -
            // and bytes are what the cross-service suite compares.
            assertThat(canonical(json.valueToTree(parsed)))
                    .as(vector)
                    .isEqualTo(canonical(golden));
        }
    }

    @Test
    void nullValuedPropertiesStayOnTheWire() {
        // spring.jackson.default-property-inclusion must stay unset. spec/runtime-stats.md
        // REQUIRES present-but-null fields (gogc, heap_goal_bytes) and shapecheck.py checks that
        // required keys are present, so a global non_null would delete them and fail the shape
        // gate - while every 4xx body would still look correct, because omitting `errors` is
        // handled by @JsonInclude on the Problem record alone.
        record Nullable(String left, Long right) {
        }

        assertThat(json.writeValueAsString(new Nullable(null, null)))
                .isEqualTo("{\"left\":null,\"right\":null}");
    }

    @Test
    void theMapperCarriesOnlyFrameworkModules() {
        // The artifact behind the docs/FAIRNESS.md JSON row: "no Blackbird" is asserted from the
        // resolved mapper, not from the POM and not from prose. find-and-add-modules defaults
        // true and Blackbird ships a ServiceLoader entry, so a one-line dependency add would
        // enable a bytecode-generating accelerator with no other change anywhere.
        //
        // Four modules: two from Boot, and two that spring-data-commons contributes through
        // SpringDataJackson3Configuration. Both of those are mixin/serializer registrations for
        // Spring Data types this service never puts on the wire (Point/Distance, and Page - the
        // repositories return Lists precisely so no Page is ever serialized). Neither generates
        // bytecode.
        List<String> modules = json.registeredModules().stream()
                .map(JacksonModule::getModuleName)
                .toList();

        assertThat(modules).containsExactlyInAnyOrder(
                "org.springframework.boot.jackson.JacksonComponentModule",
                "org.springframework.boot.jackson.JacksonMixinModule",
                "org.springframework.data.web.config.SpringDataJackson3Configuration$PageModule",
                "Spring Data Geo Mixins");
    }

    private static Map<String, Object> samples() {
        return new LinkedHashMap<>(Map.of(
                "Health", HealthResponse.OK,
                "InvoiceItem", invoice().items().getFirst(),
                "Invoice", invoice(),
                "InvoiceSummary", summary(),
                "InvoiceListPage", listPage(),
                "PdfStub", pdfStub(),
                "QuoteLine", quoteLine(),
                "QuoteResponse", new QuoteResponse("USD", List.of(quoteLine()), 300, 0, 300)));
    }

    private static InvoiceResponse invoice() {
        return new InvoiceResponse(1, 2, "draft", "USD", 300,
                Instant.parse("2026-06-06T00:00:00Z"),
                List.of(new InvoiceItemResponse(4, "EL-0001", "widget", 3, 100, 300)));
    }

    private static InvoiceSummaryResponse summary() {
        return new InvoiceSummaryResponse(1, 2, "draft", "USD", 300,
                Instant.parse("2026-06-06T00:00:00Z"));
    }

    private static InvoiceListPageResponse listPage() {
        return new InvoiceListPageResponse(2, 1, InvoiceListPageResponse.PAGE_SIZE,
                List.of(summary()));
    }

    private static PdfStubResponse pdfStub() {
        return new PdfStubResponse(1, 32768, "/data/pdf/1.pdfstub");
    }

    private static QuoteLineResponse quoteLine() {
        return new QuoteLineResponse("EL-0001", "EL", 3, 100, 0, 0, 300);
    }

    /** normalize.py's canonical form, rendered as the text the cross-service suite compares. */
    private String canonical(JsonNode body) {
        return json.writeValueAsString(GoldenVectors.normalize(body));
    }

    private static List<String> names(JsonNode object) {
        return new ArrayList<>(object.propertyNames());
    }

    @SuppressWarnings("unchecked")
    private static List<String> propertyNames(Map<String, Object> schemas, String schemaName) {
        Map<String, Object> schema = (Map<String, Object>) schemas.get(schemaName);
        Map<String, Object> properties = (Map<String, Object>) schema.get("properties");
        return new ArrayList<>(properties.keySet());
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> openapiSchemas() {
        try (Reader reader = Files.newBufferedReader(SpecPaths.openapiYaml())) {
            Map<String, Object> root = new Yaml().load(reader);
            Map<String, Object> components = (Map<String, Object>) root.get("components");
            return (Map<String, Object>) components.get("schemas");
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }
}
