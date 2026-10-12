-- spec/schema.sql
-- Canonical DDL for Ledgerline, and the only DDL source. Consumed VERBATIM by:
-- the seeder (seed/, `--schema`, which applies it before COPY), the EF model
-- assertion tests (services/dotnet/Ledgerline.Tests/EfSchemaMappingTests.cs),
-- and the Java mapping + live-endpoint tests
-- (services/java .../data/SchemaMappingTest.java, .../web/LiveEndpointTest.java).
-- Money: bigint minor units end-to-end. Time: timestamptz. PostgreSQL 18.4.
--
-- ID GENERATION (frozen):
--   customers.id      app-assigned (seeder writes explicit ids 1..N; dense
--                     keyspace required for deterministic Zipf selection).
--   invoices.id       GENERATED ALWAYS AS IDENTITY. POST /invoices returns the
--                     new id via INSERT ... RETURNING id: one-round-trip id
--                     allocation on BOTH stacks (Hibernate emits
--                     `... returning id,created_at` under
--                     use_get_generated_keys=false; EF SaveChanges reads the key
--                     back in the same batched round-trip). App-generated ids
--                     rejected: id-generation cost would differ in kind between
--                     languages. The item inserts are NOT symmetric -- see
--                     spec/db-access.md's round-trip ledger.
--   invoice_items.id  GENERATED ALWAYS AS IDENTITY (never returned/queried).
--
-- Seeder note: COPY may write explicit ids into IDENTITY columns; after COPY
-- the seeder must run, for each identity:
--   ALTER TABLE invoices      ALTER COLUMN id RESTART WITH <seeded_max+1>;
--   ALTER TABLE invoice_items ALTER COLUMN id RESTART WITH <seeded_max+1>;
-- Without the restart, runtime POSTs collide with seeded ids.

CREATE TABLE customers (
    id            bigint        PRIMARY KEY,
    name          text          NOT NULL,
    email         text          NOT NULL,
    balance_minor bigint        NOT NULL DEFAULT 0,  -- = sum(invoices.total_minor) per customer (seed invariant)
    currency      char(3)       NOT NULL,            -- ISO 4217
    created_at    timestamptz   NOT NULL
);

CREATE TABLE invoices (
    id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id bigint      NOT NULL REFERENCES customers(id),
    status      text        NOT NULL,                -- 'draft'|'open'|'paid'|'void' (by convention)
    currency    char(3)     NOT NULL,
    total_minor bigint      NOT NULL,                -- = sum(invoice_items.line_total_minor)
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE invoice_items (
    id               bigint  GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    invoice_id       bigint  NOT NULL REFERENCES invoices(id),
    sku              text    NOT NULL,               -- ^[A-Z]{2}-[0-9]{4}$ (spec/pricing-algorithm.md)
    description      text    NOT NULL,
    qty              int     NOT NULL,
    unit_price_minor bigint  NOT NULL,
    line_total_minor bigint  NOT NULL                -- = qty * unit_price_minor
);

-- INDEXES: each justified against an endpoint; nothing speculative.
--
-- (1) customers PK: GET /invoices/{id} 404 probe, balance UPDATE by id.
-- (2) invoices PK: GET /invoices/{id}.
-- (3) The list-page index. GET /customers/{id}/invoices?page=N is
--     WHERE customer_id=$1 ORDER BY id DESC LIMIT 20 OFFSET (N-1)*20.
--     (customer_id, id DESC) makes it a pure backward index range scan, no
--     sort node. 25% of the mix; load-bearing for fairness: EF-generated and
--     Hibernate-generated SQL must hit the SAME plan (P5 confirms it with
--     EXPLAIN; spec/db-access.md).
CREATE INDEX idx_invoices_customer_id_id_desc
    ON invoices (customer_id, id DESC);

-- (4) Items fetch for GET /invoices/{id}: WHERE invoice_id=$1 ORDER BY id.
CREATE INDEX idx_invoice_items_invoice_id
    ON invoice_items (invoice_id, id);

-- NO index on invoices.status / invoices.created_at / customers.email / sku:
-- the workload never filters or sorts by them; adding any would only inflate
-- write cost. Explicitly excluded.
