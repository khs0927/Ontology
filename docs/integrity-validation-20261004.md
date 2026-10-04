# Derived-data integrity validation (2026-10-04)

Implementation baseline: Ontology `7b138139` (#64). Work is isolated from the running ingestion/embedding checkout.

- KG cache keys now include complete steel catalog values and byte provenance, including legacy DAT files. Document byte hashes and parser revisions both invalidate project projections.
- ArchOntos references become stale after either a byte change or a parser revision change, or when the referenced current revision cannot be resolved. Stale evaluation outcomes become `REVIEW`; the exported outcome is retained for audit. A locator cannot grant another project's scope. Synthetic test rules verify the contract only and make no claim about real legal compliance.
- Facts from partial drawing coverage report observed storey labels/bounds. They do not certify total building floor or basement counts; rules requiring those totals remain `REVIEW` until authoritative evidence is provided.
- Re-embedding selects missing or old-revision vectors and repairs `index_state` only for documents whose searchable objects all have the active model's current-revision vectors. Partial chunks, endpoint failures and dry runs cannot advertise completion. A zero-work resume can repair stale metadata.

Validation: 88 targeted tests passed; 7 PostgreSQL integration cases were skipped because no disposable test DSN was supplied. Ruff passed for all `src`, `tests` and `extensions`; `git diff --check` passed. Offline regressions use in-memory database adapters and mocked endpoints. Live database migrations, inference, worker scheduling, Docker runtime and real legal datasets are outside this change. PostgreSQL integration cases remain for CI's disposable database, never the live store.
