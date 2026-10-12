package io.ledgerline;

import java.time.ZoneOffset;
import java.util.TimeZone;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.ConfigurationPropertiesScan;

@SpringBootApplication
@ConfigurationPropertiesScan
public class LedgerlineApplication {

    public static void main(String[] args) {
        // Set before anything can cache it. Every value on the frozen wire is already
        // zone-independent by construction, so this changes no contract behaviour; it removes the
        // host's TZ - which the container does not set and the manifest does not record - from
        // the set of things a future timestamp could depend on.
        TimeZone.setDefault(TimeZone.getTimeZone(ZoneOffset.UTC));
        SpringApplication.run(LedgerlineApplication.class, args);
    }
}
