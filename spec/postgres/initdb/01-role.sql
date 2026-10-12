-- spec/postgres/initdb/01-role.sql
-- App role, least privilege. Superuser (postgres) is used only by the seeder
-- and admin tasks. Password is the dev default; compose overrides via env.
-- Runs at container init (database 'ledgerline' is created by POSTGRES_DB env).

CREATE ROLE ledger LOGIN PASSWORD 'ledger';
GRANT CONNECT ON DATABASE ledgerline TO ledger;

-- Table grants run after the seeder applies spec/schema.sql; the seeder
-- executes the following (kept here as the single documented statement set):
--   GRANT SELECT, INSERT, UPDATE ON customers, invoices, invoice_items TO ledger;
--   GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ledger;
-- No DELETE granted: the workload never deletes.
