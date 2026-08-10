-- 0014_flight_receipt_console_grants.sql
-- Day 6 receipt/explain HTTP path needs composite leaf tables that 0012
-- omitted. Without these, cairn_console_ro hits InsufficientPrivilege on
-- /api/flight/receipt/{id} while the worker CLI (admin URL) succeeds.
-- GRANT is idempotent.

GRANT SELECT ON TABLE composite_derivations TO cairn_console_ro;
GRANT SELECT ON TABLE derivation_fragments  TO cairn_console_ro;
GRANT SELECT ON TABLE fragment_commits      TO cairn_console_ro;
