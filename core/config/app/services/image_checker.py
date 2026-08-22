# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Image update checker — compares local Docker image digests against remote registries
and checks the version manifest for vendor-curated updates."""

import json
import subprocess
import sqlite3
import os
import re
import threading
import time
from datetime import datetime, timezone

from .env_utils import parse_env_file


class ImageChecker:
    def __init__(self, profile_manager, db_path='/data/image_updates.db',
                 manifest_path=None):
        self._profile_manager = profile_manager
        self._db_path = db_path
        self._manifest_path = manifest_path or os.path.join(
            os.environ.get('STACK_ROOT', '/stack'), 'config', 'manifests', 'versions.json')
        self._init_db()
        self._checking = False
        self._manifest_updates = None  # cached manifest comparison

    def _init_db(self):
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS image_update_cache (
                    image_ref TEXT PRIMARY KEY,
                    current_digest TEXT,
                    remote_digest TEXT,
                    checked_at TEXT NOT NULL,
                    update_available BOOLEAN NOT NULL DEFAULT 0
                )
            """)

    def get_cached_results(self):
        """Return cached update check results."""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM image_update_cache ORDER BY update_available DESC, image_ref"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_last_check_time(self):
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("SELECT MAX(checked_at) FROM image_update_cache").fetchone()
        return row[0] if row and row[0] else None

    def check_now(self):
        """Run update check for all images with check_updates=true. Returns results."""
        if self._checking:
            return None
        self._checking = True
        try:
            results = []
            profiles = self._profile_manager.get_all_profiles()
            for pid, profile in profiles.items():
                for container in profile.get('containers', []):
                    if not container.get('check_updates', False):
                        continue
                    image_ref = container.get('image', '')
                    if not image_ref or ':' not in image_ref and '/' not in image_ref:
                        continue

                    local_digest = self._get_local_digest(container['name'])
                    remote_digest = self._get_remote_digest(image_ref)
                    update_available = (
                        bool(local_digest) and bool(remote_digest)
                        and local_digest != remote_digest
                    )
                    now = datetime.now(timezone.utc).isoformat()

                    with sqlite3.connect(self._db_path) as conn:
                        conn.execute("""
                            INSERT OR REPLACE INTO image_update_cache
                            (image_ref, current_digest, remote_digest, checked_at, update_available)
                            VALUES (?, ?, ?, ?, ?)
                        """, (image_ref, local_digest or '', remote_digest or '', now, update_available))

                    results.append({
                        'image_ref': image_ref,
                        'container': container['name'],
                        'update_available': update_available,
                        'current_digest': (local_digest or '')[:16],
                        'remote_digest': (remote_digest or '')[:16],
                    })
            return results
        finally:
            self._checking = False

    def _get_local_digest(self, container_name):
        """Get the image digest for a running container."""
        try:
            result = subprocess.run(
                ['docker', 'inspect', container_name, '--format', '{{.Image}}'],
                capture_output=True, text=True, timeout=10,
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except Exception:
            return None

    def _get_remote_digest(self, image_ref):
        """Get remote image digest via docker manifest inspect."""
        try:
            # Normalize: add :latest if no tag
            if ':' not in image_ref.split('/')[-1]:
                image_ref += ':latest'
            result = subprocess.run(
                ['docker', 'manifest', 'inspect', '--insecure', image_ref],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return None
            data = json.loads(result.stdout)
            # For manifest list, use the overall digest
            return data.get('config', {}).get('digest', '') or json.dumps(data.get('manifests', [{}])[0].get('digest', ''))
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Manifest-based version comparison (M013)
    # ------------------------------------------------------------------

    def get_manifest_updates(self):
        """Compare current .env versions against the manifest. Returns dict keyed by profile."""
        if self._manifest_updates is not None:
            return self._manifest_updates

        try:
            manifest = self._load_manifest()
            if not manifest:
                return {}
            env = self._read_env()
            enabled = self._profile_manager.get_enabled_profiles()
            enabled.add('core')

            updates_by_profile = {}
            for key, entry in manifest.get('images', {}).items():
                profile = entry.get('profile', '')
                env_var = entry.get('env_var', '')
                current = env.get(env_var, '')
                manifest_ver = entry.get('current', '')
                compat = entry.get('compatibility', 'patch')

                if not current or compat == 'frozen':
                    continue
                if self._strip_v(current) == self._strip_v(manifest_ver):
                    continue
                if self._is_within_compat(current, manifest_ver, compat):
                    updates_by_profile.setdefault(profile, []).append({
                        'key': key,
                        'image': entry['image'],
                        'from': current,
                        'to': manifest_ver,
                    })

            self._manifest_updates = updates_by_profile
            return updates_by_profile
        except Exception:
            return {}

    def get_manifest_cve_alerts(self):
        """Return CVE alerts from the manifest."""
        try:
            manifest = self._load_manifest()
            return manifest.get('cve_alerts', []) if manifest else []
        except Exception:
            return []

    def get_manifest_info(self):
        """Return manifest metadata (version, published date)."""
        try:
            manifest = self._load_manifest()
            if not manifest:
                return None
            return {
                'stack_version': manifest.get('stack_version', ''),
                'published': manifest.get('published', ''),
                'channel': manifest.get('channel', ''),
            }
        except Exception:
            return None

    def fetch_remote_manifest(self):
        """Fetch the latest manifest from origin/main and save locally.

        Tries two transports in order:
        1. `git fetch + git show` — works in Authentik-protected fleets where
           git.razzfazz.ai's smart-HTTP path is exempt from the SSO proxy.
           This is the SEQIS deployment pattern.
        2. HTTPS GET of the configured `manifest_url` — fallback for setups
           where git isn't available, the repo isn't a git checkout, or the
           URL points at an unauthenticated CDN.
        """
        try:
            local = self._load_manifest()
            if local is None:
                return False, 'No local manifest at ' + self._manifest_path

            # #275 — public/customer boxes sit behind a corporate proxy and
            # cannot reach the internal Gitea (git.razzfazz.ai). On the public
            # channel, fetch the manifest ANONYMOUSLY from the GitHub public-
            # mirror raw URL and skip the git / gitea-api (PAT) transports
            # entirely. (requests honours HTTPS_PROXY via trust_env; the config
            # container also carries the corporate CA — it is in the overlay
            # egress allow-list — so the WSA-intercepted TLS verifies.)
            if self._channel() == 'public':
                return self._fetch_via_url(self._public_manifest_url())

            # Path 1 — git smart-HTTP. Uses ~/.git-credentials via the helper,
            # which Authentik's proxy on git.razzfazz.ai exempts. The same
            # credentials razzfazz-upgrade.sh uses for the regular pull.
            stack_root = os.environ.get('STACK_ROOT', '/stack')
            if os.path.isdir(os.path.join(stack_root, '.git')):
                ok, payload = self._fetch_via_git(stack_root)
                if ok:
                    remote = payload
                    return self._verify_and_persist(remote, source='git')
                # Fall through to URL fetch on failure (record the reason
                # in case URL fetch also fails so the operator sees both).
                git_err = payload
            else:
                git_err = 'STACK_ROOT is not a git checkout'

            murl = local.get('manifest_url')
            if not murl:
                return False, (
                    f'git fetch failed ({git_err}) and no manifest_url '
                    f'configured for HTTPS fallback'
                )

            # Path 1.5 — Gitea API raw endpoint with token auth. The SEQIS fleet
            # mounts the repo .git READ-ONLY into the config container (so
            # `git fetch` can't write FETCH_HEAD → git exit 255) AND the Gitea
            # repo is private (so the plain /raw/branch/ web route 302-redirects
            # to the Gitea login and ignores HTTP basic auth → text/html). The
            # API raw endpoint /api/v1/repos/<o>/<r>/raw/<path>?ref=<ref> honours
            # `Authorization: token <PAT>` (the same PAT git uses, read from
            # /root/.git-credentials) and returns the file directly. This is the
            # path that actually works on a locked-down fleet box (0.208).
            api_ok, api_payload = self._fetch_via_gitea_api(murl)
            if api_ok:
                return self._verify_and_persist(api_payload, source='gitea-api')
            api_err = api_payload

            # Path 2 — plain HTTPS GET (public / unauthenticated CDN).
            ok, msg = self._fetch_via_url(murl)
            if not ok:
                return False, (
                    f'All manifest fetch paths failed. git: {git_err}. '
                    f'gitea-api: {api_err}. http: {msg}'
                )
            return ok, msg
        except Exception as exc:
            return False, str(exc)

    def _fetch_via_git(self, stack_root):
        """git fetch origin + git show origin/main:config/manifests/versions.json.

        Returns (True, parsed_dict) or (False, error_message).

        - `-c safe.directory=*` bypasses git's dubious-ownership check
          (the container runs as root but the repo dir is owned by the
          installing user — we trust the bind mount).
        - Credentials come from /root/.git-credentials, which compose
          bind-mounts read-only from the host's $HOME/.git-credentials
          (set by razzfazz-init.sh / razzfazz-upgrade.sh as
          STACK_GIT_CREDENTIALS_PATH). The credential.helper=store
          override ensures git looks there even with no global gitconfig.
        """
        import subprocess
        env = dict(os.environ)
        common = [
            'git',
            '-c', 'safe.directory=*',
            '-c', 'credential.helper=store --file=/root/.git-credentials',
        ]
        try:
            subprocess.run(
                common + ['fetch', '--quiet', 'origin', 'main'],
                cwd=stack_root, env=env, capture_output=True, timeout=30, check=True,
            )
            # Post-2026.07 reorg the manifest lives at config/manifests/.
            # Try the current path first; fall back to the legacy top-level
            # manifests/ path so a deployed container can still read a ref
            # (e.g. origin/main during the brief deploy window, or an older
            # branch) that predates the P3 config/ move.
            last_err = None
            for ref in ('origin/main:config/manifests/versions.json',
                        'origin/main:manifests/versions.json'):
                try:
                    out = subprocess.run(
                        common + ['show', ref],
                        cwd=stack_root, env=env,
                        capture_output=True, timeout=10, check=True,
                    )
                    return True, json.loads(out.stdout)
                except subprocess.CalledProcessError as e:
                    last_err = e
                    continue
            err = (last_err.stderr or b'').decode(errors='replace').strip() if last_err else ''
            return False, f'git exit {last_err.returncode if last_err else "?"}: {err[:200]}'
        except subprocess.TimeoutExpired:
            return False, 'git timed out'
        except subprocess.CalledProcessError as e:
            err = (e.stderr or b'').decode(errors='replace').strip()
            return False, f'git exit {e.returncode}: {err[:200]}'
        except json.JSONDecodeError as e:
            return False, f'git output not JSON: {e}'

    def _read_git_token(self, host):
        """Extract the PAT for `host` from /root/.git-credentials.

        Lines look like `https://<user>:<token>@git.razzfazz.ai`. `host` is the
        scheme+host of the manifest URL (e.g. `https://git.razzfazz.ai`).
        Returns the token string, or None.
        """
        try:
            with open('/root/.git-credentials') as fh:
                for line in fh:
                    m = re.match(r'^https?://[^:]+:([^@]+)@(.+?)/?$', line.strip())
                    if m and m.group(2) in host:
                        return m.group(1)
        except OSError:
            pass
        return None

    def _fetch_via_gitea_api(self, manifest_url):
        """Fetch the manifest via Gitea's token-authenticated API raw endpoint.

        Rewrites a `/raw/branch/<ref>/<path>` web URL to the API form
        `/api/v1/repos/<owner>/<repo>/raw/<path>?ref=<ref>` and sends
        `Authorization: token <PAT>` (PAT from /root/.git-credentials). Unlike
        the web /raw route, the API endpoint honours the token for private repos
        and needs no writable .git. Returns (True, parsed_dict) or (False, err).
        """
        import requests
        m = re.match(
            r'^(https?://[^/]+)/([^/]+)/([^/]+)/raw/branch/([^/]+)/(.+)$',
            manifest_url,
        )
        if not m:
            return False, f'manifest_url is not a Gitea /raw/branch/ URL: {manifest_url}'
        host, owner, repo, ref, path = m.groups()
        token = self._read_git_token(host)
        if not token:
            return False, f'no PAT for {host} in /root/.git-credentials'
        api = f'{host}/api/v1/repos/{owner}/{repo}/raw/{path}?ref={ref}'
        try:
            resp = requests.get(
                api, headers={'Authorization': f'token {token}'}, timeout=15,
            )
            resp.raise_for_status()
            return True, json.loads(resp.text)
        except requests.RequestException as e:
            return False, f'gitea-api GET failed: {e}'
        except ValueError as e:
            return False, f'gitea-api output not JSON: {e}'

    def _fetch_via_url(self, url):
        """HTTPS GET of the manifest URL. Returns (True, status_msg) or
        (False, error_message)."""
        import requests
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()

            ctype = resp.headers.get('content-type', '').split(';', 1)[0].strip().lower()
            if ctype and ctype not in ('application/json', 'text/plain', 'text/json'):
                snippet = (resp.text or '')[:80].replace('\n', ' ')
                return False, (
                    f'URL returned {ctype!r} (expected application/json). '
                    f'Likely auth-gated by Authentik / SSO proxy. '
                    f'First bytes: {snippet!r}. '
                    f'Workaround: ensure the box has git origin set to '
                    f'git.razzfazz.ai (the smart-HTTP path is exempt from '
                    f'the proxy and image_checker prefers it when available).'
                )
            try:
                remote = resp.json()
            except ValueError as json_exc:
                snippet = (resp.text or '')[:80].replace('\n', ' ')
                return False, (
                    f'URL did not return JSON ({json_exc}). '
                    f'First bytes: {snippet!r}'
                )
            return self._verify_and_persist(remote, source='url')
        except requests.RequestException as e:
            return False, f'HTTPS GET failed: {e}'

    def _verify_and_persist(self, remote, source='unknown'):
        """Persist the fetched manifest to the local path, clear the diff cache.

        The pre-rc5 .sha256 sidecar verify is dropped — git delivery is already
        integrity-checked by the git protocol, and the URL-fallback path here
        is best-effort secondary. If we re-add a public CDN delivery path
        later, hash-verify can come back as an opt-in.
        """
        try:
            manifest_dir = os.path.dirname(self._manifest_path)
            os.makedirs(manifest_dir, exist_ok=True)
            with open(self._manifest_path, 'w') as f:
                json.dump(remote, f, indent=2)
                f.write('\n')
            self._manifest_updates = None  # invalidate diff cache
            return True, (
                f'Manifest updated via {source}: '
                f'{remote.get("stack_version", "?")} '
                f'({remote.get("published", "")[:10]})'
            )
        except Exception as exc:
            return False, str(exc)

    def invalidate_manifest_cache(self):
        """Clear the cached manifest comparison."""
        self._manifest_updates = None

    def _load_manifest(self):
        if not os.path.exists(self._manifest_path):
            return None
        with open(self._manifest_path) as f:
            return json.load(f)

    def _read_env(self):
        # rc6.7: route through shared env_utils so the image-checker's view
        # of `.env` matches what razzfazz-upgrade.sh writes/reads. Without
        # this, an inline ` # ferretdb-postgres` comment on
        # KOMODO_DB_VERSION leaked into the value and the version
        # comparison flagged a phantom "update available" because the
        # comment-laden current didn't string-match the manifest's bare
        # current.
        env_path = os.path.join(os.environ.get('STACK_ROOT', '/stack'), '.env')
        return parse_env_file(env_path)

    def _channel(self):
        """Release channel from $STACK_ROOT/.env (default 'internal'). The config
        container has no RAZZFAZZ_CHANNEL in its process env, so read the same
        .env that _read_env() already parses (#275)."""
        return (self._read_env().get('RAZZFAZZ_CHANNEL', 'internal')
                or 'internal').strip().lower()

    def _public_manifest_url(self):
        """Anonymous GitHub-raw URL for the public-mirror manifest (#275).
        Derives from RAZZFAZZ_PUBLIC_REMOTE (.env) or the canonical default:
        github.com/<owner>/<repo>[.git] ->
        raw.githubusercontent.com/<owner>/<repo>/main/config/manifests/versions.json."""
        remote = (self._read_env().get('RAZZFAZZ_PUBLIC_REMOTE', '')
                  or 'https://github.com/rzfz-ai/rzfz-ai-service-stack.git').strip()
        default = ('https://raw.githubusercontent.com/rzfz-ai/'
                   'rzfz-ai-service-stack/main/config/manifests/versions.json')
        marker = 'github.com/'
        if marker in remote:
            path = remote.split(marker, 1)[1].strip('/')
            if path.endswith('.git'):
                path = path[:-4]
            parts = path.split('/')
            if len(parts) >= 2:
                return ('https://raw.githubusercontent.com/%s/%s'
                        '/main/config/manifests/versions.json'
                        % (parts[0], parts[1]))
        return default

    @staticmethod
    def _strip_v(v):
        return v.lstrip('v')

    @staticmethod
    def _parse_semver(v):
        v = v.lstrip('v')
        parts = re.split(r'[.\-]', v)
        nums = []
        for p in parts:
            m = re.match(r'^(\d+)', p)
            if m:
                nums.append(int(m.group(1)))
            else:
                break
        while len(nums) < 3:
            nums.append(0)
        return tuple(nums[:3])

    @classmethod
    def _is_within_compat(cls, current, new, compat):
        cur = cls._parse_semver(current)
        nw = cls._parse_semver(new)
        if compat == 'patch':
            return cur[0] == nw[0] and cur[1] == nw[1] and nw[2] >= cur[2]
        if compat == 'minor':
            return cur[0] == nw[0] and (nw[1] > cur[1] or (nw[1] == cur[1] and nw[2] >= cur[2]))
        return False
