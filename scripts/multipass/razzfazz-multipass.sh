#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# razzfazz.ai Multipass VM Launcher
# ==============================================================================
# This script creates a Multipass VM and deploys the razzfazz.ai stack.
#
# Prerequisites:
#   - Multipass installed (https://multipass.run/)
#
# Usage:
#   ./razzfazz-multipass.sh --repo <git-url> --tag <version> [options]
#
# Examples:
#   ./razzfazz-multipass.sh --repo git@gitlab.com:razzfazz.ai/razzfazz-ai-service-stack.git \
#       --tag v1.0.0 --domain mycompany.ai --password 'SecurePass!'
# ==============================================================================

set -e

# ==============================================================================
# Color Definitions
# ==============================================================================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# ==============================================================================
# Default Values
# ==============================================================================
VM_NAME="razzfazz-ai"
VM_CPUS="8"
VM_MEMORY="16G"
VM_DISK="100G"
VM_IMAGE=""  # Auto-detect best available Ubuntu LTS

# Repository settings
REPO_URL=""
REPO_TAG=""
REPO_BRANCH=""
REPO_TOKEN=""  # GitLab/GitHub Personal Access Token for private repos

# Init script parameters (passed through to razzfazz-init.sh)
CONFIG_DOMAIN=""
CONFIG_TIMEZONE=""
CONFIG_ADMIN_PASSWORD=""
CONFIG_PROFILES=""
CONFIG_ADMIN_EMAIL=""
CONFIG_SCENARIO=""
CONFIG_TLS_MODE=""
CONFIG_GPUSTACK_MODE=""
CONFIG_GPUSTACK_SERVER_URL=""
CONFIG_GPUSTACK_TOKEN=""
CONFIG_GOOGLE_CLIENT_ID=""
CONFIG_GOOGLE_CLIENT_SECRET=""
CONFIG_PACKAGE=""
CONFIG_SMTP_MODE=""
CONFIG_SMTP_RELAY_HOST=""
CONFIG_SMTP_RELAY_USERNAME=""
CONFIG_SMTP_RELAY_PASSWORD=""
SKIP_BUILD=false
NO_SECRETS=false
FORCE_REINIT=false
SKIP_INTERACTIVE=true  # Default to non-interactive for VM deployment
FOLLOW_LOGS=false      # Follow cloud-init logs after VM creation

# ==============================================================================
# Utility Functions
# ==============================================================================
print_step() {
    echo -e "${CYAN}▶ $1${NC}"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

# ==============================================================================
# Help
# ==============================================================================
show_help() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Creates a Multipass VM and deploys the razzfazz.ai stack."
    echo ""
    echo "Required Options:"
    echo "  --repo URL                  Git repository URL to clone"
    echo "  --tag TAG                   Git tag to checkout (e.g., v1.0.0)"
    echo "  OR"
    echo "  --branch BRANCH             Git branch to checkout (alternative to --tag)"
    echo ""
    echo "Authentication:"
    echo "  --token TOKEN               GitLab/GitHub Personal Access Token for private repos"
    echo ""
    echo "VM Options:"
    echo "  --vm-name NAME              VM name (default: $VM_NAME)"
    echo "  --vm-cpus NUM               Number of CPUs (default: $VM_CPUS)"
    echo "  --vm-memory SIZE            Memory size (default: $VM_MEMORY)"
    echo "  --vm-disk SIZE              Disk size (default: $VM_DISK)"
    echo "  --vm-image IMAGE            Ubuntu image (default: $VM_IMAGE)"
    echo ""
    echo "Stack Configuration (passed to razzfazz-init.sh):"
    echo "  -d, --domain DOMAIN         Main domain for the stack"
    echo "  -t, --timezone TZ           Timezone (e.g., Europe/Berlin)"
    echo "  -p, --password PASSWORD     Admin password"
    echo "  -e, --email EMAIL           Admin email for SSL certificates"
    echo "  --profiles PROFILES         Comma-separated list of modules"
    echo "  --scenario SCENARIO         Authentik scenario (base or google)"
    echo "  --tls-mode MODE             TLS mode (letsencrypt or selfsigned)"
    echo "  --gpustack-mode MODE        GPUStack mode (standalone, master, or worker)"
    echo "  --gpustack-server-url URL   Master server URL (for worker mode)"
    echo "  --gpustack-token TOKEN      Master server token (for worker mode)"
    echo "  --google-client-id ID       Google OAuth Client ID (for google scenario)"
    echo "  --google-client-secret S    Google OAuth Client Secret (for google scenario)"
    echo "  --package PACKAGE           Package preset (single-box, master-cpu, testvm-cpu, worker-box)"
    echo "  --smtp-mode MODE            SMTP relay mode: relay (via external SMTP) or direct (to MX servers)"
    echo "  --smtp-relay-host HOST      External SMTP relay host (e.g., smtp.gmail.com)"
    echo "  --smtp-relay-user USER      SMTP relay username (e.g., email address)"
    echo "  --smtp-relay-pass PASS      SMTP relay password (e.g., app password)"
    echo "  --skip-build                Skip docker build/pull steps"
    echo "  --no-secrets                Don't regenerate secrets (use existing values)"
    echo "  --force                     Force re-initialization on existing installation"
    echo ""
    echo "Other Options:"
    echo "  --follow                    Follow cloud-init logs after VM creation"
    echo "  -h, --help                  Show this help message"
    echo ""
    echo "Examples:"
    echo "  # Deploy with specific tag"
    echo "  $0 --repo https://gitlab.com/razzfazz.ai/stack.git --tag v1.0.0 \\"
    echo "     --domain mycompany.ai --password 'SecurePass123!'"
    echo ""
    echo "  # Deploy from branch with custom VM specs"
    echo "  $0 --repo git@gitlab.com:razzfazz.ai/stack.git --branch main \\"
    echo "     --vm-cpus 16 --vm-memory 32G --domain test.local"
    echo ""
    echo "  # Deploy private repo with access token"
    echo "  $0 --repo https://gitlab.com/razzfazz.ai/stack.git --branch main \\"
    echo "     --token glpat-xxxxxxxxxxxx --domain mycompany.ai"
    echo ""
    echo "  # Deploy using a package preset"
    echo "  $0 --repo https://gitlab.com/razzfazz.ai/stack.git --branch main \\"
    echo "     --token glpat-xxxxxxxxxxxx --package single-box --domain myai.local --password 'Secret!'"
    echo ""
    echo "  # Deploy as a GPU worker node"
    echo "  $0 --repo https://gitlab.com/razzfazz.ai/stack.git --branch main \\"
    echo "     --token glpat-xxxxxxxxxxxx --package worker-box --domain worker.local --password 'Secret!' \\"
    echo "     --gpustack-server-url http://master:9090 --gpustack-token MASTER_TOKEN"
    echo ""
    exit 0
}

# ==============================================================================
# Check Prerequisites
# ==============================================================================
check_prerequisites() {
    print_step "Checking prerequisites..."
    
    if ! command -v multipass &> /dev/null; then
        print_error "Multipass is not installed."
        echo "  Install it from: https://multipass.run/"
        exit 1
    fi
    
    MULTIPASS_VERSION=$(multipass version | head -1)
    print_success "Multipass found: $MULTIPASS_VERSION"
    
    # Auto-detect best Ubuntu image if not specified
    if [ -z "$VM_IMAGE" ]; then
        print_step "Detecting available Ubuntu images..."
        # Try to find the best available LTS image (prefer 24.04, then 22.04, then 20.04)
        for img in "24.04" "noble" "22.04" "jammy" "20.04" "focal"; do
            if multipass find 2>/dev/null | grep -q "$img"; then
                VM_IMAGE="$img"
                print_success "Using Ubuntu image: $VM_IMAGE"
                break
            fi
        done
        
        # If no standard image found, try generic 'lts' or just launch without specifying
        if [ -z "$VM_IMAGE" ]; then
            # On older multipass, just use 'lts' or empty (uses default)
            VM_IMAGE="lts"
            print_warning "No specific Ubuntu version found. Using 'lts' (default image)."
        fi
    fi
}

# ==============================================================================
# Generate Cloud-Init Configuration
# ==============================================================================
generate_cloud_init() {
    local cloud_init_file="$1"
    
    # Build the init script command
    local init_args=""
    [ -n "$CONFIG_PACKAGE" ] && init_args="$init_args --package '$CONFIG_PACKAGE'"
    [ -n "$CONFIG_DOMAIN" ] && init_args="$init_args --domain '$CONFIG_DOMAIN'"
    [ -n "$CONFIG_TIMEZONE" ] && init_args="$init_args --timezone '$CONFIG_TIMEZONE'"
    [ -n "$CONFIG_ADMIN_PASSWORD" ] && init_args="$init_args --password '$CONFIG_ADMIN_PASSWORD'"
    [ -n "$CONFIG_ADMIN_EMAIL" ] && init_args="$init_args --email '$CONFIG_ADMIN_EMAIL'"
    [ -n "$CONFIG_PROFILES" ] && init_args="$init_args --profiles '$CONFIG_PROFILES'"
    [ -n "$CONFIG_SCENARIO" ] && init_args="$init_args --scenario '$CONFIG_SCENARIO'"
    [ -n "$CONFIG_TLS_MODE" ] && init_args="$init_args --tls-mode '$CONFIG_TLS_MODE'"
    [ -n "$CONFIG_GPUSTACK_MODE" ] && init_args="$init_args --gpustack-mode '$CONFIG_GPUSTACK_MODE'"
    [ -n "$CONFIG_GPUSTACK_SERVER_URL" ] && init_args="$init_args --gpustack-server-url '$CONFIG_GPUSTACK_SERVER_URL'"
    [ -n "$CONFIG_GPUSTACK_TOKEN" ] && init_args="$init_args --gpustack-token '$CONFIG_GPUSTACK_TOKEN'"
    [ -n "$CONFIG_GOOGLE_CLIENT_ID" ] && init_args="$init_args --google-client-id '$CONFIG_GOOGLE_CLIENT_ID'"
    [ -n "$CONFIG_GOOGLE_CLIENT_SECRET" ] && init_args="$init_args --google-client-secret '$CONFIG_GOOGLE_CLIENT_SECRET'"
    [ -n "$CONFIG_SMTP_MODE" ] && init_args="$init_args --smtp-mode '$CONFIG_SMTP_MODE'"
    [ -n "$CONFIG_SMTP_RELAY_HOST" ] && init_args="$init_args --smtp-relay-host '$CONFIG_SMTP_RELAY_HOST'"
    [ -n "$CONFIG_SMTP_RELAY_USERNAME" ] && init_args="$init_args --smtp-relay-user '$CONFIG_SMTP_RELAY_USERNAME'"
    [ -n "$CONFIG_SMTP_RELAY_PASSWORD" ] && init_args="$init_args --smtp-relay-pass '$CONFIG_SMTP_RELAY_PASSWORD'"
    [ "$SKIP_BUILD" = true ] && init_args="$init_args --skip-build"
    [ "$NO_SECRETS" = true ] && init_args="$init_args --no-secrets"
    [ "$FORCE_REINIT" = true ] && init_args="$init_args --force"
    init_args="$init_args --skip-interactive"
    
    # Determine git checkout command
    local git_checkout=""
    if [ -n "$REPO_TAG" ]; then
        git_checkout="git checkout tags/$REPO_TAG"
    elif [ -n "$REPO_BRANCH" ]; then
        git_checkout="git checkout $REPO_BRANCH"
    fi
    
    # Build the git clone URL with token if provided
    local git_clone_url="$REPO_URL"
    if [ -n "$REPO_TOKEN" ]; then
        # Insert token into HTTPS URL: https://gitlab.com/... -> https://oauth2:TOKEN@gitlab.com/...
        if [[ "$REPO_URL" == https://* ]]; then
            git_clone_url=$(echo "$REPO_URL" | sed "s|https://|https://oauth2:${REPO_TOKEN}@|")
        else
            print_warning "Token provided but URL is not HTTPS. Token will be ignored."
        fi
    fi
    
    cat > "$cloud_init_file" << EOF
#cloud-config
# Note: package_upgrade disabled to prevent VM reboot during deployment
# System updates can cause kernel/systemd upgrades that trigger reboots,
# breaking the cloud-init process on macOS with Multipass
package_update: true
package_upgrade: false

packages:
  - git
  - curl
  - ca-certificates

runcmd:
  # Install Docker
  - curl -fsSL https://get.docker.com | sh
  - usermod -aG docker ubuntu
  
  # Clone repository
  - cd /home/ubuntu
  - git clone $git_clone_url razzfazz-ai-service-stack
  - cd razzfazz-ai-service-stack
  - $git_checkout
  - chown -R ubuntu:ubuntu /home/ubuntu/razzfazz-ai-service-stack
  
  # Remove token from git remote URL (security)
  - cd /home/ubuntu/razzfazz-ai-service-stack && git remote set-url origin $REPO_URL || true
  
  # Run init script
  - cd /home/ubuntu/razzfazz-ai-service-stack
  - chmod +x razzfazz-init.sh
  - sudo -u ubuntu rzfz init $init_args

final_message: "razzfazz.ai stack deployment complete after \$UPTIME seconds"
EOF
    
    print_success "Generated cloud-init configuration"
}

# ==============================================================================
# Create and Launch VM
# ==============================================================================
launch_vm() {
    # macOS compatible mktemp (doesn't support suffix in template)
    local cloud_init_file
    cloud_init_file=$(mktemp /tmp/razzfazz-cloud-init.XXXXXX)
    mv "$cloud_init_file" "${cloud_init_file}.yaml"
    cloud_init_file="${cloud_init_file}.yaml"
    
    # Generate cloud-init
    generate_cloud_init "$cloud_init_file"
    
    print_step "Creating Multipass VM '$VM_NAME'..."
    echo "  CPUs:   $VM_CPUS"
    echo "  Memory: $VM_MEMORY"
    echo "  Disk:   $VM_DISK"
    echo "  Image:  Ubuntu $VM_IMAGE"
    echo ""
    
    # Check if VM already exists
    if multipass list | grep -q "^$VM_NAME "; then
        print_warning "VM '$VM_NAME' already exists."
        read -p "Delete and recreate? [y/N]: " confirm
        if [[ "$confirm" =~ ^[Yy]$ ]]; then
            print_step "Deleting existing VM..."
            multipass delete "$VM_NAME" --purge
        else
            print_error "Aborted. Use --vm-name to specify a different name."
            rm -f "$cloud_init_file"
            exit 1
        fi
    fi
    
    # Launch VM
    print_step "Launching VM (this may take several minutes)..."
    
    # Detect multipass version and use correct memory flag
    # Older versions (< 1.10) use --mem, newer versions use --memory
    local mem_flag="--memory"
    if multipass launch --help 2>&1 | grep -q "\-\-mem "; then
        mem_flag="--mem"
    fi
    
    if [ "$FOLLOW_LOGS" = true ]; then
        # With --follow: Launch with long timeout, then show complete log
        # Cloud-init takes 5-15 minutes, so use 1800s (30 min) timeout
        print_step "Launching VM and waiting for deployment (this takes 5-15 minutes)..."
        echo ""
        print_warning "Tip: You can watch logs in another terminal with:"
        echo "         multipass exec $VM_NAME -- tail -f /var/log/cloud-init-output.log"
        echo ""
        
        multipass launch "$VM_IMAGE" \
            --name "$VM_NAME" \
            --cpus "$VM_CPUS" \
            $mem_flag "$VM_MEMORY" \
            --disk "$VM_DISK" \
            --timeout 1800 \
            --cloud-init "$cloud_init_file"
        
        # Cleanup
        rm -f "$cloud_init_file"
        
        print_success "VM '$VM_NAME' deployment completed!"
        echo ""
        echo -e "${CYAN}═══════════════════════════════════════════════════════════════════${NC}"
        echo -e "${CYAN}                    Cloud-Init Deployment Log                       ${NC}"
        echo -e "${CYAN}═══════════════════════════════════════════════════════════════════${NC}"
        echo ""
        
        # Show the complete cloud-init log
        multipass exec "$VM_NAME" -- cat /var/log/cloud-init-output.log 2>/dev/null || true
        
        echo ""
        echo -e "${CYAN}═══════════════════════════════════════════════════════════════════${NC}"
        print_success "Deployment log complete!"
    else
        # Without --follow: Normal launch, cloud-init runs in background
        multipass launch "$VM_IMAGE" \
            --name "$VM_NAME" \
            --cpus "$VM_CPUS" \
            $mem_flag "$VM_MEMORY" \
            --disk "$VM_DISK" \
            --timeout 300 \
            --cloud-init "$cloud_init_file"
        
        # Cleanup
        rm -f "$cloud_init_file"
        
        print_success "VM '$VM_NAME' created successfully!"
    fi
}

# ==============================================================================
# Show VM Info
# ==============================================================================
show_vm_info() {
    echo ""
    echo -e "${GREEN}╔══════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║               razzfazz.ai VM Created Successfully!               ║${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    
    # Get VM IP
    local vm_ip=$(multipass info "$VM_NAME" | grep "IPv4" | awk '{print $2}')
    
    echo "VM Details:"
    echo "  Name:     $VM_NAME"
    echo "  IP:       $vm_ip"
    echo ""
    echo "Access VM:"
    echo "  multipass shell $VM_NAME"
    echo ""
    echo "View cloud-init logs:"
    echo "  multipass exec $VM_NAME -- tail -f /var/log/cloud-init-output.log"
    echo ""
    echo "Check deployment status:"
    echo "  multipass exec $VM_NAME -- docker compose -f /home/ubuntu/razzfazz-ai-service-stack/compose.yml ps"
    echo ""
    
    # Show /etc/hosts configuration
    echo -e "${CYAN}═══════════════════════════════════════════════════════════════════${NC}"
    echo -e "${CYAN}                    /etc/hosts Configuration                        ${NC}"
    echo -e "${CYAN}═══════════════════════════════════════════════════════════════════${NC}"
    echo ""
    
    if [ -n "$CONFIG_DOMAIN" ]; then
        echo "Add the following line to your /etc/hosts file to access the services:"
        echo ""
        echo -e "${YELLOW}  $vm_ip  $CONFIG_DOMAIN chat.$CONFIG_DOMAIN dify.$CONFIG_DOMAIN auth.$CONFIG_DOMAIN llm.$CONFIG_DOMAIN${NC}"
        echo ""
        echo "Quick command (run on your host machine):"
        echo ""
        echo "  sudo bash -c 'echo \"$vm_ip  $CONFIG_DOMAIN chat.$CONFIG_DOMAIN dify.$CONFIG_DOMAIN auth.$CONFIG_DOMAIN llm.$CONFIG_DOMAIN\" >> /etc/hosts'"
        echo ""
        echo "After deployment, access your services at:"
        echo "  • Chat:     https://chat.$CONFIG_DOMAIN"
        echo "  • Dify:     https://dify.$CONFIG_DOMAIN"
        echo "  • Auth:     https://auth.$CONFIG_DOMAIN"
        echo "  • LLM:      https://llm.$CONFIG_DOMAIN"
    else
        echo "No domain configured. Add to /etc/hosts manually:"
        echo ""
        echo -e "${YELLOW}  $vm_ip  razzfazz.local chat.razzfazz.local dify.razzfazz.local auth.razzfazz.local llm.razzfazz.local${NC}"
    fi
    
    echo ""
    echo -e "${CYAN}═══════════════════════════════════════════════════════════════════${NC}"
    echo ""
    if [ "$FOLLOW_LOGS" = false ]; then
        print_warning "Note: Cloud-init deployment continues in background (takes ~5-10 minutes)."
        echo "         Use 'multipass exec $VM_NAME -- tail -f /var/log/cloud-init-output.log' to monitor."
        echo "         Or re-run with --follow to watch the deployment."
    fi
}

# ==============================================================================
# Parse Command Line Arguments
# ==============================================================================
while [[ $# -gt 0 ]]; do
    case $1 in
        --repo)
            REPO_URL="$2"
            shift 2
            ;;
        --tag)
            REPO_TAG="$2"
            shift 2
            ;;
        --branch)
            REPO_BRANCH="$2"
            shift 2
            ;;
        --token)
            REPO_TOKEN="$2"
            shift 2
            ;;
        --vm-name)
            VM_NAME="$2"
            shift 2
            ;;
        --vm-cpus)
            VM_CPUS="$2"
            shift 2
            ;;
        --vm-memory)
            VM_MEMORY="$2"
            shift 2
            ;;
        --vm-disk)
            VM_DISK="$2"
            shift 2
            ;;
        --vm-image)
            VM_IMAGE="$2"
            shift 2
            ;;
        -d|--domain)
            CONFIG_DOMAIN="$2"
            shift 2
            ;;
        -t|--timezone)
            CONFIG_TIMEZONE="$2"
            shift 2
            ;;
        -p|--password)
            CONFIG_ADMIN_PASSWORD="$2"
            shift 2
            ;;
        -e|--email)
            CONFIG_ADMIN_EMAIL="$2"
            shift 2
            ;;
        --profiles)
            CONFIG_PROFILES="$2"
            shift 2
            ;;
        --scenario)
            CONFIG_SCENARIO="$2"
            shift 2
            ;;
        --tls-mode)
            CONFIG_TLS_MODE="$2"
            shift 2
            ;;
        --gpustack-mode)
            CONFIG_GPUSTACK_MODE="$2"
            shift 2
            ;;
        --gpustack-server-url)
            CONFIG_GPUSTACK_SERVER_URL="$2"
            shift 2
            ;;
        --gpustack-token)
            CONFIG_GPUSTACK_TOKEN="$2"
            shift 2
            ;;
        --google-client-id)
            CONFIG_GOOGLE_CLIENT_ID="$2"
            shift 2
            ;;
        --google-client-secret)
            CONFIG_GOOGLE_CLIENT_SECRET="$2"
            shift 2
            ;;
        --package)
            CONFIG_PACKAGE="$2"
            shift 2
            ;;
        --smtp-mode)
            CONFIG_SMTP_MODE="$2"
            shift 2
            ;;
        --smtp-relay-host)
            CONFIG_SMTP_RELAY_HOST="$2"
            shift 2
            ;;
        --smtp-relay-user)
            CONFIG_SMTP_RELAY_USERNAME="$2"
            shift 2
            ;;
        --smtp-relay-pass)
            CONFIG_SMTP_RELAY_PASSWORD="$2"
            shift 2
            ;;
        --skip-build)
            SKIP_BUILD=true
            shift
            ;;
        --no-secrets)
            NO_SECRETS=true
            shift
            ;;
        --force)
            FORCE_REINIT=true
            shift
            ;;
        --follow)
            FOLLOW_LOGS=true
            shift
            ;;
        -h|--help)
            show_help
            ;;
        *)
            print_error "Unknown option: $1"
            echo "Use --help for usage information."
            exit 1
            ;;
    esac
done

# ==============================================================================
# Validate Required Arguments
# ==============================================================================
if [ -z "$REPO_URL" ]; then
    print_error "Repository URL is required. Use --repo <url>"
    exit 1
fi

if [ -z "$REPO_TAG" ] && [ -z "$REPO_BRANCH" ]; then
    print_error "Either --tag or --branch is required."
    exit 1
fi

# ==============================================================================
# Main Execution
# ==============================================================================
echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║            razzfazz.ai Multipass VM Launcher                     ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════════════════════════╝${NC}"
echo ""

check_prerequisites
launch_vm
show_vm_info

# Note: --follow log streaming is now handled inside launch_vm function

echo ""
print_success "Done!"
