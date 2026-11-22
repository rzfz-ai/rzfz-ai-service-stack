-- SPDX-License-Identifier: BUSL-1.1
-- Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
-- 2026.09-ga.5 re-domain fixup (#2932): Dify (dify_db).
-- ga.5 schema (measured on a 2026.09-ga.5 box): accounts(email, normalized_email). Dify keys
-- the account by e-mail; the Authentik SSO login arrives with the new domain.
-- An account whose new address already belongs to another account is NOT moved
-- (two workspaces are never merged); it stays in `after`.
BEGIN;
SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '120s';
INSERT INTO rz_out (line) SELECT 'before dify.accounts ' || count(*) FROM accounts
 WHERE pg_temp.rz_old_email(email) OR pg_temp.rz_old_email(normalized_email);

WITH c AS (
  UPDATE accounts u
     SET email = CASE WHEN pg_temp.rz_old_email(u.email) THEN pg_temp.rz_new_email(u.email) ELSE u.email END,
         normalized_email = CASE WHEN pg_temp.rz_old_email(u.normalized_email)
                                 THEN pg_temp.rz_new_email(u.normalized_email) ELSE u.normalized_email END
   WHERE (pg_temp.rz_old_email(u.email) OR pg_temp.rz_old_email(u.normalized_email))
     AND NOT EXISTS (SELECT 1 FROM accounts o WHERE o.id <> u.id AND (
           lower(o.email) = lower(pg_temp.rz_new_email(u.email))
        OR lower(o.normalized_email) = lower(pg_temp.rz_new_email(coalesce(u.normalized_email, u.email)))))
  RETURNING 1)
INSERT INTO rz_out (line) SELECT 'changed dify.accounts ' || count(*) FROM c;

INSERT INTO rz_out (line) SELECT 'after dify.accounts ' || count(*) FROM accounts
 WHERE pg_temp.rz_old_email(email) OR pg_temp.rz_old_email(normalized_email);
SELECT line FROM rz_out ORDER BY ord;
\if :apply
COMMIT;
\else
ROLLBACK;
\endif
