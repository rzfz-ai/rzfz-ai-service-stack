# Container Version Compatibility Matrix

This document defines the version bump rules for each container image in the
razzfazz.ai stack. The update engine (`razzfazz-upgrade.sh --update`) enforces
these rules when applying a new manifest.

## Compatibility Levels

| Level | Allowed Bumps | Example | Risk |
|-------|--------------|---------|------|
| **patch** | `x.y.Z` only | `1.35.6` -> `1.35.7` | Low — bug fixes, security patches |
| **minor** | `x.Y.z` | `2026.2.2` -> `2026.3.0` | Low-Medium — new features, no breaking changes expected |
| **frozen** | None | N/A | None — requires full stack upgrade to change version |

## Version Comparison Rules

The update engine uses **semantic version comparison** with these special cases:

- **Standard semver** (`1.2.3`): compare major.minor.patch numerically
- **Date-based tags** (`2026.4.13-hash`): compare date components, ignore hash suffix
- **Prefixed versions** (`v1.2.3`): strip `v` prefix before comparison
- **Suffixed versions** (`0.8.3-cpu`, `0.5.6-local`): preserve suffix, compare numeric prefix
- **Compound tags** (`17-0.107.0-ferretdb-2.7.0`): compare as opaque string, update only on exact match in manifest

## Image Classification

### Frozen (no automated updates)

| Image | Reason |
|-------|--------|
| pgvector/pgvector (pg17) | PostgreSQL major version. Data migration required for upgrades. |
| gpustack/gpustack (CPU) | AMD Vulkan driver compatibility breaks on newer versions. |
| gpustack/gpustack (experimental) | Tied to Vulkan build compatibility. |

### Minor (x.Y.z bumps allowed)

| Image | Reason |
|-------|--------|
| ghcr.io/goauthentik/server | Hard to upgrade. Minor releases are stable but major versions introduce breaking schema changes. |
| searxng/searxng | Rolling release with date-based tags. Minor bumps are routine. |
| vectorim/element-web | Frontend-only client. Minor bumps safe — no backend state. |

### Patch (x.y.Z bumps only) — Default

All remaining images default to **patch** compatibility. This is the safest
level: only bug fixes and security patches are applied.

Notable patch-only images with specific considerations:

| Image | Notes |
|-------|-------|
| langgenius/dify-api | Shared version across dify-api, dify-worker, dify-worker-beat. All must update together. |
| ghcr.io/moghtech/komodo-core | Shared version with komodo-periphery. |
| ghcr.io/all-hands-ai/openhands | Runtime image must match (0.9.1 + 0.9.1-nikolaik). |
| mcr.microsoft.com/presidio-* | Analyzer and anonymizer should match versions. |
| onyxdotapp/onyx-* | Backend, web-server, and model-server share one version tag. |
| ghcr.io/speaches-ai/speaches | Uses `-cpu` suffix. New version must preserve suffix. |
| langgenius/dify-plugin-daemon | Uses `-local` suffix. New version must preserve suffix. |
| infisical/infisical | Uses `-postgres` suffix. New version must preserve suffix. |

## Linked Version Groups

Some images share a single version variable or must be updated in lockstep:

| Group | Images | Version Source |
|-------|--------|---------------|
| Dify core | dify-api, dify-worker, dify-worker-beat | `DIFY_VERSION` |
| Komodo | komodo-core, komodo-periphery | `KOMODO_VERSION` |
| OpenHands | openhands, openhands-runtime | Hardcoded (must match) |
| Presidio | presidio-analyzer, presidio-anonymizer | Hardcoded (should match) |
| Onyx | onyx-backend, onyx-web-server, onyx-model-server | Hardcoded (must match) |

## Custom-Built Images (Excluded from Updates)

These images are built locally from Dockerfiles in the stack repository. They
are versioned with the stack code and are **never** updated via the manifest:

- razzfazz-caddy, razzfazz-help, razzfazz-licenses
- razzfazz-backup-management, razzfazz-backup-service, razzfazz-config
- razzfazz-dify-web, razzfazz-model-sync
- razzfazz-hermes-agent, razzfazz-cognee, razzfazz-coding-tools
- razzfazz-agent-manager

## Testing Checklist for Manifest Updates

Before publishing an updated manifest:

1. [ ] Pull new image version locally
2. [ ] Start the service and verify health check passes
3. [ ] Check for breaking changes in upstream release notes
4. [ ] Verify version tag format matches existing pattern (prefix, suffix)
5. [ ] For linked groups: verify all images in the group are updated together
6. [ ] Run `razzfazz-upgrade.sh --update --check` against the new manifest
7. [ ] Test on a non-production stack before publishing
8. [ ] Update `manifests/versions.json` and regenerate checksum
9. [ ] Tag the manifest update: `git tag manifest-{stack_version}-{seq}`

## Manifest Publishing Workflow

```bash
# 1. Edit manifests/versions.json with new versions
# 2. Regenerate checksum
sha256sum manifests/versions.json > manifests/versions.json.sha256

# 3. Commit and push
git add manifests/versions.json manifests/versions.json.sha256
git commit -m "manifest: bump <image> to <version>"

# 4. Tag
git tag manifest-2026.05-rc1-002
git push origin main --tags
```
