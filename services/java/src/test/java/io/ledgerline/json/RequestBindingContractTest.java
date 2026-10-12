package io.ledgerline.json;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;

import io.ledgerline.GoldenVectors;
import io.ledgerline.SpecPaths;
import io.ledgerline.dto.CreateInvoiceItem;
import io.ledgerline.dto.CreateInvoiceRequest;
import io.ledgerline.dto.QuoteItem;
import io.ledgerline.dto.QuoteRequest;
import io.ledgerline.web.MockWebTest;
import jakarta.validation.Validator;
import java.io.IOException;
import java.io.Reader;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.yaml.snakeyaml.Yaml;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

/**
 * The binding half of the wire contract. The response half is asserted in
 * {@link WireContractTest}; this covers what the service must ACCEPT, which is the half no
 * golden vector compares directly.
 */
@MockWebTest
class RequestBindingContractTest {

    @Autowired
    private JsonMapper json;

    @Autowired
    private Validator validator;

    @Autowired
    private MockMvc mvc;

    @Test
    void requestTypesCarryExactlyTheFrozenPropertyNames() {
        // The naming strategy is global, so the names a request record BINDS are the names it
        // writes. Asserting the written names against spec/openapi.yaml therefore pins the
        // accepted keys without a fixture that could drift from the contract.
        assertThat(names(json.valueToTree(createRequest())))
                .containsExactlyInAnyOrderElementsOf(specProperties("CreateInvoiceRequest"));
        assertThat(names(json.valueToTree(createRequest().items().getFirst())))
                .containsExactlyInAnyOrderElementsOf(specProperties("CreateInvoiceItem"));
        assertThat(names(json.valueToTree(quoteRequest())))
                .containsExactlyInAnyOrderElementsOf(specProperties("QuoteRequest"));
        assertThat(names(json.valueToTree(quoteRequest().items().getFirst())))
                .containsExactlyInAnyOrderElementsOf(specProperties("QuoteItem"));
    }

    @Test
    void theGoldenQuoteRequestsBindAndValidateClean() {
        for (String vector : List.of("quote_a_wire_request.json", "quote_b_wire_request.json",
                "quote_c_wire_request.json")) {
            JsonNode golden = GoldenVectors.load("pricing_quote/requests/" + vector);
            QuoteRequest request = json.treeToValue(golden, QuoteRequest.class);

            assertThat(validator.validate(request)).as(vector).isEmpty();
            assertThat(request.items()).as(vector).hasSize(50);
            assertThat(request.currency()).isEqualTo(golden.get("currency").stringValue());
            assertThat(request.items().getFirst().sku())
                    .isEqualTo(golden.get("items").get(0).get("sku").stringValue());
            assertThat(request.items().getFirst().unitPriceMinor())
                    .isEqualTo(golden.get("items").get(0).get("unit_price_minor").longValue());
        }
    }

    @Test
    void anAbsentUnitPriceBindsZeroRatherThanNull() {
        // .NET binds the CLR default into a non-nullable long. Coalescing in the canonical
        // constructor is what makes the two stacks STORE the same row, not merely return the same
        // status: without it the create path would have to decide what null means, and it would
        // decide it somewhere the error contract cannot see.
        QuoteItem item = json.readValue("{\"sku\":\"EL-0001\",\"qty\":1}", QuoteItem.class);

        assertThat(item.unitPriceMinor()).isZero();
        assertThat(validator.validate(item)).isEmpty();
    }

    @Test
    void unknownPropertiesAreIgnored() {
        // spec/openapi.yaml: "Unknown JSON fields in requests: IGNORED both sides". Already false
        // by default in Jackson 3, so nothing configures it - this asserts the default holds
        // rather than adding a property that would read as a tuning decision.
        QuoteItem item = json.readValue(
                "{\"sku\":\"EL-0001\",\"qty\":1,\"unit_price_minor\":5,\"nope\":true}",
                QuoteItem.class);

        assertThat(item.sku()).isEqualTo("EL-0001");
    }

    @Test
    void aResponseBodyOverHttpCarriesTheSixDigitTimestamp() {
        // Through the real converter and the real mapper, not through a unit-level serializer
        // call: the failure mode critique E2 describes is a formatter that is correct in a unit
        // test and never reaches the MVC write path.
        String body = exchange("""
                {"customer_id":7,"currency":"USD","status":"draft",
                 "items":[{"sku":"EL-0001","description":"ok","qty":1,"unit_price_minor":1},
                          {"sku":"EL-0001","description":"ok","qty":1,"unit_price_minor":1},
                          {"sku":"EL-0001","description":"ok","qty":1,"unit_price_minor":1}]}""");

        assertThat(body).contains("\"created_at\":\"1970-01-01T00:00:00.000000Z\"");
        GoldenVectors.normalize(json.readTree(body));
    }

    private String exchange(String requestBody) {
        try {
            return mvc.perform(post("/probe/invoices")
                            .contentType(MediaType.APPLICATION_JSON).content(requestBody))
                    .andReturn().getResponse().getContentAsString(StandardCharsets.UTF_8);
        } catch (Exception e) {
            throw new AssertionError(e);
        }
    }

    private static CreateInvoiceRequest createRequest() {
        return new CreateInvoiceRequest(1L, "USD", "draft",
                List.of(new CreateInvoiceItem("EL-0001", "widget", 3, 100L)));
    }

    private static QuoteRequest quoteRequest() {
        return new QuoteRequest("USD", List.of(new QuoteItem("EL-0001", 3, 100L)));
    }

    private static List<String> names(JsonNode object) {
        return new ArrayList<>(object.propertyNames());
    }

    @SuppressWarnings("unchecked")
    private static List<String> specProperties(String schemaName) {
        try (Reader reader = Files.newBufferedReader(SpecPaths.openapiYaml())) {
            Map<String, Object> root = new Yaml().load(reader);
            Map<String, Object> components = (Map<String, Object>) root.get("components");
            Map<String, Object> schemas = (Map<String, Object>) components.get("schemas");
            Map<String, Object> schema = (Map<String, Object>) schemas.get(schemaName);
            return new ArrayList<>(((Map<String, Object>) schema.get("properties")).keySet());
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }
}
