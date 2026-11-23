-- SPDX-License-Identifier: BUSL-1.1
-- Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
-- 2026.09-ga.5 re-domain fixup (#2932, also the #2928 gap): Authentik (authentik_db).
-- ga.5 (Authentik 2026.5) re-templates its blueprints on `docker compose up -d`
-- after set-domain; this catches what that leaves behind:
--   * authentik_core_user.email          the admin (and any user) on the old domain
--   * proxyprovider.external_host / cookie_domain, outpost._config,
--     application.meta_launch_url        hosts on the old domain (whole-host rule)
--   * oauth2provider._redirect_uris      a JSON list of {matching_mode, url}: only
--                                        non-regex URLs are rewritten; regex URIs
--                                        are counted as `info` for the admin UI.
BEGIN;
SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '120s';
INSERT INTO rz_out (line) SELECT 'before authentik.user ' || count(*) FROM authentik_core_user WHERE pg_temp.rz_old_email(email);
INSERT INTO rz_out (line) SELECT 'before authentik.proxyprovider ' || count(*) FROM authentik_providers_proxy_proxyprovider
 WHERE pg_temp.rz_left(external_host) OR pg_temp.rz_left(cookie_domain);
INSERT INTO rz_out (line) SELECT 'before authentik.outpost ' || count(*) FROM authentik_outposts_outpost WHERE pg_temp.rz_left(_config::text);
INSERT INTO rz_out (line) SELECT 'before authentik.redirect_uris ' || count(*) FROM authentik_providers_oauth2_oauth2provider p
 WHERE jsonb_typeof(p._redirect_uris::jsonb) = 'array' AND EXISTS (
       SELECT 1 FROM jsonb_array_elements(p._redirect_uris::jsonb) e
        WHERE coalesce(e->>'matching_mode', 'strict') <> 'regex' AND pg_temp.rz_left(coalesce(e->>'url', e::text)));
INSERT INTO rz_out (line) SELECT 'before authentik.launch_url ' || count(*) FROM authentik_core_application WHERE pg_temp.rz_left(meta_launch_url);

WITH c AS (
  UPDATE authentik_core_user SET email = pg_temp.rz_new_email(email)
   WHERE pg_temp.rz_old_email(email) RETURNING 1)
INSERT INTO rz_out (line) SELECT 'changed authentik.user ' || count(*) FROM c;

WITH c AS (
  UPDATE authentik_providers_proxy_proxyprovider
     SET external_host = pg_temp.rz_text(external_host),
         cookie_domain = CASE WHEN pg_temp.rz_has(cookie_domain) THEN pg_temp.rz_text(cookie_domain) ELSE cookie_domain END
   WHERE pg_temp.rz_has(external_host) OR pg_temp.rz_has(cookie_domain) RETURNING 1)
INSERT INTO rz_out (line) SELECT 'changed authentik.proxyprovider ' || count(*) FROM c;

WITH c AS (
  UPDATE authentik_outposts_outpost SET _config = pg_temp.rz_text(_config::text)::jsonb
   WHERE pg_temp.rz_has(_config::text) RETURNING 1)
INSERT INTO rz_out (line) SELECT 'changed authentik.outpost ' || count(*) FROM c;

WITH c AS (
  UPDATE authentik_providers_oauth2_oauth2provider p SET _redirect_uris = (
      SELECT coalesce(jsonb_agg(CASE WHEN jsonb_typeof(e) = 'object' AND e ? 'url'
                                      AND coalesce(e->>'matching_mode', 'strict') <> 'regex'
                 THEN jsonb_set(e, '{url}', to_jsonb(pg_temp.rz_text(e->>'url')))
                 ELSE e END ORDER BY o), '[]'::jsonb)
        FROM jsonb_array_elements(p._redirect_uris::jsonb) WITH ORDINALITY AS x(e, o))
   WHERE jsonb_typeof(p._redirect_uris::jsonb) = 'array' AND EXISTS (
         SELECT 1 FROM jsonb_array_elements(p._redirect_uris::jsonb) e
          WHERE jsonb_typeof(e) = 'object' AND coalesce(e->>'matching_mode', 'strict') <> 'regex'
            AND pg_temp.rz_has(e->>'url'))
  RETURNING 1)
INSERT INTO rz_out (line) SELECT 'changed authentik.redirect_uris ' || count(*) FROM c;

WITH c AS (
  UPDATE authentik_core_application SET meta_launch_url = pg_temp.rz_text(meta_launch_url)
   WHERE pg_temp.rz_has(meta_launch_url) RETURNING 1)
INSERT INTO rz_out (line) SELECT 'changed authentik.launch_url ' || count(*) FROM c;

INSERT INTO rz_out (line) SELECT 'after authentik.user ' || count(*) FROM authentik_core_user WHERE pg_temp.rz_old_email(email);
INSERT INTO rz_out (line) SELECT 'after authentik.proxyprovider ' || count(*) FROM authentik_providers_proxy_proxyprovider
 WHERE pg_temp.rz_left(external_host) OR pg_temp.rz_left(cookie_domain);
INSERT INTO rz_out (line) SELECT 'after authentik.outpost ' || count(*) FROM authentik_outposts_outpost WHERE pg_temp.rz_left(_config::text);
INSERT INTO rz_out (line) SELECT 'after authentik.redirect_uris ' || count(*) FROM authentik_providers_oauth2_oauth2provider p
 WHERE jsonb_typeof(p._redirect_uris::jsonb) = 'array' AND EXISTS (
       SELECT 1 FROM jsonb_array_elements(p._redirect_uris::jsonb) e
        WHERE coalesce(e->>'matching_mode', 'strict') <> 'regex' AND pg_temp.rz_left(coalesce(e->>'url', e::text)));
INSERT INTO rz_out (line) SELECT 'after authentik.launch_url ' || count(*) FROM authentik_core_application WHERE pg_temp.rz_left(meta_launch_url);
INSERT INTO rz_out (line) SELECT 'info authentik.regex_redirect_uris ' || count(*) FROM authentik_providers_oauth2_oauth2provider p,
       jsonb_array_elements(CASE WHEN jsonb_typeof(p._redirect_uris::jsonb) = 'array' THEN p._redirect_uris::jsonb ELSE '[]'::jsonb END) e
 WHERE e->>'matching_mode' = 'regex' AND pg_temp.rz_left(coalesce(e->>'url', ''));
SELECT line FROM rz_out ORDER BY ord;
\if :apply
COMMIT;
\else
ROLLBACK;
\endif
