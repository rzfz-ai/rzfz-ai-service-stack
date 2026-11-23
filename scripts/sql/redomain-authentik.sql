-- SPDX-License-Identifier: BUSL-1.1
-- Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
-- Re-domain: the Authentik rows that keep the old host after .env moved and
-- the blueprints re-applied. Run by scripts/redomain-reconcile.sh, fed on stdin.
-- Targets (Authentik 2026.5 model fields):
--   required: proxyprovider.external_host, outpost._config, oauth2provider._redirect_uris
--   expected (skipped with a `skipped` line when absent): proxyprovider.cookie_domain,
--             core_user.email (the admin's address, #2928; other domains stay),
--             application.meta_launch_url (reported only: `warn`)
--   optional: oauth2provider.redirect_uris (the pre-2024.10 text column)
-- Regex-mode redirect URIs are never rewritten: they are counted (`warn-regex`)
-- for the operator to edit in the Authentik admin UI.
--
-- psql vars: old_re (the old domain, regex-escaped), new (the new domain).
--
-- REWRITE rule (the same as razzfazz_common.redomain): a host matches only as
-- a WHOLE host, `<old>` or `<label>.<old>`, preceded by start, '/', '.', '@',
-- '"', ',' or whitespace and followed by end, ':', '/', '?', '#', '"', ',',
-- '\' (JSON-escaped `\/` and nested `\"` strings) or whitespace,
-- case-insensitive. `mybox.local` survives a `box.local` re-domain. When the
-- new domain is a subdomain of the old one, a host already under the new
-- domain is skipped, so a second run changes nothing.
--
-- LEFTOVER detector, deliberately NOT the rewrite pattern: the column text is
-- lower-cased with every backslash removed (so `\/`, `\"` and regex `\.`
-- forms read plainly), then searched for the old domain bounded by anything
-- that cannot be part of a host label. It sees what the rewrite chose not to
-- touch (a host after `=`, inside quotes or brackets, a regex URI).
--
-- One transaction: the target check runs first, so a REQUIRED table or column
-- this version lacks rewrites nothing (psql prints `WARNING: missing
-- <table>.<column>` per target and exits 3). Any error rolls every table back.
-- Output on stdout, counts and names only, never row content:
--   rewritten <t.c> <rows>        rows changed
--   skipped <t.c>                 an optional target this version lacks
--   leftover <t.c> <rows>         rows that still name the old domain (fail)
--   warn <t.c> <rows>             same, in a column that is only reported
--   warn-regex <t.c> <uris> <rows>  regex-mode redirect URIs (never rewritten)
--   <n>                           LAST line: the sum of the `leftover` rows
\set ON_ERROR_STOP on
\set QUIET on
BEGIN;
SELECT set_config('rzfz.old_re', :'old_re', true) AS rzfz_o,
       set_config('rzfz.new', :'new', true) AS rzfz_n \gset
CREATE TEMP TABLE rzfz_redomain (ord int, line text);
DO $$
DECLARE
  old_re  text := current_setting('rzfz.old_re');
  new_d   text := current_setting('rzfz.new');
  delim   text := '[/.@",[:space:]]';
  pre     text := '';
  pat     text;
  rep     text;
  old_l   text;
  new_l   text;
  det_pre text := '';
  det     text;
  norm    text := 'regexp_replace(lower(%s), ''\\'', '''', ''g'')';
  t       record;
  n       bigint;
  l       bigint;
  w       bigint;
  wr      bigint;
  total   bigint := 0;
  k       int := 0;
  miss    text[] := '{}';
  have    text[] := '{}';
BEGIN
  IF old_re = '' OR new_d = '' THEN
    RAISE EXCEPTION 'old_re and new must both be set';
  END IF;
  -- the rewrite pattern
  IF new_d ~* ('\.' || old_re || '$') THEN
    pre := '(?<!(?:^|' || delim || ')'
        || regexp_replace(regexp_replace(new_d, '\.' || old_re || '$', '', 'i'),
                          '([^A-Za-z0-9])', '\\\1', 'g')
        || ')';
  END IF;
  pat := pre || '(^|' || delim || ')' || old_re || '(?=$|[:/?#",\\[:space:]])';
  rep := '\1' || new_d;
  -- the independent detector (plain string arithmetic, its own boundaries)
  old_l := lower(regexp_replace(old_re, '\\(.)', '\1', 'g'));
  new_l := lower(new_d);
  IF length(new_l) > length(old_l) + 1 AND right(new_l, length(old_l) + 1) = '.' || old_l THEN
    det_pre := '(?<!(?:^|[^a-z0-9-])'
            || regexp_replace(left(new_l, length(new_l) - length(old_l) - 1), '([^a-z0-9])', '\\\1', 'g')
            || ')';
  END IF;
  det := det_pre || '(^|[^a-z0-9-])' || regexp_replace(old_l, '([^a-z0-9])', '\\\1', 'g')
      || '($|[^a-z0-9.-])';

  -- 1) every target, BEFORE anything is rewritten
  FOR t IN
    SELECT w.tbl, w.col, w.need, w.sev, c.data_type AS typ
      FROM (VALUES ('authentik_providers_proxy_proxyprovider', 'external_host', 'required', 'fail'),
                   ('authentik_outposts_outpost', '_config', 'required', 'fail'),
                   ('authentik_providers_oauth2_oauth2provider', '_redirect_uris', 'required', 'redirects'),
                   ('authentik_providers_proxy_proxyprovider', 'cookie_domain', 'expected', 'fail'),
                   ('authentik_core_application', 'meta_launch_url', 'expected', 'warn'),
                   ('authentik_core_user', 'email', 'expected', 'fail'),
                   ('authentik_providers_oauth2_oauth2provider', 'redirect_uris', 'optional', 'fail')) AS w(tbl, col, need, sev)
      LEFT JOIN information_schema.columns c
        ON c.table_schema = current_schema() AND c.table_name = w.tbl AND c.column_name = w.col
       AND c.data_type IN ('text', 'character varying', 'json', 'jsonb')
  LOOP
    IF t.typ IS NOT NULL THEN
      have := have || (t.tbl || '.' || t.col);
    ELSIF t.need = 'required' THEN
      miss := miss || (t.tbl || '.' || t.col);
    ELSE
      k := k + 1; INSERT INTO rzfz_redomain VALUES (k, 'skipped ' || t.tbl || '.' || t.col);
    END IF;
  END LOOP;
  IF cardinality(miss) > 0 THEN
    FOR i IN 1 .. cardinality(miss) LOOP
      RAISE WARNING 'missing %', miss[i];
    END LOOP;
    RAISE EXCEPTION 'required re-domain target(s) missing, nothing was rewritten';
  END IF;

  -- 2) rewrite, then measure with the detector
  FOR t IN
    SELECT w.tbl, w.col, w.sev, c.data_type AS typ
      FROM (VALUES ('authentik_providers_proxy_proxyprovider', 'external_host', 'required', 'fail'),
                   ('authentik_outposts_outpost', '_config', 'required', 'fail'),
                   ('authentik_providers_oauth2_oauth2provider', '_redirect_uris', 'required', 'redirects'),
                   ('authentik_providers_proxy_proxyprovider', 'cookie_domain', 'expected', 'fail'),
                   ('authentik_core_application', 'meta_launch_url', 'expected', 'warn'),
                   ('authentik_core_user', 'email', 'expected', 'fail'),
                   ('authentik_providers_oauth2_oauth2provider', 'redirect_uris', 'optional', 'fail')) AS w(tbl, col, need, sev)
      JOIN information_schema.columns c
        ON c.table_schema = current_schema() AND c.table_name = w.tbl AND c.column_name = w.col
       AND c.data_type IN ('text', 'character varying', 'json', 'jsonb')
  LOOP
    IF t.sev = 'redirects' THEN
      -- a list of {matching_mode, url}: only non-regex URLs are rewritten
      EXECUTE format($f$UPDATE %1$I SET %2$I = (
            SELECT coalesce(jsonb_agg(CASE WHEN jsonb_typeof(e) = 'object' AND e ? 'url'
                                            AND coalesce(e->>'matching_mode', 'strict') <> 'regex'
                       THEN jsonb_set(e, '{url}', to_jsonb(regexp_replace(e->>'url', %3$L, %4$L, 'gi')))
                       ELSE e END ORDER BY o), '[]'::jsonb)
              FROM jsonb_array_elements(%2$I::jsonb) WITH ORDINALITY AS x(e, o))::%5$s
          WHERE jsonb_typeof(%2$I::jsonb) = 'array' AND EXISTS (
            SELECT 1 FROM jsonb_array_elements(%2$I::jsonb) e
             WHERE jsonb_typeof(e) = 'object' AND coalesce(e->>'matching_mode', 'strict') <> 'regex'
               AND e->>'url' ~* %3$L)$f$, t.tbl, t.col, pat, rep, t.typ);
      GET DIAGNOSTICS n = ROW_COUNT;
      EXECUTE format($f$SELECT count(*) FROM %1$I p WHERE jsonb_typeof(%2$I::jsonb) = 'array' AND EXISTS (
            SELECT 1 FROM jsonb_array_elements(p.%2$I::jsonb) e
             WHERE coalesce(e->>'matching_mode', 'strict') <> 'regex' AND $f$
          || format(norm, 'coalesce(e->>''url'', e::text)') || $f$ ~ %3$L)$f$, t.tbl, t.col, det) INTO l;
      EXECUTE format($f$SELECT count(*), count(DISTINCT p.ctid) FROM %1$I p,
              jsonb_array_elements(CASE WHEN jsonb_typeof(p.%2$I::jsonb) = 'array' THEN p.%2$I::jsonb ELSE '[]'::jsonb END) e
             WHERE e->>'matching_mode' = 'regex' AND $f$
          || format(norm, 'coalesce(e->>''url'', '''')') || ' ~ %3$L', t.tbl, t.col, det) INTO w, wr;
    ELSE
      IF t.typ IN ('json', 'jsonb') THEN
        EXECUTE format('UPDATE %1$I SET %2$I = regexp_replace(%2$I::text, %3$L, %4$L, ''gi'')::%5$s '
                       'WHERE %2$I::text ~* %3$L', t.tbl, t.col, pat, rep, t.typ);
      ELSE
        EXECUTE format('UPDATE %1$I SET %2$I = regexp_replace(%2$I, %3$L, %4$L, ''gi'') '
                       'WHERE %2$I ~* %3$L', t.tbl, t.col, pat, rep);
      END IF;
      GET DIAGNOSTICS n = ROW_COUNT;
      EXECUTE format('SELECT count(*) FROM %1$I WHERE ' || format(norm, '%2$I::text') || ' ~ %3$L',
                     t.tbl, t.col, det) INTO l;
      w := 0; wr := 0;
    END IF;
    IF n > 0 THEN
      k := k + 1; INSERT INTO rzfz_redomain VALUES (k, 'rewritten ' || t.tbl || '.' || t.col || ' ' || n);
    END IF;
    IF l > 0 AND t.sev = 'warn' THEN
      k := k + 1; INSERT INTO rzfz_redomain VALUES (k, 'warn ' || t.tbl || '.' || t.col || ' ' || l);
    ELSIF l > 0 THEN
      k := k + 1; INSERT INTO rzfz_redomain VALUES (k, 'leftover ' || t.tbl || '.' || t.col || ' ' || l);
      total := total + l;
    END IF;
    IF w > 0 THEN
      k := k + 1; INSERT INTO rzfz_redomain VALUES (k, 'warn-regex ' || t.tbl || '.' || t.col || ' ' || w || ' ' || wr);
    END IF;
  END LOOP;
  k := k + 1; INSERT INTO rzfz_redomain VALUES (k, total::text);
END $$;
COMMIT;
SELECT line FROM rzfz_redomain ORDER BY ord;
