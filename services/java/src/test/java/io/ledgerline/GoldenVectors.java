package io.ledgerline;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.TreeMap;
import java.util.regex.Pattern;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * The committed cross-service vectors under {@code bench/validation/golden/}, plus a Java port of
 * the two rules the Python suite applies to every body before comparing it:
 * {@code bench/validation/normalize.py} and {@code bench/validation/shapecheck.py}.
 *
 * <p>Ported rather than approximated. The point of running the vectors here is that a divergence
 * fails at {@code mvn test} on a laptop instead of at P5 against a live pair of services, and that
 * only holds if the rules are the SAME rules - so the constants below are copied from the Python
 * source and every branch of {@code check()} is carried over, RuntimeStats included. The two
 * normalize.py rules not ported are the ones that need two services (R4's placeholder mode) and
 * the arrays-are-order-significant rule (R2), which is a statement about what normalization must
 * NOT do.
 */
public final class GoldenVectors {

    public static final Path DIR = findGoldenDir();

    /** normalize.py: {@code _RFC3339_MICROS_Z}. */
    private static final Pattern RFC3339_MICROS_Z =
            Pattern.compile("^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{6}Z$");

    /** normalize.py: {@code _MONEY_KEY_SUBSTRINGS} - keys whose value must be a JSON integer. */
    private static final List<String> MONEY_KEY_SUBSTRINGS = List.of("_minor", "balance");

    /** normalize.py: {@code _TIMESTAMP_KEYS}. */
    private static final List<String> TIMESTAMP_KEYS = List.of("created_at", "ts");

    /** normalize.py: {@code EXCLUDED_KEYS} - never in a body, stripped defensively at any depth. */
    private static final List<String> EXCLUDED_KEYS = List.of("request_id");

    private static final JsonMapper MAPPER = JsonMapper.builder().build();

    private GoldenVectors() {
    }

    public static JsonNode load(String relativePath) {
        try {
            return MAPPER.readTree(Files.readString(DIR.resolve(relativePath)));
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }

    /**
     * normalize.py R1/R3/R5/R6: object keys sorted recursively, {@code request_id} stripped, money
     * keys asserted integral, timestamp keys asserted RFC3339-micros-Z. Arrays keep their order.
     */
    public static JsonNode normalize(JsonNode value) {
        return normalize(value, "$", null);
    }

    /** shapecheck.py {@code check}: returns the violations, empty when the body satisfies. */
    public static List<String> checkShape(JsonNode body, String schemaName) {
        JsonNode shapes = load("shapes.json");
        JsonNode descriptor = shapes.get(schemaName);
        if (descriptor == null) {
            throw new IllegalArgumentException("no shape descriptor " + schemaName);
        }
        List<String> errors = checkRequired(body, descriptor.get("required"), schemaName);
        if (!errors.isEmpty()) {
            return errors;
        }
        checkConst(body, descriptor, "page_size_const", "page_size", schemaName, errors);
        checkConst(body, descriptor, "bytes_written_const", "bytes_written", schemaName, errors);
        JsonNode element = descriptor.get("items_element");
        if (element != null) {
            JsonNode items = body.get("items");
            for (int i = 0; i < items.size(); i++) {
                int index = i;
                checkShape(items.get(i), element.stringValue()).forEach(violation ->
                        errors.add(schemaName + ".items[" + index + "] -> " + violation));
            }
        }
        if (schemaName.equals("RuntimeStats")) {
            errors.addAll(checkRuntimeStatsExtras(body, descriptor));
        }
        return errors;
    }

    /**
     * shapecheck.py's {@code RuntimeStats} branch: the nested {@code build} object and the
     * required {@code endpoint_calls} keys. Without it {@code checkShape} would return no
     * violations for a /runtime-stats body missing every one of them.
     */
    private static List<String> checkRuntimeStatsExtras(JsonNode body, JsonNode descriptor) {
        List<String> errors = new ArrayList<>();
        JsonNode buildRequired = descriptor.get("build_required");
        if (buildRequired != null) {
            JsonNode build = body.get("build");
            errors.addAll(checkRequired(build == null ? MAPPER.createObjectNode() : build,
                    buildRequired, "RuntimeStats.build"));
        }
        JsonNode calls = descriptor.get("endpoint_calls_keys");
        if (calls != null) {
            JsonNode actual = body.get("endpoint_calls");
            for (JsonNode key : calls) {
                if (actual == null || actual.get(key.stringValue()) == null) {
                    errors.add("RuntimeStats.endpoint_calls: missing key '"
                            + key.stringValue() + "'");
                }
            }
        }
        return errors;
    }

    private static void checkConst(JsonNode body, JsonNode descriptor, String constKey,
            String bodyKey, String schemaName, List<String> errors) {
        JsonNode expected = descriptor.get(constKey);
        if (expected != null && body.get(bodyKey).intValue() != expected.intValue()) {
            errors.add(schemaName + "." + bodyKey + ": expected " + expected.intValue()
                    + ", got " + body.get(bodyKey).intValue());
        }
    }

    private static List<String> checkRequired(JsonNode body, JsonNode required, String path) {
        List<String> errors = new ArrayList<>();
        for (String key : required.propertyNames()) {
            JsonNode value = body.get(key);
            String token = required.get(key).stringValue();
            if (value == null) {
                errors.add(path + "." + key + ": missing required key");
            } else if (!typeOk(value, token)) {
                errors.add(path + "." + key + ": expected " + token + ", got "
                        + value.getNodeType());
            }
        }
        return errors;
    }

    private static boolean typeOk(JsonNode value, String token) {
        return switch (token) {
            case "int" -> value.isIntegralNumber();
            case "number" -> value.isNumber();
            case "str" -> value.isString();
            case "array" -> value.isArray();
            case "object" -> value.isObject();
            default -> throw new IllegalArgumentException("unknown type token " + token);
        };
    }

    private static JsonNode normalize(JsonNode value, String path, String key) {
        if (value.isObject()) {
            Map<String, JsonNode> sorted = new TreeMap<>();
            for (String property : value.propertyNames()) {
                if (!EXCLUDED_KEYS.contains(property)) {
                    sorted.put(property,
                            normalize(value.get(property), path + "." + property, property));
                }
            }
            ObjectNode out = MAPPER.createObjectNode();
            sorted.forEach(out::set);
            return out;
        }
        if (value.isArray()) {
            ArrayNode out = MAPPER.createArrayNode();
            for (int i = 0; i < value.size(); i++) {
                out.add(normalize(value.get(i), path + "[" + i + "]", null));
            }
            return out;
        }
        if (key != null && TIMESTAMP_KEYS.contains(key) && value.isString()) {
            if (!RFC3339_MICROS_Z.matcher(value.stringValue()).matches()) {
                throw new AssertionError(path + ": timestamp not RFC3339 UTC with exactly 6 "
                        + "fractional digits + Z (got " + value.stringValue() + ")");
            }
        }
        if (key != null && isMoneyKey(key) && !value.isIntegralNumber()) {
            throw new AssertionError(path + ": money field " + key
                    + " must be an integer minor-unit value, got " + value.getNodeType());
        }
        return value;
    }

    private static boolean isMoneyKey(String key) {
        String lower = key.toLowerCase(Locale.ROOT);
        return MONEY_KEY_SUBSTRINGS.stream().anyMatch(lower::contains);
    }

    private static Path findGoldenDir() {
        Path dir = Path.of("").toAbsolutePath();
        while (dir != null) {
            Path candidate = dir.resolve("bench/validation/golden");
            if (Files.exists(candidate.resolve("shapes.json"))) {
                return candidate;
            }
            dir = dir.getParent();
        }
        throw new IllegalStateException("could not locate bench/validation/golden");
    }
}
