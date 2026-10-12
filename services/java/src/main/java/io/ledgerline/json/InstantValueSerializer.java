package io.ledgerline.json;

import java.time.Instant;
import org.springframework.boot.jackson.JacksonComponent;
import tools.jackson.core.JsonGenerator;
import tools.jackson.databind.SerializationContext;
import tools.jackson.databind.ser.std.StdSerializer;

/**
 * The single Jackson customisation on the serialization side, registered as a bean rather than
 * with {@code @JsonSerialize} per component so a field added later cannot miss the frozen format.
 * {@code @JacksonComponent} does not change the registered-module list, which is the artifact
 * behind docs/FAIRNESS.md's "no Blackbird".
 */
@JacksonComponent
public class InstantValueSerializer extends StdSerializer<Instant> {

    public InstantValueSerializer() {
        super(Instant.class);
    }

    @Override
    public void serialize(Instant value, JsonGenerator generator, SerializationContext context) {
        generator.writeString(Rfc3339Micros.format(value));
    }
}
