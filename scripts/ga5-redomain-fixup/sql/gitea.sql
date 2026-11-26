-- SPDX-License-Identifier: BUSL-1.1
-- Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
-- 2026.09-ga.5 re-domain fixup (#2932, also the #2928 gap): Gitea (gitea_db).
-- ga.5 schema (measured on a 2026.09-ga.5 box): "user"(email, avatar_email),
-- email_address(email, lower_email UNIQUE), login_source(cfg text, JSON),
-- external_login_user(email). A primary address whose new value is already
-- taken by another user is NOT moved (it stays in `after`).
BEGIN;
SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '120s';
INSERT INTO rz_out (line) SELECT 'before gitea.user ' || count(*) FROM "user" WHERE pg_temp.rz_old_email(email);
INSERT INTO rz_out (line) SELECT 'before gitea.user.avatar_email ' || count(*) FROM "user" WHERE pg_temp.rz_old_email(avatar_email);
INSERT INTO rz_out (line) SELECT 'before gitea.email_address ' || count(*) FROM email_address WHERE pg_temp.rz_old_email(email) OR pg_temp.rz_old_email(lower_email);
INSERT INTO rz_out (line) SELECT 'before gitea.login_source ' || count(*) FROM login_source WHERE pg_temp.rz_left(cfg);
INSERT INTO rz_out (line) SELECT 'before gitea.external_login_user ' || count(*) FROM external_login_user WHERE pg_temp.rz_old_email(email);

WITH c AS (
  UPDATE "user" u SET email = pg_temp.rz_new_email(u.email)
   WHERE pg_temp.rz_old_email(u.email)
     AND NOT EXISTS (SELECT 1 FROM "user" o WHERE o.id <> u.id AND lower(o.email) = lower(pg_temp.rz_new_email(u.email)))
  RETURNING 1)
INSERT INTO rz_out (line) SELECT 'changed gitea.user ' || count(*) FROM c;

WITH c AS (
  UPDATE "user" SET avatar_email = pg_temp.rz_new_email(avatar_email)
   WHERE pg_temp.rz_old_email(avatar_email) RETURNING 1)
INSERT INTO rz_out (line) SELECT 'changed gitea.user.avatar_email ' || count(*) FROM c;

WITH c AS (
  UPDATE email_address e
     SET email = pg_temp.rz_new_email(e.email), lower_email = lower(pg_temp.rz_new_email(e.email))
   WHERE (pg_temp.rz_old_email(e.email) OR pg_temp.rz_old_email(e.lower_email))
     AND NOT EXISTS (SELECT 1 FROM email_address o WHERE o.id <> e.id
                      AND o.lower_email = lower(pg_temp.rz_new_email(e.email)))
  RETURNING 1)
INSERT INTO rz_out (line) SELECT 'changed gitea.email_address ' || count(*) FROM c;

WITH c AS (
  UPDATE login_source SET cfg = pg_temp.rz_text(cfg) WHERE pg_temp.rz_has(cfg) RETURNING 1)
INSERT INTO rz_out (line) SELECT 'changed gitea.login_source ' || count(*) FROM c;

WITH c AS (
  UPDATE external_login_user SET email = pg_temp.rz_new_email(email)
   WHERE pg_temp.rz_old_email(email) RETURNING 1)
INSERT INTO rz_out (line) SELECT 'changed gitea.external_login_user ' || count(*) FROM c;

INSERT INTO rz_out (line) SELECT 'after gitea.user ' || count(*) FROM "user" WHERE pg_temp.rz_old_email(email);
INSERT INTO rz_out (line) SELECT 'after gitea.user.avatar_email ' || count(*) FROM "user" WHERE pg_temp.rz_old_email(avatar_email);
INSERT INTO rz_out (line) SELECT 'after gitea.email_address ' || count(*) FROM email_address WHERE pg_temp.rz_old_email(email) OR pg_temp.rz_old_email(lower_email);
INSERT INTO rz_out (line) SELECT 'after gitea.login_source ' || count(*) FROM login_source WHERE pg_temp.rz_left(cfg);
INSERT INTO rz_out (line) SELECT 'after gitea.external_login_user ' || count(*) FROM external_login_user WHERE pg_temp.rz_old_email(email);
SELECT line FROM rz_out ORDER BY ord;
\if :apply
COMMIT;
\else
ROLLBACK;
\endif
