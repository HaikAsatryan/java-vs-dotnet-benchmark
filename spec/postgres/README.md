# spec/postgres — shared DB tier configuration provenance

PostgreSQL 18.4, pinned by digest in infra/compose.yaml. One PG container is
shared infrastructure for both services: its configuration affects absolute
numbers, cancels in the relative comparison, and is fully disclosed here.

## Why not "the prior post's config verbatim"

The prior post (PostgreSQL write performance, same machine) tuned PG to own the
whole 62 GB / 16-core box. This benchmark boxes PG into an 8 GB / 3-core
container so the SUT's resources are accounted separately. The post's
memory-class settings are physically impossible inside this container
(shared_buffers=15GB in an 8GB cgroup). So the config is split three ways, each
setting tagged in postgresql.conf:

| tag | meaning | settings |
|---|---|---|
| [INHERITED] | verbatim from the post | fsync=on, synchronous_commit=on, wal_buffers=64MB, max_wal_size=4GB, checkpoint_completion_target=0.9, effective_io_concurrency=200, random_page_cost=1.1, huge_pages=try, statement_timeout=5s, lock_timeout=1s, idle_in_transaction_session_timeout=10s |
| [RESCALED] | same ratios, container budget | shared_buffers=2GB (~25% of 8GB), effective_cache_size=6GB (~75%), work_mem=16MB, max_connections=100 |
| [PG18-DEFAULT] | not in the post; PG 18 defaults, disclosed | io_method=worker (PG 18 AIO), io_workers, checkpoint_timeout, min_wal_size, wal_compression, autovacuum, bgwriter |

The cross-reference defense ("the DB tier is configured exactly as in the
already-published PostgreSQL post") therefore applies to the durability/WAL/
planner/timeout settings, which are the contested ones; the memory settings are
proportional rescales; everything else is stock PG 18 and says so.

`synchronous_commit = off` is never used, under any lever of the DB capacity
gate (PLAN: the prior post is the argument).

## Host artifact (this project's own decision, NOT from the post)

PGDATA on btrfs gets copy-on-write disabled (`chattr +C` / nodatacow) and
noatime. Applied once by infra/host-setup.sh on the Fedora box, shared
identically by both languages. Documented as our decision; the prior post does
not disclose its filesystem setup.

## Files

- `postgresql.conf` — the config above, mounted by compose.
- `initdb/01-role.sql` — `ledger` app role, least privilege (no DELETE; the
  workload never deletes).
- Schema is applied from `spec/schema.sql` by the seeder (single DDL source;
  not duplicated here).
