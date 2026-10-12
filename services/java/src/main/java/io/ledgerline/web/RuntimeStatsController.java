package io.ledgerline.web;

import io.ledgerline.observability.RuntimeStatsCollector;
import io.ledgerline.observability.RuntimeStatsWriter;
import java.util.Map;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
class RuntimeStatsController {

    private final RuntimeStatsCollector collector;
    private final RuntimeStatsWriter writer;

    RuntimeStatsController(RuntimeStatsCollector collector, RuntimeStatsWriter writer) {
        this.collector = collector;
        this.writer = writer;
    }

    /** Polled at 1 Hz by the runner; excluded from the workload mix and from the access log. */
    @GetMapping("/runtime-stats")
    Map<String, Object> runtimeStats() {
        return writer.write(collector.snapshot());
    }
}
