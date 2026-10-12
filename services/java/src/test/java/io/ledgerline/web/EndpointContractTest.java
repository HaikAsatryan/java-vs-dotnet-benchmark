package io.ledgerline.web;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.options;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;

import io.ledgerline.GoldenVectors;
import java.nio.charset.StandardCharsets;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.MediaType;
import org.springframework.mock.web.MockHttpServletResponse;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

/**
 * The eight frozen paths of spec/openapi.yaml, exercised on a host with no PostgreSQL and no
 * Redis. Everything asserted here short-circuits before the first infrastructure touch - id and
 * page validation, the content-type gate, the pure-CPU quote, and the two instrumentation
 * endpoints - which is the same DB-less posture the .NET suite's WebApplicationFactory tests
 * assume. The paths that genuinely need a database are in {@code LiveEndpointTest}.
 */
@io.ledgerline.web.MockWebTest
class EndpointContractTest {

    @Autowired
    private MockMvc mvc;

    @Autowired
    private JsonMapper json;

    @Test
    void healthzIsTheStaticGoldenBody() {
        MockHttpServletResponse response = exchange(get("/healthz"));

        assertThat(response.getStatus()).isEqualTo(200);
        assertThat(GoldenVectors.normalize(parse(response)).toString())
                .isEqualTo(GoldenVectors.normalize(GoldenVectors.load("healthz/ok.json")).toString());
    }

    @Test
    void runtimeStatsSatisfiesTheFrozenShape() {
        JsonNode body = parse(exchange(get("/runtime-stats")));

        assertThat(GoldenVectors.checkShape(body, "RuntimeStats")).isEmpty();
    }

    /**
     * The two rows docs/research/2026-08-21-measured.md shows cannot be mapped honestly. The
     * harness reads null as "not measured" and never as zero, so the assertion is that the keys
     * are PRESENT and null - a missing key and a false zero are the two failures to avoid.
     */
    @Test
    void theGcRowsWithNoHonestJvmAnalogArePresentAndNull() {
        JsonNode gc = parse(exchange(get("/runtime-stats"))).get("gc");

        assertThat(gc.get("collections").get("gen0").isNull()).isTrue();
        assertThat(gc.get("collections").get("gen1").isNull()).isTrue();
        assertThat(gc.get("collections").get("gen2").isNull()).isTrue();
        assertThat(gc.get("collections_note").stringValue()).contains("Full GC only");
        assertThat(gc.get("collections").propertyNames()).hasSizeGreaterThan(3);
        assertThat(gc.get("heap_in_use_bytes").longValue()).isPositive();
        assertThat(gc.get("allocated_total_bytes").longValue()).isPositive();
    }

    @Test
    void theDbPoolAcquireCountersAreNullOnBothStacks() {
        JsonNode pool = parse(exchange(get("/runtime-stats"))).get("db_pool");

        assertThat(pool.get("acquire_count").isNull()).isTrue();
        assertThat(pool.get("empty_acquire_count").isNull()).isTrue();
        assertThat(pool.get("acquire_wait_total_seconds").isNull()).isTrue();
        assertThat(pool.get("max").intValue()).isEqualTo(24);
    }

    @Test
    void theConfigEchoCarriesTheKnobsOfEveryRuntimeWithNullWhereOneDoesNotApply() {
        JsonNode config = parse(exchange(get("/runtime-stats"))).get("config");

        assertThat(config.get("gogc").isNull()).isTrue();
        assertThat(config.get("gomemlimit").isNull()).isTrue();
        assertThat(config.get("gc_dynamic_adaptation_mode").isNull()).isTrue();
        assertThat(config.get("max_auto_prepare").isNull()).isTrue();
        // D4's behavioural counterpart: both stacks promote a statement on its 5th execution.
        assertThat(config.get("jdbc_prepare_threshold").intValue()).isEqualTo(5);
        assertThat(config.get("log_queue_len").intValue()).isEqualTo(8192);
    }

    @Test
    void endpointCallCountersUseTheFrozenRouteTemplatesAndMoveOnEveryCall() {
        long before = endpointCalls("GET /healthz");
        exchange(get("/healthz"));
        exchange(get("/healthz"));

        assertThat(endpointCalls("GET /healthz")).isEqualTo(before + 2);
    }

    @Test
    void everyResponseNamesItsMediaTypeWithNoCharsetParameter() {
        // spec/openapi.yaml names "application/json" and "application/problem+json" with no
        // parameters. Measured on the two live SUTs, .NET was sending
        // "application/json; charset=utf-8" on every 2xx while writing a BARE
        // application/problem+json on every error - 15 bytes per response of difference against
        // this side, on every response. Normalized there (RequestIdMiddleware), asserted here.
        assertThat(exchange(get("/healthz")).getContentType()).isEqualTo("application/json");
        assertThat(exchange(get("/runtime-stats")).getContentType()).isEqualTo("application/json");
        assertThat(exchange(post("/pricing/quote")
                .contentType(MediaType.APPLICATION_JSON)
                .content(GoldenVectors
                        .load("pricing_quote/requests/quote_a_wire_request.json").toString()))
                .getContentType()).isEqualTo("application/json");
        assertThat(exchange(get("/invoices/0")).getContentType())
                .isEqualTo("application/problem+json");
    }

    @Test
    void optionsOnAMappedRouteIsTheFrozen405NotASpringGeneratedAllowResponse() {
        // Spring answers OPTIONS itself with a 200, an Allow that adds the implicit HEAD and
        // OPTIONS, and an EMPTY Accept-Patch header; Minimal API maps no OPTIONS handler and
        // answers 405 with the frozen internal envelope and the declared method only.
        MockHttpServletResponse response = exchange(options("/invoices/1"));

        assertThat(response.getStatus()).isEqualTo(405);
        assertThat(response.getHeader("Allow")).isEqualTo("GET");
        assertThat(response.getHeader("Accept-Patch")).isNull();
        assertThat(parse(response).get("type").stringValue())
                .isEqualTo("https://ledgerline.test/problems/internal");
        assertThat(parse(response).get("status").intValue()).isEqualTo(405);
    }

    @Test
    void aNonPositiveInvoiceIdIs400BeforeAnythingIsLoaded() {
        MockHttpServletResponse response = exchange(get("/invoices/0"));

        assertThat(response.getStatus()).isEqualTo(400);
        assertThat(parse(response).get("errors").get("id").get(0).stringValue())
                .isEqualTo("must be a positive integer");
    }

    @ParameterizedTest
    @ValueSource(strings = {"0", "-1", "abc", "1.0", " 1", "9223372036854775808"})
    void theIdGrammarIsTheOneLongTryParseAccepts(String id) {
        assertThat(exchange(get("/invoices/" + id)).getStatus()).isEqualTo(400);
        assertThat(exchange(post("/invoices/" + id + "/pdf-stub")).getStatus()).isEqualTo(400);
    }

    @Test
    void theListEndpointAggregatesTheIdAndPageFailuresIntoOneMap() {
        JsonNode errors = parse(exchange(get("/customers/0/invoices?page=0"))).get("errors");

        assertThat(errors.propertyNames()).containsExactlyInAnyOrder("id", "page");
        assertThat(errors.get("page").get(0).stringValue()).isEqualTo("must be >= 1");
    }

    @ParameterizedTest
    @CsvSource({"0, must be >= 1", "-3, must be >= 1", "abc, must be >= 1",
                "100001, must be <= 100000"})
    void thePageBoundsCarryTheirOwnFrozenMessages(String page, String message) {
        JsonNode errors = parse(exchange(get("/customers/1/invoices?page=" + page))).get("errors");

        assertThat(errors.get("page").get(0).stringValue()).isEqualTo(message);
    }

    @Test
    void aCommaSeparatedPageValueIsOneUnparseableStringNotTwoValues() {
        // Spring's String->List conversion splits "1,2" into two elements and would silently
        // accept the first; ASP.NET's Query["page"][0] hands the whole "1,2" to the parser.
        JsonNode errors = parse(exchange(get("/customers/1/invoices?page=1,2"))).get("errors");

        assertThat(errors.get("page").get(0).stringValue()).isEqualTo("must be >= 1");
    }

    @Test
    void aRepeatedPageParameterReadsTheFirstValue() {
        assertThat(exchange(get("/customers/1/invoices?page=0&page=5")).getStatus()).isEqualTo(400);
    }

    @ParameterizedTest
    @ValueSource(strings = {"quote_a", "quote_b", "quote_c"})
    void theQuoteEndpointReproducesTheGoldenWireVectors(String vector) {
        String request = GoldenVectors
                .load("pricing_quote/requests/" + vector + "_wire_request.json").toString();

        MockHttpServletResponse response = exchange(
                post("/pricing/quote").contentType(MediaType.APPLICATION_JSON).content(request));

        assertThat(response.getStatus()).isEqualTo(200);
        assertThat(GoldenVectors.normalize(parse(response)).toString()).isEqualTo(
                GoldenVectors.normalize(GoldenVectors.load("pricing_quote/" + vector + "_wire.json"))
                        .toString());
    }

    @Test
    void theQuoteEndpointIsBehindTheContentTypeGate() {
        MockHttpServletResponse response = exchange(post("/pricing/quote")
                .contentType(MediaType.TEXT_PLAIN).content("{}"));

        assertThat(response.getStatus()).isEqualTo(415);
        assertThat(parse(response).get("type").stringValue())
                .isEqualTo("https://ledgerline.test/problems/unsupported-media-type");
    }

    @Test
    void anUnmatchedPathIsTheFrozen404EnvelopeAndNotAnEmptyBody() {
        // spring.web.resources.add-mappings is off, so nothing matches /** any more and the
        // dispatcher raises NoHandlerFoundException; LedgerExceptionHandler has to turn that
        // into the same envelope every other 404 gets.
        MockHttpServletResponse response = exchange(get("/not-a-route"));

        assertThat(response.getStatus()).isEqualTo(404);
        assertThat(parse(response).get("type").stringValue())
                .isEqualTo("https://ledgerline.test/problems/not-found");
        assertThat(response.getContentType()).startsWith("application/problem+json");
    }

    @Test
    void everyResponseEchoesTheRequestIdHeaderAndNoBodyCarriesIt() {
        MockHttpServletResponse response = exchange(
                get("/invoices/0").header("X-Request-Id", "given-id"));

        assertThat(response.getHeader("X-Request-Id")).isEqualTo("given-id");
        assertThat(parse(response).get("request_id")).isNull();
    }

    @Test
    void anInboundRequestIdIsGeneratedWhenAbsent() {
        assertThat(exchange(get("/healthz")).getHeader("X-Request-Id")).isNotBlank();
    }

    private long endpointCalls(String route) {
        return parse(exchange(get("/runtime-stats"))).get("endpoint_calls").get(route).longValue();
    }

    private MockHttpServletResponse exchange(MockHttpServletRequestBuilder request) {
        try {
            return mvc.perform(request).andReturn().getResponse();
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private JsonNode parse(MockHttpServletResponse response) {
        try {
            return json.readTree(response.getContentAsString(StandardCharsets.UTF_8));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }
}
