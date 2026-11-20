-- SPDX-License-Identifier: BUSL-1.1
-- Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
-- Re-domain: the LLM Manager keeps every registered worker's address in its
-- own database (workers.address, a host[:port] or URL). A worker registered
-- under <label>.<old-domain> would stay unreachable after the move. This
-- re-points each address whose HOST is the old domain or below it.
-- Run by scripts/redomain-reconcile.sh against the llm_manager database, fed
-- on stdin; psql vars: old_re (old domain, regex-escaped), new (new domain).
--
-- The host rule is the re-domain's own (razzfazz_common.redomain): `<old>` or
-- `<label>.<old>` as a WHOLE host, case-insensitive, so `mybox.local` survives
-- a `box.local` move; when the new domain is below the old one a host already
-- under it is left alone and a second run changes nothing. Ports, paths and
-- userinfo are carried over. A database without a workers.address column is
-- a no-op (`skipped`). One transaction. Output: counts and names only:
--   rewritten workers.address <rows>
--   skipped workers.address
--   leftover workers.address <rows>     still naming the old domain (fail)
--   <n>                                 LAST line: the leftover rows
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
                  WHERE table_schema = current_schema() AND table_name = 'workers'
                    AND column_name = 'address' AND data_type IN ('text', 'character varying')) THEN
    INSERT INTO rzfz_redomain VALUES (1, 'skipped workers.address');
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
  EXECUTE format('UPDATE workers SET address = regexp_replace(address, %1$L, %2$L, ''gi'') WHERE address ~* %1$L',
                 pat, '\1' || new_d);
  GET DIAGNOSTICS n = ROW_COUNT;
  EXECUTE format('SELECT count(*) FROM workers WHERE lower(address) ~ %1$L', det) INTO l;
  IF n > 0 THEN INSERT INTO rzfz_redomain VALUES (1, 'rewritten workers.address ' || n); END IF;
  IF l > 0 THEN INSERT INTO rzfz_redomain VALUES (2, 'leftover workers.address ' || l); END IF;
  INSERT INTO rzfz_redomain VALUES (3, l::text);
END $$;
COMMIT;
SELECT line FROM rzfz_redomain ORDER BY ord;
