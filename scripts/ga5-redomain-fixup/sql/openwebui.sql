-- SPDX-License-Identifier: BUSL-1.1
-- Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
-- 2026.09-ga.5 re-domain fixup (#2932): Open WebUI (openwebui_db).
-- ga.5 schema (measured on a 2026.09-ga.5 box): config(key, value json) key-value rows that
-- OVERRIDE the env (webui.url …); "user"(id, email); auth(id, email), auth.id = "user".id.
-- An account is moved only when its new address is free in BOTH tables: two
-- accounts are never merged (the row stays and is counted in `after`).
BEGIN;
SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '120s';
INSERT INTO rz_out (line) SELECT 'before openwebui.config ' || count(*) FROM config WHERE pg_temp.rz_left(value::text);
INSERT INTO rz_out (line) SELECT 'before openwebui.user ' || count(*) FROM "user" WHERE pg_temp.rz_old_email(email);
INSERT INTO rz_out (line) SELECT 'before openwebui.auth ' || count(*) FROM auth WHERE pg_temp.rz_old_email(email);

WITH c AS (
  UPDATE config SET value = pg_temp.rz_text(value::text)::json
   WHERE pg_temp.rz_has(value::text) RETURNING 1)
INSERT INTO rz_out (line) SELECT 'changed openwebui.config ' || count(*) FROM c;

WITH c AS (
  UPDATE "user" u SET email = pg_temp.rz_new_email(u.email)
   WHERE pg_temp.rz_old_email(u.email)
     AND NOT EXISTS (SELECT 1 FROM "user" o WHERE o.id <> u.id AND lower(o.email) = lower(pg_temp.rz_new_email(u.email)))
     AND NOT EXISTS (SELECT 1 FROM auth o WHERE o.id <> u.id AND lower(o.email) = lower(pg_temp.rz_new_email(u.email)))
  RETURNING 1)
INSERT INTO rz_out (line) SELECT 'changed openwebui.user ' || count(*) FROM c;

WITH c AS (
  UPDATE auth a SET email = pg_temp.rz_new_email(a.email)
   WHERE pg_temp.rz_old_email(a.email)
     AND NOT EXISTS (SELECT 1 FROM auth o WHERE o.id <> a.id AND lower(o.email) = lower(pg_temp.rz_new_email(a.email)))
     AND NOT EXISTS (SELECT 1 FROM "user" o WHERE o.id <> a.id AND lower(o.email) = lower(pg_temp.rz_new_email(a.email)))
  RETURNING 1)
INSERT INTO rz_out (line) SELECT 'changed openwebui.auth ' || count(*) FROM c;

INSERT INTO rz_out (line) SELECT 'after openwebui.config ' || count(*) FROM config WHERE pg_temp.rz_left(value::text);
INSERT INTO rz_out (line) SELECT 'after openwebui.user ' || count(*) FROM "user" WHERE pg_temp.rz_old_email(email);
INSERT INTO rz_out (line) SELECT 'after openwebui.auth ' || count(*) FROM auth WHERE pg_temp.rz_old_email(email);
SELECT line FROM rz_out ORDER BY ord;
\if :apply
COMMIT;
\else
ROLLBACK;
\endif
