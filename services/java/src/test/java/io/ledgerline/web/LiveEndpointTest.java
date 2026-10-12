package io.ledgerline.web;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;

import io.ledgerline.GoldenVectors;
import io.ledgerline.SpecPaths;
import io.ledgerline.config.LedgerProperties;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.Statement;
import javax.sql.DataSource;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.hibernate.stat.Statistics;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.data.redis.connection.RedisConnectionFactory;
import org.springframework.http.MediaType;
import org.springframework.mock.web.MockHttpServletResponse;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

/**
 * The paths that cannot be asserted without a database and a Redis: the create transaction, the
 * cache-aside read, the paged list and the existence probes. Everything else in the suite runs
 * on a host with neither, so this class is OPT-IN - set {@code LEDGERLINE_LIVE=1} plus the
 * ordinary {@code PG_*} / {@code REDIS_ADDR} env surface and it runs; leave it unset and JUnit
 * skips it rather than failing.
 *
 * <p>The schema is applied from spec/schema.sql VERBATIM before each test, which makes this the
 * one place where the entity mapping is checked against the frozen DDL rather than against a
 * dialect's idea of it.
 *
 * <p>To run it: bring up a throwaway pair, point the env at it, and set the switch.
 *
 * <pre>
 * docker run -d --name ledger-pg -e POSTGRES_USER=ledger -e POSTGRES_PASSWORD=ledger \
 *     -e POSTGRES_DB=ledgerline -p 5432:5432 postgres:18.4
 * docker run -d --name ledger-redis -p 6379:6379 redis:8.10-alpine
 * LEDGERLINE_LIVE=1 ./mvnw -B test -Dtest=LiveEndpointTest
 * </pre>
 *
 * The defaults in application.yaml already point at localhost:5432 / localhost:6379 with the
 * ledger/ledger credentials, so no other variable is needed; PG_HOST, PG_PORT, PG_DB, PG_USER,
 * PG_PASSWORD, REDIS_ADDR and PDF_DIR override them. The class DROPS and recreates the three
 * tables before every test, so never point it at a database that matters.
 */
@EnabledIfEnvironmentVariable(named = "LEDGERLINE_LIVE", matches = "1",
        disabledReason = "needs a live PostgreSQL and Redis; see the class javadoc")
// The pool warm is ON here and off everywhere else: with a real database behind it, this is
// the only place the startup warm and the warmup gate's db_pool.total == max can be exercised.
@SpringBootTest(properties = {
    "ledger.db.pool-warm-on-start=true",
    // For theCreatePathIssuesFourInsertsAndNoUpdate; off in the SUT, so it is set here and
    // nowhere else.
    "spring.jpa.properties.hibernate.generate_statistics=true",
})
@AutoConfigureMockMvc
class LiveEndpointTest {

    @Autowired
    private MockMvc mvc;

    @Autowired
    private JsonMapper json;

    @Autowired
    private DataSource dataSource;

    @Autowired
    private RedisConnectionFactory redis;

    @Autowired
    private LedgerProperties properties;

    @Autowired
    private org.hibernate.SessionFactory sessionFactory;

    private static final String CREATE_BODY = """
            {"customer_id":1,"currency":"USD","status":"draft",
             "items":[{"sku":"EL-0042","description":"widget","qty":10,"unit_price_minor":10000},
                      {"sku":"GR-0001","description":"grocery","qty":1,"unit_price_minor":500},
                      {"sku":"HZ-0007","description":"hazmat","qty":3,"unit_price_minor":333}]}""";

    @BeforeEach
    void resetFixtures() throws Exception {
        try (Connection connection = dataSource.getConnection();
             Statement statement = connection.createStatement()) {
            statement.execute("DROP TABLE IF EXISTS invoice_items, invoices, customers CASCADE");
            statement.execute(SpecPaths.read(SpecPaths.schemaSql()));
            statement.execute("""
                    INSERT INTO customers (id, name, email, balance_minor, currency, created_at)
                    VALUES (1, 'a', 'a@x', 0, 'USD', now()), (2, 'b', 'b@x', 0, 'USD', now())""");
            statement.execute("""
                    INSERT INTO invoices (customer_id, status, currency, total_minor)
                    SELECT 1, 'open', 'USD', 100 * n FROM generate_series(1, 25) AS n""");
            statement.execute("""
                    INSERT INTO invoice_items (invoice_id, sku, description, qty,
                                               unit_price_minor, line_total_minor)
                    SELECT id, 'EL-0001', 'seeded', 1, total_minor, total_minor FROM invoices""");
        }
        try (var connection = redis.getConnection()) {
            connection.serverCommands().flushAll();
        }
    }

    @Test
    void theCreatePathWritesTheHeaderTheItemsAndTheBalanceInOneTransaction() throws Exception {
        MockHttpServletResponse response = exchange(post("/invoices")
                .contentType(MediaType.APPLICATION_JSON).content(CREATE_BODY));

        assertThat(response.getStatus()).isEqualTo(201);
        JsonNode body = parse(response);
        assertThat(GoldenVectors.checkShape(body, "Invoice")).isEmpty();
        assertThat(response.getHeader("Location"))
                .isEqualTo("/invoices/" + body.get("id").longValue());
        // Server-computed: the client never sends totals (spec/openapi.yaml).
        assertThat(body.get("total_minor").longValue()).isEqualTo(100_000 + 500 + 999);
        assertThat(body.get("items").valueStream().map(item -> item.get("id").longValue()))
                .allMatch(id -> id > 0);
        // created_at came back on the INSERT's RETURNING clause, in the frozen wire format.
        GoldenVectors.normalize(body);

        assertThat(scalar("SELECT balance_minor FROM customers WHERE id = 1"))
                .isEqualTo(100_000L + 500 + 999);
        assertThat(scalar("SELECT count(*) FROM invoice_items WHERE invoice_id = "
                + body.get("id").longValue())).isEqualTo(3L);
    }

    @Test
    void theCreatePathIssuesFourInsertsAndNoUpdate() throws Exception {
        // The flush-ordering rule InvoiceController.create carries as a comment, as a fact.
        // IDENTITY makes persist() an immediate INSERT, so total_minor has to be assigned before
        // the write; assigning it afterwards is a natural-looking edit that leaves every
        // functional assertion green and adds a full-column UPDATE on invoices to 22% of the mix.
        Statistics statistics = sessionFactory.getStatistics();
        statistics.clear();

        assertThat(exchange(post("/invoices")
                .contentType(MediaType.APPLICATION_JSON).content(CREATE_BODY))
                .getStatus()).isEqualTo(201);

        assertThat(statistics.getEntityInsertCount()).isEqualTo(4);
        assertThat(statistics.getEntityUpdateCount()).isZero();
    }

    @Test
    void anAbsentCustomerIs404AndWritesNothing() throws Exception {
        MockHttpServletResponse response = exchange(post("/invoices")
                .contentType(MediaType.APPLICATION_JSON)
                .content(CREATE_BODY.replace("\"customer_id\":1", "\"customer_id\":999999")));

        assertThat(response.getStatus()).isEqualTo(404);
        assertThat(scalar("SELECT count(*) FROM invoices WHERE customer_id = 999999")).isZero();
    }

    @Test
    void aCacheHitIsByteIdenticalToTheMissThatFilledIt() {
        String miss = exchangeBody(get("/invoices/1"));
        String hit = exchangeBody(get("/invoices/1"));

        assertThat(hit).isEqualTo(miss);
        assertThat(GoldenVectors.checkShape(json.readTree(hit), "Invoice")).isEmpty();
    }

    @Test
    void anAbsentInvoiceIs404AndIsNotNegativelyCached() throws Exception {
        assertThat(exchange(get("/invoices/999999")).getStatus()).isEqualTo(404);
        try (var connection = redis.getConnection()) {
            assertThat(connection.keyCommands()
                    .exists("invoice:999999".getBytes(StandardCharsets.UTF_8))).isFalse();
        }
        assertThat(exchange(get("/invoices/999999")).getStatus()).isEqualTo(404);
    }

    @Test
    void theCreatePathInvalidatesTheKeyItJustWrote() throws Exception {
        exchange(get("/invoices/1"));
        try (var connection = redis.getConnection()) {
            connection.stringCommands().set("invoice:26".getBytes(StandardCharsets.UTF_8),
                    "stale".getBytes(StandardCharsets.UTF_8));
        }

        long created = parse(exchange(post("/invoices")
                .contentType(MediaType.APPLICATION_JSON).content(CREATE_BODY)))
                .get("id").longValue();

        try (var connection = redis.getConnection()) {
            assertThat(connection.keyCommands()
                    .exists(("invoice:" + created).getBytes(StandardCharsets.UTF_8))).isFalse();
        }
    }

    @Test
    void theListPageIsTwentyRowsIdDescending() {
        JsonNode page = parse(exchange(get("/customers/1/invoices?page=1")));

        assertThat(GoldenVectors.checkShape(page, "InvoiceListPage")).isEmpty();
        assertThat(page.get("page_size").intValue()).isEqualTo(20);
        assertThat(page.get("items").size()).isEqualTo(20);
        long previous = Long.MAX_VALUE;
        for (JsonNode item : page.get("items")) {
            assertThat(item.get("id").longValue()).isLessThan(previous);
            previous = item.get("id").longValue();
        }
        // Page 2 exercises the SECOND Q3 statement text (the one with the offset clause).
        assertThat(parse(exchange(get("/customers/1/invoices?page=2"))).get("items").size())
                .isEqualTo(5);
    }

    @Test
    void anEmptyPageIs200ForAKnownCustomerAnd404ForAnUnknownOne() {
        MockHttpServletResponse present = exchange(get("/customers/2/invoices?page=1"));
        assertThat(present.getStatus()).isEqualTo(200);
        assertThat(parse(present).get("items")).isEmpty();

        assertThat(exchange(get("/customers/999999/invoices")).getStatus()).isEqualTo(404);
        assertThat(exchange(get("/customers/1/invoices?page=100000")).getStatus()).isEqualTo(200);
    }

    @Test
    void thePdfStubWritesExactlyTheFrozenByteCountToPdfDir() {
        MockHttpServletResponse response = exchange(post("/invoices/1/pdf-stub"));

        assertThat(response.getStatus()).isEqualTo(200);
        JsonNode body = parse(response);
        assertThat(GoldenVectors.checkShape(body, "PdfStub")).isEmpty();
        assertThat(Path.of(properties.pdfDir(), "1.pdfstub")).exists();
        assertThat(fileSize(Path.of(properties.pdfDir(), "1.pdfstub"))).isEqualTo(32768);

        assertThat(exchange(post("/invoices/999999/pdf-stub")).getStatus()).isEqualTo(404);
    }

    @Test
    void readyzIsGreenWhenBothDependenciesAnswer() {
        MockHttpServletResponse response = exchange(get("/readyz"));

        assertThat(response.getStatus()).isEqualTo(200);
        assertThat(parse(response).get("status").stringValue()).isEqualTo("ok");
    }

    @Test
    void theWarmupGateSeesAFullPool() {
        // spec/db-access.md's min=max policy exists so the pool never grows or prunes mid-run,
        // and the warmup gate asserts db_pool.total == max off this endpoint.
        JsonNode pool = parse(exchange(get("/runtime-stats"))).get("db_pool");

        assertThat(pool.get("max").intValue()).isEqualTo(properties.db().poolMax());
        assertThat(pool.get("total").intValue()).isEqualTo(properties.db().poolMax());
    }

    private long scalar(String sql) throws Exception {
        try (Connection connection = dataSource.getConnection();
             Statement statement = connection.createStatement();
             var rows = statement.executeQuery(sql)) {
            return rows.next() ? rows.getLong(1) : -1;
        }
    }

    private static long fileSize(Path path) {
        try {
            return Files.size(path);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private MockHttpServletResponse exchange(MockHttpServletRequestBuilder request) {
        try {
            return mvc.perform(request).andReturn().getResponse();
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private String exchangeBody(MockHttpServletRequestBuilder request) {
        try {
            return exchange(request).getContentAsString(StandardCharsets.UTF_8);
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
