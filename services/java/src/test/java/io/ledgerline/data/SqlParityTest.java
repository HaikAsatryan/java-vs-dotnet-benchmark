package io.ledgerline.data;

import static org.assertj.core.api.Assertions.assertThat;

import io.ledgerline.SpecPaths;
import java.lang.reflect.Method;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;
import org.springframework.data.jpa.repository.Query;

/**
 * Asserts the two native statements are byte-identical to their bodies in spec/db-access.sql
 * once JDBC's positional placeholders are normalised, and - the more important half - that they
 * are the ONLY native statements in the service. The analog of
 * {@code services/dotnet/Ledgerline.Tests/SqlParityTests.cs}, with the extra invariant that
 * guards the boundary this round exists to hold: everything else goes through the ORM.
 */
class SqlParityTest {

    private static final Map<String, String> SPEC_BODIES = parseSpec();

    @Test
    void customerExistsMatchesTheSpecBody() {
        // Q3b/Q7. Native because neither JPA form reaches the frozen text: existsById emits
        // count(*) and JPQL count(c)>0 emits a count comparison. Hibernate copies native SQL
        // character for character, so after ?n -> $n this is the spec's wire text exactly,
        // parameter numbering included.
        assertThat(normalisePlaceholders(nativeSql(CustomerRepository.class, "customerExists", long.class)))
                .isEqualTo(normalisePlaceholders(SPEC_BODIES.get("CustomerExists")));
        assertThat(nativeSql(CustomerRepository.class, "customerExists", long.class))
                .isEqualTo(SPEC_BODIES.get("CustomerExists").replace("$1", "?1"));
    }

    @Test
    void updateCustomerBalanceMatchesTheSpecBodyExceptForParameterNumbering() {
        String java = nativeSql(CustomerRepository.class, "addToBalance", long.class, long.class);
        String spec = SPEC_BODIES.get("UpdateCustomerBalance");

        assertThat(normalisePlaceholders(java)).isEqualTo(normalisePlaceholders(spec));

        // PINNED DEVIATION. JDBC placeholders are positional, so the increment amount is
        // necessarily $1 and the id $2, where spec/db-access.sql (and the ADO variant, which
        // binds $n directly) numbers them the other way round. Same body, same two parameters,
        // same plan; the labels differ, and pg_stat views show it.
        assertThat(java).contains("balance_minor + ?1", "WHERE id = ?2");
        assertThat(spec).contains("balance_minor + $2", "WHERE id = $1");
    }

    @Test
    void noOtherStatementIsNative() {
        // The boundary that keeps this an ORM-vs-ORM comparison. A native @Query added to a read
        // path here would make it ORM against hand-written SQL, so the set is asserted rather
        // than described.
        var nativeMethods = Stream.of(InvoiceRepository.class, InvoiceItemRepository.class, CustomerRepository.class)
                .flatMap(repository -> Stream.of(repository.getMethods()))
                .filter(method -> {
                    Query query = method.getAnnotation(Query.class);
                    return query != null && query.nativeQuery();
                })
                .map(method -> method.getDeclaringClass().getSimpleName() + "." + method.getName())
                .toList();

        assertThat(nativeMethods).containsExactlyInAnyOrder(
                "CustomerRepository.customerExists", "CustomerRepository.addToBalance");
    }

    @Test
    void theSpecFileStillCarriesExactlySevenStatements() {
        // A statement added to spec/db-access.sql fails here until someone decides where the Java
        // side carries it. This is a statement about the SPEC FILE only - the two native bodies
        // above and StatementShapeTest's five generated texts are what pin what Java emits.
        assertThat(SPEC_BODIES.keySet()).containsExactlyInAnyOrder(
                "GetInvoice", "GetInvoiceItems", "ListInvoicesByCustomer", "CustomerExists",
                "InsertInvoice", "InsertInvoiceItem", "UpdateCustomerBalance");
    }

    private static String nativeSql(Class<?> repository, String method, Class<?>... parameterTypes) {
        Method declared;
        try {
            declared = repository.getMethod(method, parameterTypes);
        } catch (NoSuchMethodException e) {
            throw new AssertionError(e);
        }
        Query query = declared.getAnnotation(Query.class);
        assertThat(query.nativeQuery()).isTrue();
        return query.value();
    }

    private static String normalisePlaceholders(String sql) {
        return sql.replaceAll("[?$]\\d+", "?");
    }

    /**
     * Parses spec/db-access.sql by its {@code -- name:} annotations, taking each body as
     * everything up to the next annotation with surrounding blank lines and the trailing
     * {@code ;} separator removed - the same normalisation SqlParityTests.cs applies, so both
     * services pin the identical wire text.
     */
    private static Map<String, String> parseSpec() {
        Map<String, String> bodies = new LinkedHashMap<>();
        String current = null;
        StringBuilder body = new StringBuilder();

        for (String line : SpecPaths.read(SpecPaths.dbAccessSql()).replace("\r\n", "\n").split("\n", -1)) {
            if (line.startsWith("-- name:")) {
                flush(bodies, current, body);
                current = line.split("\\s+")[2];
                body.setLength(0);
            } else if (!line.startsWith("--")) {
                body.append(line).append('\n');
            }
        }
        flush(bodies, current, body);
        return bodies;
    }

    private static void flush(Map<String, String> bodies, String name, StringBuilder body) {
        if (name != null) {
            bodies.put(name, body.toString().strip().replaceAll(";$", ""));
        }
    }
}
