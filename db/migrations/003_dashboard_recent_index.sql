-- =============================================================================
-- 003_dashboard_recent_index.sql
--
-- The live dashboard asks for "the latest N transactions" every 2 seconds.
-- Without this index Postgres would sort the whole transactions table each
-- time; with it, it reads the newest N rows straight from the index.
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_txn_created ON transactions (created_at DESC);
