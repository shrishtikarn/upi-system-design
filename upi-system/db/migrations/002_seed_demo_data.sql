-- =============================================================================
-- 002_seed_demo_data.sql  -  demo + load-test accounts (development only)
--
--   * 3 named demo users for the live demo (alice, bob, chai point merchant)
--   * 1000 load-test users  user0001@mockbank .. user1000@mockbank,
--     each with Rs 10,00,000 so the load test never runs out of money
-- =============================================================================

WITH u AS (
    INSERT INTO users (full_name, phone) VALUES
        ('Alice Sharma',        '9000000001'),
        ('Bob Mehta',           '9000000002'),
        ('Chai Point Merchant', '9000000003')
    RETURNING id, phone
), a AS (
    INSERT INTO bank_accounts (user_id, bank_name, ifsc, account_last4, balance_paise)
    SELECT id,
           CASE phone WHEN '9000000001' THEN 'Axis Bank' WHEN '9000000002' THEN 'State Bank of India' ELSE 'ICICI Bank' END,
           CASE phone WHEN '9000000001' THEN 'UTIB0000001' WHEN '9000000002' THEN 'SBIN0000001' ELSE 'ICIC0000001' END,
           right(phone, 4),
           CASE phone WHEN '9000000003' THEN 0 ELSE 5000000 END          -- Rs 50,000 for alice and bob
    FROM u
    RETURNING id, user_id
)
INSERT INTO vpas (vpa, user_id, account_id)
SELECT CASE u.phone WHEN '9000000001' THEN 'alice@okaxis'
                    WHEN '9000000002' THEN 'bob@oksbi'
                    ELSE 'chaipoint@okicici' END,
       u.id, a.id
FROM u JOIN a ON a.user_id = u.id;

WITH u AS (
    INSERT INTO users (full_name, phone)
    SELECT 'Load User ' || lpad(g::text, 4, '0'), '8' || lpad(g::text, 9, '0')
    FROM generate_series(1, 1000) AS g
    RETURNING id, phone
), a AS (
    INSERT INTO bank_accounts (user_id, bank_name, ifsc, account_last4, balance_paise)
    SELECT id, 'Mock Bank', 'MOCK0000001', right(phone, 4), 100000000            -- Rs 10,00,000
    FROM u
    RETURNING id, user_id
)
INSERT INTO vpas (vpa, user_id, account_id)
SELECT 'user' || right(u.phone, 4) || '@mockbank', u.id, a.id
FROM u JOIN a ON a.user_id = u.id;
