package io.ledgerline;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

/**
 * Asserts the Java port of {@code shapecheck.py} rejects what the Python does. Only the
 * RuntimeStats branch is covered here: the other branches are exercised by every body
 * {@link io.ledgerline.json.WireContractTest} runs through them, whereas /runtime-stats has no
 * DTO, so nothing else would notice if this branch returned an empty violation list for a
 * body missing every {@code build.*} field and every {@code endpoint_calls} key.
 */
class GoldenVectorsTest {

    private static final JsonMapper MAPPER = JsonMapper.builder().build();

    @Test
    void theRuntimeStatsBranchRejectsAMissingBuildObjectAndMissingEndpointKeys() {
        JsonNode body = MAPPER.readTree("""
                {"ts":"2026-06-06T00:00:00.000000Z","uptime_seconds":1,"build":{},"config":{},
                 "gc":{},"db_pool":{},"redis_pool":{},"goroutines_or_threadpool":1,
                 "endpoint_calls":{}}""");

        assertThat(GoldenVectors.checkShape(body, "RuntimeStats"))
                .contains("RuntimeStats.build.language: missing required key")
                .contains("RuntimeStats.build.processor_count: missing required key")
                .contains("RuntimeStats.endpoint_calls: missing key 'GET /healthz'")
                .contains("RuntimeStats.endpoint_calls: missing key 'POST /invoices'");
    }

    @Test
    void theRuntimeStatsBranchAcceptsACompleteBody() {
        JsonNode body = MAPPER.readTree("""
                {"ts":"2026-06-06T00:00:00.000000Z","uptime_seconds":1,
                 "build":{"language":"java","runtime_version":"25","service_version":"0",
                          "data_layer":"jpa","processor_count":6},
                 "config":{},"gc":{},"db_pool":{},"redis_pool":{},
                 "goroutines_or_threadpool":1,
                 "endpoint_calls":{"GET /invoices/{id}":0,"GET /customers/{id}/invoices":0,
                                   "POST /pricing/quote":0,"POST /invoices":0,
                                   "POST /invoices/{id}/pdf-stub":0,"GET /healthz":0}}""");

        assertThat(GoldenVectors.checkShape(body, "RuntimeStats")).isEmpty();
    }
}
