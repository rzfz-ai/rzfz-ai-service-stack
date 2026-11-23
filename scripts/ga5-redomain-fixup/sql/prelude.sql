-- SPDX-License-Identifier: BUSL-1.1
-- Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
-- 2026.09-ga.5 re-domain fixup (#2932): shared prelude, fed on stdin in front of
-- every per-app file by rzfz-redomain-fixup-ga5.sh.
--
-- psql vars: old (old domain), new (new domain), apply (on = COMMIT, off = ROLLBACK).
--
-- Matching rules (ported from the 2026.11 scripts/sql/redomain-*.sql and
-- razzfazz_common.redomain, #2376):
--   * text/JSON blobs: a host matches only as a WHOLE host, `<old>` or
--     `<label>.<old>`, preceded by start, '/', '.', '@', '"', ',' or whitespace
--     and followed by end, ':', '/', '?', '#', '"', ',', '\' or whitespace,
--     case-insensitive (pg_temp.rz_text). `mybox.local` survives a `box.local` move.
--   * e-mail addresses: only the HOST part, `@<old>` or `@<label>.<old>` at the
--     end of the address (pg_temp.rz_old_email / rz_new_email). The local part
--     is never touched.
--   * when the new domain is below the old one (old a.com, new x.a.com), a host
--     already under the new domain is left alone, so a second run changes nothing.
--   * the leftover detector (pg_temp.rz_left) is deliberately NOT the rewrite
--     pattern: lower-cased text with every backslash removed, searched for the
--     old domain bounded by anything that cannot be part of a host label.
--
-- Output protocol of every per-app file (one line each, counts only):
--   before <target> <n>   rows naming the old domain before the change
--   changed <target> <n>  rows changed (rolled back again in a dry run)
--   after <target> <n>    rows still naming the old domain afterwards
--   info <target> <n>     reported only, never changed
--   stop <target> ...     the change was refused (cognee: both accounts own data)
--   note <target> ...     what was done to a single account (cognee)
\set ON_ERROR_STOP on
\set QUIET on
SELECT set_config('rzfz.old', lower(:'old'), false) AS rz_o,
       set_config('rzfz.new', lower(:'new'), false) AS rz_n \gset
DO $$
DECLARE
  old_d   text := current_setting('rzfz.old');
  new_d   text := current_setting('rzfz.new');
  esc     text := '([^a-z0-9])';
  dom     text := '^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$';
  delim   text := '[/.@",[:space:]]';
  old_re  text;
  new_re  text;
  lbl_re  text;
  pre     text := '';
  det_pre text := '';
  below   boolean;
BEGIN
  IF old_d !~ dom OR new_d !~ dom OR old_d = new_d THEN
    RAISE EXCEPTION 'old and new must be two different domain names';
  END IF;
  old_re := regexp_replace(old_d, esc, '\\\1', 'g');
  new_re := regexp_replace(new_d, esc, '\\\1', 'g');
  below := right(new_d, length(old_d) + 1) = '.' || old_d;
  IF below THEN
    lbl_re := regexp_replace(left(new_d, length(new_d) - length(old_d) - 1), esc, '\\\1', 'g');
    pre := '(?<!(?:^|' || delim || ')' || lbl_re || ')';
    det_pre := '(?<!(?:^|[^a-z0-9-])' || lbl_re || ')';
  END IF;
  PERFORM set_config('rzfz.pat', pre || '(^|' || delim || ')' || old_re || '(?=$|[:/?#",\\[:space:]])', false);
  PERFORM set_config('rzfz.rep', '\1' || new_d, false);
  PERFORM set_config('rzfz.det', det_pre || '(^|[^a-z0-9-])' || old_re || '($|[^a-z0-9.-])', false);
  PERFORM set_config('rzfz.epat', '@((?:[a-z0-9-]+\.)*)' || old_re || '$', false);
  PERFORM set_config('rzfz.erep', '@\1' || new_d, false);
  PERFORM set_config('rzfz.enew', '@(?:[a-z0-9-]+\.)*' || new_re || '$', false);
  PERFORM set_config('rzfz.below', CASE WHEN below THEN 'on' ELSE 'off' END, false);
END $$;

CREATE FUNCTION pg_temp.rz_old_email(e text) RETURNS boolean LANGUAGE sql STABLE AS $f$
  SELECT coalesce(lower(e) ~ current_setting('rzfz.epat')
         AND NOT (current_setting('rzfz.below') = 'on' AND lower(e) ~ current_setting('rzfz.enew')), false)
$f$;
CREATE FUNCTION pg_temp.rz_new_email(e text) RETURNS text LANGUAGE sql STABLE AS $f$
  SELECT regexp_replace(e, current_setting('rzfz.epat'), current_setting('rzfz.erep'), 'i')
$f$;
CREATE FUNCTION pg_temp.rz_has(t text) RETURNS boolean LANGUAGE sql STABLE AS $f$
  SELECT coalesce(t ~* current_setting('rzfz.pat'), false)
$f$;
CREATE FUNCTION pg_temp.rz_text(t text) RETURNS text LANGUAGE sql STABLE AS $f$
  SELECT regexp_replace(t, current_setting('rzfz.pat'), current_setting('rzfz.rep'), 'gi')
$f$;
CREATE FUNCTION pg_temp.rz_left(t text) RETURNS boolean LANGUAGE sql STABLE AS $f$
  SELECT coalesce(regexp_replace(lower(t), '\\', '', 'g') ~ current_setting('rzfz.det'), false)
$f$;
CREATE TEMP TABLE rz_out (ord serial, line text);
