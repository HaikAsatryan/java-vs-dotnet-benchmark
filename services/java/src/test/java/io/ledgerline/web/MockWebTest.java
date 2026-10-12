package io.ledgerline.web;

import java.lang.annotation.Documented;
import java.lang.annotation.ElementType;
import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;
import java.lang.annotation.Target;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.context.annotation.Import;

/**
 * Boots the real application context with the real MVC pipeline and no infrastructure, the direct
 * counterpart of {@code services/dotnet/Ledgerline.Tests/TestEnv.cs} plus WebApplicationFactory.
 * Validation, the content-type gate and the body-size limit all short-circuit before the first
 * DB or Redis touch, so the whole error contract is assertable on a host with neither running -
 * which is what the {@code dotnet} CI job already assumes for its own side.
 *
 * <p>Three properties differ from production and each is here because the process has no database:
 * the pool warm is off (as {@code TestEnv.cs} sets {@code DB_POOL_WARM_ON_START=0}), and Hibernate
 * is given its dialect instead of reading it from JDBC metadata it cannot reach. None of them
 * touches the web layer under test.
 */
@Target(ElementType.TYPE)
@Retention(RetentionPolicy.RUNTIME)
@Documented
@SpringBootTest(properties = {
    "ledger.db.pool-warm-on-start=false",
    "spring.jpa.properties.hibernate.boot.allow_jdbc_metadata_access=false",
    "spring.jpa.properties.hibernate.dialect=org.hibernate.dialect.PostgreSQLDialect",
})
@AutoConfigureMockMvc
@Import(ContractProbeController.class)
public @interface MockWebTest {
}
