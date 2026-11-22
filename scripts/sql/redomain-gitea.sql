-- SPDX-License-Identifier: BUSL-1.1
-- Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
-- Re-domain: Gitea keeps the stack identity's e-mail address in its own rows
-- ("user".email / avatar_email, email_address.email + lower_email and, when the
-- admin signed in through SSO, external_login_user.email). After a re-domain the
-- admin keeps the old-domain address: Gitea's SSO matching by e-mail and its
-- mail notifications use the stale one. This rewrites each address whose HOST is
-- the old domain or below it, like the Authentik, Open WebUI, cognee and Dify rows.
-- Run by scripts/redomain-reconcile.sh against the Gitea database, fed on
-- stdin; psql vars: old_re (old domain, regex-escaped), new (new domain).
--
-- The host rule is the re-domain's own (razzfazz_common.redomain): `<old>` or
-- `<label>.<old>` as a WHOLE host, case-insensitive, so `a@mybox.local` survives
-- a `box.local` move; when the new domain is below the old one a host already
-- under it is left alone and a second run changes nothing. The local part is
-- never touched. email_address.lower_email follows email.
--
-- A primary address whose rewritten value ALREADY belongs to another account is
-- NOT rewritten: two Gitea users are never merged here. It is reported as
-- `leftover` (fail). A table or column this Gitea lacks is skipped. One transaction.
-- Output: counts and names only, never an address:
--   rewritten <table.column> <rows>
--   skipped <table.column>
--   leftover <table.column> <rows>    still naming the old domain (fail)
--   <n>                               LAST line: the leftover rows
\set ON_ERROR_STOP on
\set QUIET on
BEGIN;
SELECT set_config('rzfz.old_re', :'old_re', true) AS rzfz_o,
       set_config('rzfz.new', :'new', true) AS rzfz_n \gset
CREATE TEMP TABLE rzfz_redomain (ord int, line text);
CREATE FUNCTION pg_temp.rzfz_col(t text, c text) RETURNS boolean LANGUAGE sql STABLE AS $f$
  SELECT EXISTS (SELECT 1 FROM information_schema.columns
                  WHERE table_schema = current_schema() AND table_name = t AND column_name = c
                    AND data_type IN ('text', 'character varying'))
$f$;
DO $$
DECLARE
  old_re  text := current_setting('rzfz.old_re');
  new_d   text := current_setting('rzfz.new');
  delim   text := '[/.@,[:space:]]';
  pre     text := '';
  pat     text;
  rep     text;
  old_l   text;
  new_l   text;
  det_pre text := '';
  det     text;
  rx      text;
  n       bigint;
  l       bigint;
  total   bigint := 0;
  k       int := 0;
  lowu    text := '';
  lowc    text := '';
  clash   text;
BEGIN
  IF old_re = '' OR new_d = '' THEN
    RAISE EXCEPTION 'old_re and new must both be set';
  END IF;
  IF new_d ~* ('\.' || old_re || '$') THEN
    pre := '(?<!(?:^|' || delim || ')'
        || regexp_replace(regexp_replace(new_d, '\.' || old_re || '$', '', 'i'),
                          '([^A-Za-z0-9])', '\\\1', 'g')
        || ')';
  END IF;
  pat := pre || '(^|' || delim || ')' || old_re || '(?=$|[:/?#,[:space:]])';
  rep := '\1' || new_d;
  old_l := lower(regexp_replace(old_re, '\\(.)', '\1', 'g'));
  new_l := lower(new_d);
  IF length(new_l) > length(old_l) + 1 AND right(new_l, length(old_l) + 1) = '.' || old_l THEN
    det_pre := '(?<!(?:^|[^a-z0-9-])'
            || regexp_replace(left(new_l, length(new_l) - length(old_l) - 1), '([^a-z0-9])', '\\\1', 'g')
            || ')';
  END IF;
  det := det_pre || '(^|[^a-z0-9-])' || regexp_replace(old_l, '([^a-z0-9])', '\\\1', 'g')
      || '($|[^a-z0-9.-])';

  -- "user": the account's primary address (a Gitea without lower_email works too)
  IF pg_temp.rzfz_col('user', 'email') THEN
    rx := format('regexp_replace(u.email, %L, %L, ''gi'')', pat, rep);
    IF pg_temp.rzfz_col('user', 'lower_email') THEN
      lowu := ', lower_email = lower(' || rx || ')';
      clash := format(' OR o.lower_email = lower(%s)', rx);
    ELSE
      clash := '';
    END IF;
    EXECUTE format('UPDATE "user" u SET email = %1$s%2$s WHERE u.email ~* %3$L '
                   'AND NOT EXISTS (SELECT 1 FROM "user" o WHERE o.id <> u.id AND (lower(o.email) = lower(%1$s)%4$s))',
                   rx, lowu, pat, clash);
    GET DIAGNOSTICS n = ROW_COUNT;
    EXECUTE format('SELECT count(*) FROM "user" WHERE lower(email) ~ %L', det) INTO l;
    IF n > 0 THEN k := k + 1; INSERT INTO rzfz_redomain VALUES (k, 'rewritten user.email ' || n); END IF;
    IF l > 0 THEN k := k + 1; INSERT INTO rzfz_redomain VALUES (k, 'leftover user.email ' || l); total := total + l; END IF;
  ELSE
    k := k + 1; INSERT INTO rzfz_redomain VALUES (k, 'skipped user.email');
  END IF;

  IF pg_temp.rzfz_col('user', 'avatar_email') THEN
    EXECUTE format('UPDATE "user" SET avatar_email = regexp_replace(avatar_email, %L, %L, ''gi'') WHERE avatar_email ~* %L',
                   pat, rep, pat);
    GET DIAGNOSTICS n = ROW_COUNT;
    EXECUTE format('SELECT count(*) FROM "user" WHERE lower(avatar_email) ~ %L', det) INTO l;
    IF n > 0 THEN k := k + 1; INSERT INTO rzfz_redomain VALUES (k, 'rewritten user.avatar_email ' || n); END IF;
    IF l > 0 THEN k := k + 1; INSERT INTO rzfz_redomain VALUES (k, 'leftover user.avatar_email ' || l); total := total + l; END IF;
  END IF;

  -- email_address: the address list (primary + secondary); lower_email is UNIQUE
  IF pg_temp.rzfz_col('email_address', 'email') AND pg_temp.rzfz_col('email_address', 'lower_email') THEN
    rx := format('regexp_replace(e.email, %L, %L, ''gi'')', pat, rep);
    EXECUTE format('UPDATE email_address e SET email = %1$s, lower_email = lower(%1$s) '
                   'WHERE (e.email ~* %2$L OR e.lower_email ~* %2$L) '
                   'AND NOT EXISTS (SELECT 1 FROM email_address o WHERE o.id <> e.id AND o.lower_email = lower(%1$s))',
                   rx, pat);
    GET DIAGNOSTICS n = ROW_COUNT;
    EXECUTE format('SELECT count(*) FROM email_address WHERE lower(email) ~ %1$L OR lower(lower_email) ~ %1$L', det) INTO l;
    IF n > 0 THEN k := k + 1; INSERT INTO rzfz_redomain VALUES (k, 'rewritten email_address.email ' || n); END IF;
    IF l > 0 THEN k := k + 1; INSERT INTO rzfz_redomain VALUES (k, 'leftover email_address.email ' || l); total := total + l; END IF;
  ELSE
    k := k + 1; INSERT INTO rzfz_redomain VALUES (k, 'skipped email_address.email');
  END IF;

  -- external_login_user: the address the SSO provider reported at the last sign-in
  IF pg_temp.rzfz_col('external_login_user', 'email') THEN
    EXECUTE format('UPDATE external_login_user SET email = regexp_replace(email, %L, %L, ''gi'') WHERE email ~* %L',
                   pat, rep, pat);
    GET DIAGNOSTICS n = ROW_COUNT;
    EXECUTE format('SELECT count(*) FROM external_login_user WHERE lower(email) ~ %L', det) INTO l;
    IF n > 0 THEN k := k + 1; INSERT INTO rzfz_redomain VALUES (k, 'rewritten external_login_user.email ' || n); END IF;
    IF l > 0 THEN k := k + 1; INSERT INTO rzfz_redomain VALUES (k, 'leftover external_login_user.email ' || l); total := total + l; END IF;
  END IF;

  k := k + 1; INSERT INTO rzfz_redomain VALUES (k, total::text);
END $$;
COMMIT;
SELECT line FROM rzfz_redomain ORDER BY ord;
