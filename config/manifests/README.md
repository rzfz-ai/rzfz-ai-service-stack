# Container Version Manifest

This directory contains the version manifest for the razzfazz.ai service stack.
The manifest enables vendor-curated container version updates without full stack
upgrades.

## Files

| File | Purpose |
|------|---------|
| `versions.json` | Current version manifest — all tracked container images |
| `versions.json.sha256` | SHA-256 checksum for integrity verification |
| `COMPATIBILITY.md` | Compatibility rules and testing checklist |
| `applied.json` | Last applied manifest (created after `--update`) |
| `previous.json` | Previous manifest (for `--update --rollback`) |

## Hosting

The manifest is hosted in the Gitea repository at:

```
https://raw.githubusercontent.com/rzfz-ai/rzfz-ai-service-stack/main/config/manifests/versions.json
```

The checksum file is at the same path with `.sha256` appended.

Customers fetch the manifest over HTTPS. Transport-layer authentication is
provided by TLS. The checksum file guards against corruption. Cosign blob
signing is a planned future enhancement for stronger supply-chain guarantees.

## Offline Use

For air-gapped or restricted-network deployments:

```bash
# On a machine with internet access:
curl -O https://github.com/rzfz-ai/rzfz-ai-service-stack/.../config/manifests/versions.json

# Transfer the file, then on the customer machine:
rzfz upgrade --update --file versions.json
```

## Signing Workflow

### Current: SHA-256 Checksum + HTTPS

```bash
# After updating versions.json:
sha256sum manifests/versions.json > manifests/versions.json.sha256
git add manifests/versions.json manifests/versions.json.sha256
git commit -m "manifest: bump <image> to <version>"
git push
```

The update engine (`razzfazz-upgrade.sh --update`) downloads both files and
verifies the checksum before applying.

### Future: Cosign Blob Signing

When stronger supply-chain guarantees are needed:

```bash
cosign sign-blob --yes manifests/versions.json > manifests/versions.json.sig
cosign verify-blob --signature manifests/versions.json.sig manifests/versions.json
```

## Update Commands

```bash
# Check for available updates (dry run)
rzfz upgrade --update --check

# Apply updates
rzfz upgrade --update

# Apply from local file (offline)
rzfz upgrade --update --file manifests/versions.json

# Roll back to previous versions
rzfz upgrade --update --rollback

# Skip confirmation prompt
rzfz upgrade --update --force
```
