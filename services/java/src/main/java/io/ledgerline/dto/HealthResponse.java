package io.ledgerline.dto;

/** {@code {"status":"ok"}} - spec/openapi.yaml Health, golden vector golden/healthz/ok.json. */
public record HealthResponse(String status) {

    public static final HealthResponse OK = new HealthResponse("ok");

    /** Not spec-frozen; the string both services aligned on for a failed /readyz. */
    public static final HealthResponse UNAVAILABLE = new HealthResponse("unavailable");
}
