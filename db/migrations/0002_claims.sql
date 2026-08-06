-- 0002_claims.sql
-- Audit trail for fenced ownership handoffs on work_claims (lease expiry,
-- future cancellation). See db/claims.py and PROJECT.md §4.2.

CREATE TABLE ownership_transfers (
  transfer_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  work_key     STRING NOT NULL,
  from_owner   STRING NOT NULL,
  to_owner     STRING NOT NULL,
  from_fence   INT8   NOT NULL,
  to_fence     INT8   NOT NULL,
  reason       STRING NOT NULL,             -- lease_expired|prior_terminal
  at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  INDEX (work_key, at DESC)
);
