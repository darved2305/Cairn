-- 0007_cost_rates_seed.sql
-- Seed the published AWS Fargate on-demand rates the console's Savings strip
-- derives its one non-measured number from (PROJECT.md §5.4: "Derived, and
-- labelled `rate-based`: cost = duration_s × rate_usd_per_second, where rates
-- live in a cost_rates table seeded with published AWS Fargate on-demand
-- us-east-1 pricing and editable by the user").
--
-- Why a migration and not just agent/loop.py::_ensure_cost_rates: that
-- function seeds these same two rows with these same two values, but only on
-- the path of a real `cairn run`. The console is read-only by construction —
-- console/queries.py contains no INSERT, and once
-- 0008_console_readonly_role.sql is wired the console's SQL role cannot write
-- at all — so a cluster that has only ever served the console would show
-- "no rate on record" forever. Seeding here makes the rate a property of the
-- schema's setup rather than a side effect of having run compute.
--
-- These are published list prices with a recorded source, not measurements
-- and not estimates. They are ON CONFLICT DO NOTHING so a user who has
-- already edited their own rates (the table is explicitly user-editable)
-- keeps them.

INSERT INTO cost_rates (resource_kind, usd, source_note) VALUES
  ('fargate_vcpu_hour', 0.04048,  'AWS published on-demand us-east-1, 2026-08'),
  ('fargate_gb_hour',   0.004445, 'AWS published on-demand us-east-1, 2026-08')
ON CONFLICT (resource_kind) DO NOTHING;
