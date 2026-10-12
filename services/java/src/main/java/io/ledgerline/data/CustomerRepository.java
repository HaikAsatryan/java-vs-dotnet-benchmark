package io.ledgerline.data;

import io.ledgerline.data.entity.Customer;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.Repository;

/** Q3b/Q7 (customer existence) and Q6 (balance increment) of spec/db-access.sql. */
public interface CustomerRepository extends Repository<Customer, Long> {

    /**
     * Q3b and Q7. Native because neither JPA form reaches the frozen text ({@code existsById}
     * emits {@code count(*)}, JPQL {@code count(c) > 0} a count comparison), and deliberately NOT
     * named {@code existsById}: colliding with a {@code CrudRepository} name would inherit
     * {@code @Transactional(readOnly = true)} and turn one round trip into
     * {@code BEGIN READ ONLY / SELECT / COMMIT} (measured). RepositorySeamTest closes that trap.
     */
    @Query(value = "SELECT EXISTS(SELECT 1 FROM customers WHERE id = ?1) AS exists",
            nativeQuery = true)
    boolean customerExists(long id);

    /**
     * Q6, an ATOMIC increment: under Zipfian hot-customer contention a read-modify-write loses
     * concurrent updates and breaks the seeder invariant. Native because the JPQL bulk form
     * renders aliased and parenthesised. PINNED DEVIATION (SqlParityTest): JDBC placeholders are
     * positional, so the amount is necessarily {@code $1}, which is why it comes first.
     *
     * <p>Illegal without an ambient transaction - {@link InvoiceWriter} is the only caller.
     */
    @Modifying
    @Query(value = """
            UPDATE customers
            SET balance_minor = balance_minor + ?1
            WHERE id = ?2""", nativeQuery = true)
    void addToBalance(long amountMinor, long id);
}
