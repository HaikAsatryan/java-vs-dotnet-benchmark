package io.ledgerline.problem;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;

import io.ledgerline.web.MockWebTest;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.MediaType;
import org.springframework.mock.web.MockHttpServletResponse;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

/**
 * The frozen error contract, asserted against the bodies the .NET SUT actually emits. Every
 * expectation below was captured on 2026-08-23 by replaying the same payloads through
 * {@code services/dotnet} via WebApplicationFactory, not derived from reading its attributes -
 * the same method, and the same reason, as {@code ValidationBodyCaptureTests.cs}.
 *
 * <p>These are the assertions that would otherwise fail at G4 against a live pair of services,
 * where the diff is a 400 body against another 400 body with no annotation in sight.
 */
@MockWebTest
class ErrorContractTest {

    @Autowired
    private MockMvc mvc;

    @Autowired
    private JsonMapper json;

    @Test
    void emptyStringsEmitOnlyTheRequiredMessage() {
        // .NET: Validator tests [Required] first and RETURNS on failure, so a member that fails
        // presence carries exactly one message. Jakarta has no such rule and @Pattern/@Size fire
        // on "" as well, which is what LedgerExceptionHandler's presence-abort removes.
        JsonNode errors = errorsOf(jsonPost("/probe/invoices", """
                {"customer_id":5,"currency":"","status":"",
                 "items":[{"sku":"","description":"","qty":1,"unit_price_minor":1},
                          {"sku":"EL-0001","description":"ok","qty":1,"unit_price_minor":1},
                          {"sku":"EL-0001","description":"ok","qty":1,"unit_price_minor":1}]}"""));

        assertThat(keys(errors)).containsExactlyInAnyOrder(
                "Currency", "Status", "Items[0].Sku", "Items[0].Description");
        assertThat(errors.get("Currency").get(0).stringValue()).isEqualTo("is required");
        assertThat(errors.get("Items[0].Description").get(0).stringValue()).isEqualTo("is required");
    }

    @Test
    void whitespaceOnlyStringsEmitOnlyTheRequiredMessage() {
        // [Required] defaults to AllowEmptyStrings=false and rejects "   " too, which is why the
        // Java side uses @NotBlank rather than @NotNull. Captured identical to the "" case.
        JsonNode errors = errorsOf(jsonPost("/probe/invoices", """
                {"customer_id":5,"currency":"   ","status":"   ",
                 "items":[{"sku":"   ","description":"   ","qty":1,"unit_price_minor":1},
                          {"sku":"EL-0001","description":"ok","qty":1,"unit_price_minor":1},
                          {"sku":"EL-0001","description":"ok","qty":1,"unit_price_minor":1}]}"""));

        assertThat(keys(errors)).containsExactlyInAnyOrder(
                "Currency", "Status", "Items[0].Sku", "Items[0].Description");
    }

    @Test
    void absentFieldsCarryTheRangeMessageNotTheRequiredMessage() {
        // The trap this DTO shape exists for: an absent customer_id binds the CLR default 0 and
        // fails [Range(1, long.MaxValue)], so the frozen message is the RANGE one.
        JsonNode errors = errorsOf(jsonPost("/probe/invoices", "{}"));

        assertThat(keys(errors)).containsExactlyInAnyOrder(
                "CustomerId", "Currency", "Status", "Items");
        assertThat(errors.get("CustomerId").get(0).stringValue())
                .isEqualTo("must be a positive integer");
        assertThat(errors.get("Items").get(0).stringValue()).isEqualTo("is required");
    }

    @Test
    void absentQtyFailsAndAbsentUnitPriceDoesNot() {
        // unit_price_minor absent binds 0 on the .NET side and 0 passes [Range(0, 1e8)], so the
        // captured body carries NO entry for it. qty absent binds 0 and fails [Range(1,1000)].
        JsonNode errors = errorsOf(jsonPost("/probe/invoices", """
                {"customer_id":5,"currency":"USD","status":"draft",
                 "items":[{"sku":"EL-0001","description":"ok"},
                          {"sku":"EL-0001","description":"ok","qty":1,"unit_price_minor":1},
                          {"sku":"EL-0001","description":"ok","qty":1,"unit_price_minor":1}]}"""));

        assertThat(keys(errors)).containsExactly("Items[0].Qty");
        assertThat(errors.get("Items[0].Qty").get(0).stringValue())
                .isEqualTo("must be between 1 and 1000");
    }

    @Test
    void absentUnitPriceBindsZero() {
        // The VALUE, read back off the 200 body, not just the status: .NET binds the CLR default
        // into a non-nullable long, so what makes the two stacks store the same row is the
        // coalescing constructor, and it has to survive the MVC binding path and not only a
        // direct readValue (which is what RequestBindingContractTest covers).
        MockHttpServletResponse response = exchange(
                post("/probe/invoices").contentType(MediaType.APPLICATION_JSON).content("""
                        {"customer_id":5,"currency":"USD","status":"draft",
                         "items":[{"sku":"EL-0001","description":"ok","qty":1},
                                  {"sku":"EL-0001","description":"ok","qty":1},
                                  {"sku":"EL-0001","description":"ok","qty":1}]}"""));

        assertThat(response.getStatus()).isEqualTo(200);
        JsonNode items = parse(response).get("items");
        assertThat(items).hasSize(3);
        items.forEach(item ->
                assertThat(item.get("unit_price_minor").longValue()).isZero());
    }

    @Test
    void outOfRangeValuesCarryTheFrozenRangeMessages() {
        JsonNode errors = errorsOf(jsonPost("/probe/invoices", """
                {"customer_id":-1,"currency":"usdx","status":"closed",
                 "items":[{"sku":"EL-0001","description":"%s","qty":1001,"unit_price_minor":100000001},
                          {"sku":"EL-0001","description":"ok","qty":1,"unit_price_minor":1},
                          {"sku":"EL-0001","description":"ok","qty":1,"unit_price_minor":1}]}"""
                .formatted("x".repeat(201))));

        Map<String, String> expected = new LinkedHashMap<>();
        expected.put("CustomerId", "must be a positive integer");
        expected.put("Currency", "must match ^[A-Z]{3}$");
        expected.put("Status", "must be one of: draft, open");
        expected.put("Items[0].Description", "must be between 1 and 200 characters");
        expected.put("Items[0].Qty", "must be between 1 and 1000");
        expected.put("Items[0].UnitPriceMinor", "must be between 0 and 100000000");
        assertThat(flatten(errors)).containsExactlyInAnyOrderEntriesOf(expected);
    }

    @Test
    void collectionCountAndElementErrorsAppearTogether() {
        JsonNode errors = errorsOf(jsonPost("/probe/invoices", """
                {"customer_id":5,"currency":"USD","status":"draft",
                 "items":[{"sku":"bad","description":"ok","qty":1,"unit_price_minor":1}]}"""));

        assertThat(flatten(errors)).containsExactlyInAnyOrderEntriesOf(Map.of(
                "Items", "must contain between 3 and 8 items",
                "Items[0].Sku", "must match ^[A-Z]{2}-[0-9]{4}$"));
    }

    @Test
    void quoteCarriesItsOwnCountMessage() {
        assertThat(flatten(errorsOf(jsonPost("/probe/quotes", quote(49, "USD")))))
                .containsExactlyInAnyOrderEntriesOf(Map.of("Items", "must contain exactly 50 items"));
        assertThat(flatten(errorsOf(jsonPost("/probe/quotes", quote(50, "")))))
                .containsExactlyInAnyOrderEntriesOf(Map.of("Currency", "is required"));
    }

    @Test
    void explicitNullOnStringsAndCollectionsIsTheRequiredMessage() {
        JsonNode errors = errorsOf(jsonPost("/probe/invoices",
                "{\"customer_id\":5,\"currency\":null,\"status\":null,\"items\":null}"));

        assertThat(flatten(errors)).containsExactlyInAnyOrderEntriesOf(Map.of(
                "Currency", "is required", "Status", "is required", "Items", "is required"));
    }

    @Test
    void explicitNullOnANumericIsADisclosedDivergence() {
        // DISCLOSED DIVERGENCE, no dial closes it. System.Text.Json refuses null for a
        // non-nullable long and services/dotnet emits the BODY-level 400 (captured:
        // {"errors":{"body":["is required"]}}); a boxed Java component binds null and Jakarta
        // emits the FIELD-level message. Neither a primitive component nor
        // @JsonSetter(nulls = FAIL) closes it: both also reject the ABSENT field, which is the
        // case above and 100% of the frozen workload's exposure. Asserted so the divergence is a
        // checked fact behind its docs/FAIRNESS.md row rather than a footnote.
        JsonNode errors = errorsOf(jsonPost("/probe/invoices", """
                {"customer_id":null,"currency":"USD","status":"draft",
                 "items":[{"sku":"EL-0001","description":"ok","qty":null,"unit_price_minor":null},
                          {"sku":"EL-0001","description":"ok","qty":1,"unit_price_minor":1},
                          {"sku":"EL-0001","description":"ok","qty":1,"unit_price_minor":1}]}"""));

        assertThat(flatten(errors)).containsExactlyInAnyOrderEntriesOf(Map.of(
                "CustomerId", "must be a positive integer",
                "Items[0].Qty", "must be between 1 and 1000"));
    }

    @Test
    void everyEmittedKeyCarriesExactlyOneMessage() {
        // .NET emits one message per key on every captured payload, and normalize.py R2 makes the
        // message array ORDER-significant while Hibernate Validator returns an unordered Set. A
        // second message on any key would be nondeterministic across runs, so the invariant is
        // asserted rather than assumed.
        JsonNode errors = errorsOf(jsonPost("/probe/invoices", """
                {"customer_id":0,"currency":"us","status":"closed",
                 "items":[{"sku":"bad","description":"","qty":0,"unit_price_minor":-1},
                          {"sku":"EL-0001","description":"ok","qty":1,"unit_price_minor":100}]}"""));

        errors.propertyNames().forEach(key ->
                assertThat(errors.get(key).size()).as("messages for %s", key).isEqualTo(1));
    }

    @Test
    void malformedBodyIsTheFrozenBodyRequired400() {
        assertThat(flatten(errorsOf(jsonPost("/probe/invoices", "{\"customer_id\": 1,"))))
                .containsExactlyInAnyOrderEntriesOf(Map.of("body", "is required"));
    }

    @Test
    void bodylessPostIsTheFrozenBodyRequired400() {
        MockHttpServletResponse response = exchange(post("/probe/invoices"));

        assertThat(response.getStatus()).isEqualTo(400);
        assertThat(flatten(parse(response).get("errors")))
                .containsExactlyInAnyOrderEntriesOf(Map.of("body", "is required"));
    }

    @Test
    void floatWhereAnIntegerBelongsIsRefusedLikeSystemTextJson() {
        // Jackson's DEFAULTS return 200 with qty silently truncated to 5; the two
        // coercion properties in application.yaml turn it into the same body-400 .NET emits.
        assertThat(flatten(errorsOf(jsonPost("/probe/quotes",
                quote(50, "USD").replaceFirst("\"qty\":1", "\"qty\":5.7")))))
                .containsExactlyInAnyOrderEntriesOf(Map.of("body", "is required"));
    }

    @Test
    void stringWhereAnIntegerBelongsIsAcceptedLikeSystemTextJson() {
        // JsonSerializerDefaults.Web sets NumberHandling = AllowReadingFromString, so
        // {"qty":"1"} is a 201 on .NET (captured). allow-coercion-of-scalars refuses the whole
        // shape, which is why io.ledgerline.json.ScalarCoercion puts this one input back.
        MockHttpServletResponse response = exchange(post("/probe/quotes")
                .contentType(MediaType.APPLICATION_JSON)
                .content(quote(50, "USD").replaceFirst("\"qty\":1", "\"qty\":\"1\"")));

        assertThat(response.getStatus()).isEqualTo(200);
    }

    @Test
    void numberWhereAStringBelongsIsStillRefusedLikeSystemTextJson() {
        // The other half of the same switch: System.Text.Json never reads a number into a
        // string, and re-enabling string-to-number must not re-enable this direction.
        assertThat(flatten(errorsOf(jsonPost("/probe/quotes",
                quote(50, "USD").replaceFirst("\"currency\":\"USD\"", "\"currency\":840")))))
                .containsExactlyInAnyOrderEntriesOf(Map.of("body", "is required"));
    }

    @Test
    void unknownRequestFieldsAreIgnored() {
        MockHttpServletResponse response = exchange(
                post("/probe/quotes").contentType(MediaType.APPLICATION_JSON)
                        .content(quote(50, "USD").replaceFirst("\\{", "{\"unknown\":1,")));

        assertThat(response.getStatus()).isEqualTo(200);
    }

    @Test
    void nonJsonContentTypeIsTheFrozen415() {
        MockHttpServletResponse response = exchange(
                post("/probe/invoices").contentType(MediaType.TEXT_PLAIN).content("{}"));

        assertThat(response.getStatus()).isEqualTo(415);
        assertThat(response.getContentType()).isEqualTo("application/problem+json");
        assertThat(flatten(parse(response))).containsEntry(
                "type", "https://ledgerline.test/problems/unsupported-media-type");
        assertThat(parse(response).has("errors")).isFalse();
    }

    @Test
    void oversizedBodyIsTheFrozen413() {
        String padding = "x".repeat(70_000);
        MockHttpServletResponse response = exchange(
                post("/probe/invoices").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"description\":\"" + padding + "\"}"));

        assertThat(response.getStatus()).isEqualTo(413);
        assertThat(response.getContentType()).isEqualTo("application/problem+json");
        assertThat(flatten(parse(response))).containsEntry(
                "type", "https://ledgerline.test/problems/payload-too-large");
    }

    @Test
    void absentRowIsTheFrozen404() {
        MockHttpServletResponse response = exchange(get("/probe/invoices/99"));

        assertThat(response.getStatus()).isEqualTo(404);
        assertThat(response.getContentType()).isEqualTo("application/problem+json");
        assertThat(flatten(parse(response))).containsExactlyInAnyOrderEntriesOf(Map.of(
                "type", "https://ledgerline.test/problems/not-found",
                "title", "Not found",
                "status", "404",
                "detail", "The requested resource does not exist."));
    }

    @Test
    void handValidatedParametersUseLowercaseSingleTokenKeys() {
        assertThat(flatten(errorsOf(get("/probe/invoices/0"))))
                .containsExactlyInAnyOrderEntriesOf(Map.of("id", "must be a positive integer"));
        assertThat(flatten(errorsOf(get("/probe/customers/0/invoices?page=0"))))
                .containsExactlyInAnyOrderEntriesOf(Map.of(
                        "id", "must be a positive integer", "page", "must be >= 1"));
        assertThat(flatten(errorsOf(get("/probe/customers/1/invoices?page=100001"))))
                .containsExactlyInAnyOrderEntriesOf(Map.of("page", "must be <= 100000"));
    }

    @Test
    void repeatedPageParameterReadsTheFirstValue() {
        // NOTES-equivalence.md: repeated ?page= is NOT comma-joined on either side. Spring binds
        // a String parameter by joining them, which would make "0,7" unparseable and emit the
        // wrong message, so the probe route takes the List and reads the first element.
        assertThat(flatten(errorsOf(get("/probe/customers/1/invoices?page=0&page=7"))))
                .containsExactlyInAnyOrderEntriesOf(Map.of("page", "must be >= 1"));
    }

    @Test
    void noErrorBodyCarriesInstanceOrTraceIdentifiers() {
        // Spring MVC back-fills ProblemDetail.instance from the request URI on both return paths.
        // Emitting a Problem record rather than a ProblemDetail is what removes it; this asserts
        // the emitted BODY, because the interceptor alternative fails silently when it throws.
        for (MockHttpServletRequestBuilder request : List.of(
                post("/probe/invoices").contentType(MediaType.APPLICATION_JSON).content("{}"),
                post("/probe/invoices").contentType(MediaType.TEXT_PLAIN).content("{}"),
                get("/probe/invoices/99"),
                get("/probe/invoices/0"))) {
            JsonNode body = parse(exchange(request));
            assertThat(keys(body)).doesNotContain("instance", "traceId", "requestId", "trace_id");
        }
    }

    @Test
    void frameworkProducedErrorsGetTheFrozenEnvelopeToo() {
        // Neither status is in the frozen workload, and neither is raised by this service's own
        // code: both come from Spring's dispatcher. They are here because they exercise the
        // createResponseEntity rewrite, which is what keeps a framework ProblemDetail - with its
        // own type URI, its own title and its instance back-fill - off the wire. .NET's
        // CustomizeProblemDetails rewrites the same two the same way, so they agree by
        // construction rather than by coincidence.
        MockHttpServletResponse unmatched = exchange(get("/probe/nothing-here"));
        assertThat(unmatched.getStatus()).isEqualTo(404);
        assertThat(flatten(parse(unmatched))).containsEntry(
                "type", "https://ledgerline.test/problems/not-found");

        MockHttpServletResponse wrongMethod = exchange(jsonPost("/probe/invoices/1", "{}"));
        assertThat(wrongMethod.getStatus()).isEqualTo(405);
        assertThat(flatten(parse(wrongMethod))).containsEntry(
                "type", "https://ledgerline.test/problems/internal");
        assertThat(parse(wrongMethod).get("status").intValue()).isEqualTo(405);
    }

    @Test
    void everyValidationEnvelopeIsTheFrozenOne() {
        JsonNode body = parse(exchange(
                post("/probe/invoices").contentType(MediaType.APPLICATION_JSON).content("{}")));

        assertThat(body.get("type").stringValue())
                .isEqualTo("https://ledgerline.test/problems/validation");
        assertThat(body.get("title").stringValue()).isEqualTo("Validation failed");
        assertThat(body.get("status").intValue()).isEqualTo(400);
        assertThat(body.get("detail").stringValue())
                .isEqualTo("The request payload failed validation.");
    }

    private static String quote(int items, String currency) {
        StringBuilder body = new StringBuilder("{\"currency\":\"" + currency + "\",\"items\":[");
        for (int i = 0; i < items; i++) {
            body.append(i == 0 ? "" : ",")
                    .append("{\"sku\":\"EL-0001\",\"qty\":1,\"unit_price_minor\":100}");
        }
        return body.append("]}").toString();
    }

    private JsonNode errorsOf(MockHttpServletRequestBuilder request) {
        MockHttpServletResponse response = exchange(request);
        assertThat(response.getStatus()).isEqualTo(400);
        assertThat(response.getContentType()).isEqualTo("application/problem+json");
        return parse(response).get("errors");
    }

    private static MockHttpServletRequestBuilder jsonPost(String path, String bodyJson) {
        return post(path).contentType(MediaType.APPLICATION_JSON).content(bodyJson);
    }

    private MockHttpServletResponse exchange(MockHttpServletRequestBuilder request) {
        try {
            return mvc.perform(request).andReturn().getResponse();
        } catch (Exception e) {
            throw new AssertionError(e);
        }
    }

    private JsonNode parse(MockHttpServletResponse response) {
        try {
            return json.readTree(response.getContentAsString(StandardCharsets.UTF_8));
        } catch (Exception e) {
            throw new AssertionError(e);
        }
    }

    /** {@code {"Items[0].Qty": ["..."]}} to {@code {"Items[0].Qty": "..."}} for readable diffs. */
    private static Map<String, String> flatten(JsonNode object) {
        Map<String, String> flat = new LinkedHashMap<>();
        object.propertyNames().forEach(key -> {
            JsonNode value = object.get(key);
            flat.put(key, value.isArray() ? value.get(0).stringValue() : value.asString());
        });
        return flat;
    }

    private static List<String> keys(JsonNode object) {
        List<String> names = new ArrayList<>();
        object.propertyNames().forEach(names::add);
        return names;
    }
}
