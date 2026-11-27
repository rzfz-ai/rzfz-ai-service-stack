-- SPDX-License-Identifier: BUSL-1.1
-- Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
-- Re-domain: cognee keys its users by e-mail (users.email), and the stack
-- identity it provisions is <name>@<main domain>. After a re-domain the OIDC
-- login arrives as <name>@<new domain>; with no such user cognee MERGES nothing
-- and creates a second account, so every admin ends up twice (one on the old
-- domain, one on the new) with the data split between them. This rewrites each
-- address whose HOST is the old domain or below it, like the Authentik and
-- Open WebUI rows.
-- Run by scripts/redomain-reconcile.sh against the cognee database, fed on
-- stdin; psql vars: old_re (old domain, regex-escaped), new (new domain).
--
-- The host rule is the re-domain's own (razzfazz_common.redomain): `<old>` or
-- `<label>.<old>` as a WHOLE host, case-insensitive, so `a@mybox.local` survives
-- a `box.local` move; when the new domain is below the old one a host already
-- under it is left alone and a second run changes nothing. The local part is
-- never touched.
--
-- A row whose rewritten address ALREADY belongs to another user (the second
-- account an earlier re-domain left behind) is NOT rewritten: cognee requires
-- unique e-mails and the two accounts hold separate data, so merging them is a
-- decision for the operator. It is reported as `leftover` (fail).
-- A database without users.email is a no-op (`skipped`). One transaction.
-- Output: counts and names only, never an address:
--   rewritten users.email <rows>
--   skipped users.email
--   leftover users.email <rows>    still naming the old domain (fail)
--   <n>                            LAST line: the leftover rows
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
BEGIN
  IF old_re = '' OR new_d = '' THEN
    RAISE EXCEPTION 'old_re and new must both be set';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                  WHERE table_schema = current_schema() AND table_name = 'users'
                    AND column_name = 'email' AND data_type IN ('text', 'character varying')) THEN
    INSERT INTO rzfz_redomain VALUES (1, 'skipped users.email');
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
  EXECUTE format('UPDATE users u SET email = regexp_replace(u.email, %1$L, %2$L, ''gi'') '
                 'WHERE u.email ~* %1$L AND NOT EXISTS (SELECT 1 FROM users o WHERE o.ctid <> u.ctid '
                 'AND lower(o.email) = lower(regexp_replace(u.email, %1$L, %2$L, ''gi'')))',
                 pat, '\1' || new_d);
  GET DIAGNOSTICS n = ROW_COUNT;
  EXECUTE format('SELECT count(*) FROM users WHERE lower(email) ~ %1$L', det) INTO l;
  IF n > 0 THEN INSERT INTO rzfz_redomain VALUES (1, 'rewritten users.email ' || n); END IF;
  IF l > 0 THEN INSERT INTO rzfz_redomain VALUES (2, 'leftover users.email ' || l); END IF;
  INSERT INTO rzfz_redomain VALUES (3, l::text);
END $$;
COMMIT;
SELECT line FROM rzfz_redomain ORDER BY ord;
