-- SPDX-License-Identifier: BUSL-1.1
-- Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
-- 2026.09-ga.5 re-domain fixup (#2932): cognee (cognee_db).
-- cognee keys its users by e-mail (users.email, unique). After a set-domain the
-- first start / OIDC login on the new domain can create a SECOND admin
-- (<name>@<new>) while the original (<name>@<old>) keeps the data and the MCP
-- API key. Per old-domain user:
--   * no account on the new address yet  -> rename it;
--   * a duplicate exists                 -> count what EACH owns (every column
--     that references principals/users by FK plus every owner_id / user_id /
--     principal_id / parent_user_id / created_by column) and delete the one
--     that owns NOTHING (DELETE FROM principals cascades to users), keeping the
--     other on the new address; when neither owns anything, the original is kept;
--   * BOTH own rows                      -> stop: NOTHING is changed in cognee
--     (`stop` lines), the operator decides with support.
-- Deleting only an account that owns 0 rows is FK-safe: no NO ACTION reference
-- (acls, tenants, user_roles, user_tenants) can point at it.
BEGIN;
SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '120s';
INSERT INTO rz_out (line) SELECT 'before cognee.users ' || count(*) FROM users WHERE pg_temp.rz_old_email(email);
DO $$
DECLARE
  c        record;
  r        record;
  q        text := '';
  v_other  uuid;
  v_oo     bigint;
  v_onn    bigint;
  n        bigint;
  renamed  bigint := 0;
  deleted  bigint := 0;
BEGIN
  FOR c IN
    SELECT DISTINCT s.tn, s.cn FROM (
      SELECT table_name::text AS tn, column_name::text AS cn FROM information_schema.columns
       WHERE table_schema = current_schema()
         AND column_name IN ('owner_id', 'user_id', 'principal_id', 'parent_user_id', 'created_by')
      UNION
      SELECT kcu.table_name::text, kcu.column_name::text
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema
       WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = current_schema()
         AND ccu.table_name IN ('principals', 'users') AND ccu.column_name = 'id'
    ) s
    JOIN information_schema.tables t
      ON t.table_schema = current_schema() AND t.table_name = s.tn AND t.table_type = 'BASE TABLE'
    WHERE NOT (s.tn = 'users' AND s.cn = 'id')
    ORDER BY 1, 2
  LOOP
    q := q || CASE WHEN q = '' THEN '' ELSE ' + ' END
           || format('(SELECT count(*) FROM %I WHERE %I::text = $1::text)', c.tn, c.cn);
  END LOOP;
  IF q = '' THEN
    RAISE EXCEPTION 'cognee: no owner columns found, refusing to judge duplicates';
  END IF;

  CREATE TEMP TABLE rz_cg (id uuid, email text, ne text, other uuid, oo bigint, onn bigint) ON COMMIT DROP;
  FOR r IN SELECT u.id, u.email, pg_temp.rz_new_email(u.email) AS ne FROM users u
            WHERE pg_temp.rz_old_email(u.email) ORDER BY u.email LOOP
    v_other := NULL; v_oo := NULL; v_onn := NULL;
    SELECT u2.id INTO v_other FROM users u2 WHERE lower(u2.email) = lower(r.ne) AND u2.id <> r.id LIMIT 1;
    IF v_other IS NOT NULL THEN
      EXECUTE 'SELECT ' || q INTO v_oo USING r.id;
      EXECUTE 'SELECT ' || q INTO v_onn USING v_other;
    END IF;
    INSERT INTO rz_cg VALUES (r.id, r.email, r.ne, v_other, v_oo, v_onn);
  END LOOP;

  IF EXISTS (SELECT 1 FROM rz_cg WHERE other IS NOT NULL AND oo > 0 AND onn > 0) THEN
    INSERT INTO rz_out (line)
      SELECT 'stop cognee.users ' || email || ' ' || oo || ' ' || ne || ' ' || onn
        FROM rz_cg WHERE other IS NOT NULL AND oo > 0 AND onn > 0;
    INSERT INTO rz_out (line) VALUES ('changed cognee.users 0'), ('changed cognee.duplicates 0');
    RETURN;   -- nothing is changed in cognee
  END IF;

  FOR r IN SELECT * FROM rz_cg ORDER BY email LOOP
    IF r.other IS NULL THEN
      UPDATE users SET email = r.ne WHERE id = r.id;
      renamed := renamed + 1;
    ELSIF r.oo = 0 AND r.onn > 0 THEN
      -- the original owns nothing: remove it, the account on the new address stays
      DELETE FROM principals WHERE id = r.id AND type = 'user';
      GET DIAGNOSTICS n = ROW_COUNT;
      IF n <> 1 THEN RAISE EXCEPTION 'cognee: could not delete the empty account %', r.email; END IF;
      deleted := deleted + 1;
      INSERT INTO rz_out (line) VALUES ('note cognee.deleted ' || r.email || ' 0 keep ' || r.ne || ' ' || r.onn);
    ELSE
      -- the duplicate on the new address owns nothing: remove it, move the original
      DELETE FROM principals WHERE id = r.other AND type = 'user';
      GET DIAGNOSTICS n = ROW_COUNT;
      IF n <> 1 THEN RAISE EXCEPTION 'cognee: could not delete the empty duplicate %', r.ne; END IF;
      UPDATE users SET email = r.ne WHERE id = r.id;
      deleted := deleted + 1;
      renamed := renamed + 1;
      INSERT INTO rz_out (line) VALUES ('note cognee.deleted ' || r.ne || ' 0 keep ' || r.email || ' ' || r.oo);
    END IF;
  END LOOP;
  INSERT INTO rz_out (line) VALUES ('changed cognee.users ' || renamed), ('changed cognee.duplicates ' || deleted);
END $$;
INSERT INTO rz_out (line) SELECT 'after cognee.users ' || count(*) FROM users WHERE pg_temp.rz_old_email(email);
SELECT line FROM rz_out ORDER BY ord;
\if :apply
COMMIT;
\else
ROLLBACK;
\endif
