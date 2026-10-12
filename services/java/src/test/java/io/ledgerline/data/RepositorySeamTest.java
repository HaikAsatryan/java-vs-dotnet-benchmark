package io.ledgerline.data;

import static org.assertj.core.api.Assertions.assertThat;

import java.lang.reflect.Method;
import java.util.List;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;
import org.springframework.aop.Advisor;
import org.springframework.aop.framework.Advised;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.data.jpa.repository.Query;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.interceptor.TransactionAttribute;
import org.springframework.transaction.interceptor.TransactionInterceptor;

/**
 * Which methods run inside a transaction, read from the live proxies rather than from the
 * annotations - because for a Spring Data repository the two are not the same thing. A declared
 * {@code @Query} whose method NAME matches one on {@code CrudRepository} is matched against
 * {@code SimpleJpaRepository}, inherits its class-level {@code @Transactional(readOnly = true)},
 * and executes as {@code BEGIN READ ONLY / SELECT / COMMIT} - three round trips where
 * spec/db-access.md pins one. Nothing about the interface shows it; the transaction attribute
 * does.
 *
 * <p>No database is needed: the attribute is resolved by Spring's own
 * {@code RepositoryAnnotationTransactionAttributeSource} at proxy-creation time.
 */
@SpringBootTest(
        webEnvironment = SpringBootTest.WebEnvironment.NONE,
        properties = {
            "ledger.db.pool-warm-on-start=false",
            "spring.jpa.properties.hibernate.boot.allow_jdbc_metadata_access=false",
            "spring.jpa.properties.hibernate.dialect=org.hibernate.dialect.PostgreSQLDialect",
        })
class RepositorySeamTest {

    @Autowired
    private InvoiceRepository invoices;

    @Autowired
    private InvoiceItemRepository items;

    @Autowired
    private CustomerRepository customers;

    @Autowired
    private InvoiceWriter writer;

    @Test
    void noDeclaredQueryRunsInsideATransaction() {
        // The general trap. It does not care what the offending method is called, so a future
        // @Query named count, delete, findAll or exists fails here rather than at P5 with two
        // unexplained round trips per request.
        repositories().forEach(repository ->
                Stream.of(repository.getMethods())
                        .filter(method -> method.isAnnotationPresent(Query.class))
                        .forEach(method -> assertThat(attributeOf(repositoryBean(repository), method))
                                .as("%s.%s must not inherit a transaction attribute",
                                        repository.getSimpleName(), method.getName())
                                .isNull()));
    }

    @Test
    void theOnlyRepositoryMethodsInheritingATransactionAreTheTwoSaves() {
        // Pinned as a whole map, not just for the queries: save SHOULD inherit
        // SimpleJpaRepository's writable transaction (the create path relies on joining it), and
        // any other name that starts inheriting one is a change somebody has to justify.
        List<String> transactional = repositories()
                .flatMap(repository -> Stream.of(repository.getMethods())
                        .filter(method -> attributeOf(repositoryBean(repository), method) != null)
                        .map(method -> repository.getSimpleName() + "." + method.getName()))
                .toList();

        assertThat(transactional).containsExactlyInAnyOrder(
                "InvoiceRepository.save", "InvoiceItemRepository.save");
        assertThat(attributeOf(invoices, method(InvoiceRepository.class, "save",
                io.ledgerline.data.entity.Invoice.class)).isReadOnly()).isFalse();
    }

    @Test
    void theCreatePathTransactionLivesOnInvoiceWriter() {
        // spec/db-access.md pins Q4 + Q5xN + Q6 as one transaction across three repository
        // proxies, so it can only be demarcated above them.
        TransactionAttribute create = attributeOf(writer, method(InvoiceWriter.class, "create",
                io.ledgerline.data.entity.Invoice.class, List.class));

        assertThat(create).isNotNull();
        assertThat(create.isReadOnly()).isFalse();
        assertThat(create.getPropagationBehavior())
                .isEqualTo(TransactionDefinition.PROPAGATION_REQUIRED);
        // Isolation stays DEFAULT: naming READ_COMMITTED makes Spring issue a
        // `SET TRANSACTION ISOLATION LEVEL` statement for the level PostgreSQL already defaults
        // to, which .NET never sends.
        assertThat(create.getIsolationLevel())
                .isEqualTo(TransactionDefinition.ISOLATION_DEFAULT);
    }

    private Stream<Class<?>> repositories() {
        return Stream.of(InvoiceRepository.class, InvoiceItemRepository.class,
                CustomerRepository.class);
    }

    private Object repositoryBean(Class<?> repository) {
        if (repository == InvoiceRepository.class) {
            return invoices;
        }
        return repository == InvoiceItemRepository.class ? items : customers;
    }

    private static TransactionAttribute attributeOf(Object bean, Method method) {
        Advised advised = (Advised) bean;
        for (Advisor advisor : advised.getAdvisors()) {
            if (advisor.getAdvice() instanceof TransactionInterceptor interceptor) {
                return (TransactionAttribute) interceptor.getTransactionAttributeSource()
                        .getTransactionAttribute(method, advised.getTargetSource().getTargetClass());
            }
        }
        throw new AssertionError(bean.getClass() + " carries no TransactionInterceptor");
    }

    private static Method method(Class<?> type, String name, Class<?>... parameterTypes) {
        try {
            return type.getMethod(name, parameterTypes);
        } catch (NoSuchMethodException e) {
            throw new AssertionError(e);
        }
    }
}
