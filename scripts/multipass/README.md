# Multipass VM Deployment

This directory contains scripts for deploying the razzfazz.ai stack to a Multipass VM.

## Platform Compatibility

| Platform | Script | Notes |
|----------|--------|-------|
| Linux | `razzfazz-multipass.sh` | Native bash support |
| macOS | `razzfazz-multipass.sh` | Native bash support |
| Windows | `razzfazz-multipass.ps1` | Native PowerShell 5.1+ |
| Windows (WSL2) | `razzfazz-multipass.sh` | Via WSL2 with Multipass |

## Prerequisites

- [Multipass](https://multipass.run/) installed on your host machine
- Git access to the repository (SSH key or HTTPS credentials)

## Quick Start

### Linux / macOS (Bash)
```bash
# Deploy from a specific tag
./razzfazz-multipass.sh \
    --repo https://gitlab.com/razzfazz.ai/razzfazz-ai-service-stack.git \
    --tag v1.0.0 \
    --domain mycompany.ai \
    --password 'SecurePass123!'

# Deploy from a branch
./razzfazz-multipass.sh \
    --repo git@gitlab.com:razzfazz.ai/razzfazz-ai-service-stack.git \
    --branch main \
    --domain test.local \
    --timezone Europe/Berlin
```

### Windows (PowerShell)
```powershell
# Deploy from a specific tag
.\razzfazz-multipass.ps1 `
    -Repo "https://gitlab.com/razzfazz.ai/razzfazz-ai-service-stack.git" `
    -Tag "v1.0.0" `
    -Domain "mycompany.ai" `
    -Password "SecurePass123!"

# Deploy from a branch
.\razzfazz-multipass.ps1 `
    -Repo "git@gitlab.com:razzfazz.ai/razzfazz-ai-service-stack.git" `
    -Branch "main" `
    -Domain "test.local" `
    -Timezone "Europe/Berlin"
```

## VM Specifications

Default VM configuration:
- **CPUs:** 8 cores
- **Memory:** 16 GB
- **Disk:** 100 GB
- **Image:** Ubuntu 24.04 LTS

Override with `--vm-cpus`, `--vm-memory`, `--vm-disk`, `--vm-image`.

## Available Options

### Required
| Option | Description |
|--------|-------------|
| `--repo URL` | Git repository URL to clone |
| `--tag TAG` | Git tag to checkout (e.g., v1.0.0) |
| `--branch BRANCH` | Alternative: Git branch to checkout |

### VM Configuration
| Option | Default | Description |
|--------|---------|-------------|
| `--vm-name` | razzfazz-ai | Name of the VM |
| `--vm-cpus` | 8 | Number of CPU cores |
| `--vm-memory` | 16G | RAM allocation |
| `--vm-disk` | 100G | Disk size |
| `--vm-image` | 24.04 | Ubuntu version |

### Stack Configuration
| Option | Description |
|--------|-------------|
| `-d, --domain` | Main domain for the stack |
| `-t, --timezone` | Timezone (e.g., Europe/Berlin) |
| `-p, --password` | Admin password |
| `-e, --email` | Admin email for SSL |
| `--profiles` | Comma-separated modules |
| `--scenario` | Authentik mode (base/google) |
| `--tls-mode` | TLS mode (letsencrypt/selfsigned/certificate) |
| `--gpustack-mode` | GPUStack mode (standalone/master/worker) |
| `--gpustack-server-url` | Master server URL (for worker mode) |
| `--gpustack-token` | Master server token (for worker mode) |
| `--google-client-id` | Google OAuth Client ID |
| `--google-client-secret` | Google OAuth Client Secret |
| `--package` | Package preset (single-box/master-cpu/testvm-cpu/worker-box) |
| `--smtp-mode` | SMTP mode: relay or direct |
| `--smtp-relay-host` | External SMTP relay host |
| `--smtp-relay-user` | SMTP relay username |
| `--smtp-relay-pass` | SMTP relay password |
| `--skip-build` | Skip docker build steps |
| `--no-secrets` | Don't regenerate secrets |
| `--force` | Force re-init on existing install |
| `--follow` | Follow cloud-init logs after VM creation |

## After Deployment

### Access the VM
```bash
multipass shell razzfazz-ai
```

### Monitor Deployment Progress
```bash
multipass exec razzfazz-ai -- tail -f /var/log/cloud-init-output.log
```

### Check Container Status
```bash
multipass exec razzfazz-ai -- docker compose -f /home/ubuntu/razzfazz-ai-service-stack/compose.yml ps
```

### Add to /etc/hosts
After the VM is running, add entries to your hosts file:
```bash
# Get VM IP
multipass info razzfazz-ai | grep IPv4

# Add to /etc/hosts (replace with actual IP)
192.168.64.5  mydomain.ai chat.mydomain.ai dify.mydomain.ai auth.mydomain.ai llm.mydomain.ai
```

## Cleanup

```bash
# Stop VM
multipass stop razzfazz-ai

# Delete VM
multipass delete razzfazz-ai

# Purge deleted VMs
multipass purge
```

## Troubleshooting

### SSH Key Authentication
For private repositories using SSH:
1. Ensure your SSH key is available on the host
2. Use `multipass mount` to share the key, or
3. Use HTTPS with credentials

### Cloud-Init Timeout
Cloud-init runs asynchronously. If the script seems to hang:
1. The VM is created, but deployment continues in background
2. Monitor with: `multipass exec razzfazz-ai -- tail -f /var/log/cloud-init-output.log`

### VM Already Exists
```bash
# Delete and recreate
multipass delete razzfazz-ai --purge
./razzfazz-multipass.sh [options]
```
