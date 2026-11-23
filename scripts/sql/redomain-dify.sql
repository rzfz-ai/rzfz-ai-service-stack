-- SPDX-License-Identifier: BUSL-1.1
-- Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
-- Re-domain: Dify keys its accounts by e-mail (accounts.email), and the stack
-- identity it provisions is <name>@<main domain>. After a re-domain the
-- Authentik SSO login arrives as <name>@<new domain>; an admin left on the old
-- domain never matches, so the SSO login cannot reach it. This rewrites each
-- address whose HOST is the old domain or below it, like the Authentik, Open
-- WebUI and cognee rows.
-- Run by scripts/redomain-reconcile.sh against the Dify database, fed on
-- stdin; psql vars: old_re (old domain, regex-escaped), new (new domain).
--
-- The host rule is the re-domain's own (razzfazz_common.redomain): `<old>` or
-- `<label>.<old>` as a WHOLE host, case-insensitive, so `a@mybox.local` survives
-- a `box.local` move; when the new domain is below the old one a host already
-- under it is left alone and a second run changes nothing. The local part is
-- never touched.
--
-- A row whose rewritten address ALREADY belongs to another account is NOT
-- rewritten: the two accounts own separate workspaces, so merging them is a
-- decision for the operator. It is reported as `leftover` (fail).
-- accounts.normalized_email (Dify 1.17) is rewritten in the same statement when the
-- column exists and is compared in the collision check; a schema without it works.
-- A database without accounts.email is a no-op (`skipped`). One transaction.
-- Output: counts and names only, never an address:
--   rewritten accounts.email <rows>
--   skipped accounts.email
--   leftover accounts.email <rows>    still naming the old domain (fail)
--   <n>                               LAST line: the leftover rows
\set ON_ERROR_STOP on
\set QUIET on
BEGIN;
SELECT set_config('rzfz.old_re', :'old_re', true) AS rzfz_o,
       set_config('rzfz.new', :'new', true) AS rzfz_n \gset
CREATE TEMP TABLE rzfz_redomain (ord int, line text);
DO $$
DECLARE
  old_re text := current_setting('rzfz.old_re');
  new_d  text := current_setting('rzfz.new');
  delim  text := '[/.@,[:space:]]';
  pre    text := '';
  pat    text;
  old_l  text;
  new_l  text;
  det_pre text := '';
  det    text;
  n      bigint;
  l      bigint := 0;
  has_norm boolean;
  re_e   text;
  re_n   text;
BEGIN
  IF old_re = '' OR new_d = '' THEN
    RAISE EXCEPTION 'old_re and new must both be set';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                  WHERE table_schema = current_schema() AND table_name = 'accounts'
                    AND column_name = 'email' AND data_type IN ('text', 'character varying')) THEN
    INSERT INTO rzfz_redomain VALUES (1, 'skipped accounts.email');
    INSERT INTO rzfz_redomain VALUES (2, '0');
    RETURN;
  END IF;
  IF new_d ~* ('\.' || old_re || '$') THEN
    pre := '(?<!(?:^|' || delim || ')'
        || regexp_replace(regexp_replace(new_d, '\.' || old_re || '$', '', 'i'),
                          '([^A-Za-z0-9])', '\\\1', 'g')
        || ')';
  END IF;
  pat := pre || '(^|' || delim || ')' || old_re || '(?=$|[:/?#,[:space:]])';
  old_l := lower(regexp_replace(old_re, '\\(.)', '\1', 'g'));
  new_l := lower(new_d);
  IF length(new_l) > length(old_l) + 1 AND right(new_l, length(old_l) + 1) = '.' || old_l THEN
    det_pre := '(?<!(?:^|[^a-z0-9-])'
            || regexp_replace(left(new_l, length(new_l) - length(old_l) - 1), '([^a-z0-9])', '\\\1', 'g')
            || ')';
  END IF;
  det := det_pre || '(^|[^a-z0-9-])' || regexp_replace(old_l, '([^a-z0-9])', '\\\1', 'g')
      || '($|[^a-z0-9.-])';
  -- Dify 1.17 also keeps accounts.normalized_email; rewrite it in the same
  -- statement when the column exists, and let it take part in the collision check.
  has_norm := EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_schema = current_schema() AND table_name = 'accounts'
                         AND column_name = 'normalized_email'
                         AND data_type IN ('text', 'character varying'));
  re_e := format('regexp_replace(u.email, %1$L, %2$L, ''gi'')', pat, '\1' || new_d);
  IF has_norm THEN
    re_n := format('regexp_replace(u.normalized_email, %1$L, %2$L, ''gi'')', pat, '\1' || new_d);
    EXECUTE format('UPDATE accounts u SET email = %3$s, normalized_email = %4$s '
                   'WHERE (u.email ~* %1$L OR u.normalized_email ~* %1$L) '
                   'AND NOT EXISTS (SELECT 1 FROM accounts o WHERE o.ctid <> u.ctid AND ('
                   '(%3$s IS DISTINCT FROM u.email AND lower(o.email) = lower(%3$s)) OR '
                   '(%4$s IS DISTINCT FROM u.normalized_email AND lower(o.normalized_email) = lower(%4$s))))',
                   pat, '\1' || new_d, re_e, re_n);
  ELSE
    EXECUTE format('UPDATE accounts u SET email = %3$s '
                   'WHERE u.email ~* %1$L AND NOT EXISTS (SELECT 1 FROM accounts o WHERE o.ctid <> u.ctid '
                   'AND lower(o.email) = lower(%3$s))',
                   pat, '\1' || new_d, re_e);
  END IF;
  GET DIAGNOSTICS n = ROW_COUNT;
  IF has_norm THEN
    EXECUTE format('SELECT count(*) FROM accounts WHERE lower(email) ~ %1$L OR lower(normalized_email) ~ %1$L', det) INTO l;
  ELSE
    EXECUTE format('SELECT count(*) FROM accounts WHERE lower(email) ~ %1$L', det) INTO l;
  END IF;
  IF n > 0 THEN INSERT INTO rzfz_redomain VALUES (1, 'rewritten accounts.email ' || n); END IF;
  IF l > 0 THEN INSERT INTO rzfz_redomain VALUES (2, 'leftover accounts.email ' || l); END IF;
  INSERT INTO rzfz_redomain VALUES (3, l::text);
END $$;
COMMIT;
SELECT line FROM rzfz_redomain ORDER BY ord;
