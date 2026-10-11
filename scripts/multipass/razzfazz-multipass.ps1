# ==============================================================================
# razzfazz.ai Multipass VM Launcher (PowerShell)
# ==============================================================================
# This script creates a Multipass VM and deploys the razzfazz.ai stack.
#
# Prerequisites:
#   - Multipass installed (https://multipass.run/)
#   - PowerShell 5.1+ (Windows) or PowerShell Core (cross-platform)
#
# Usage:
#   .\razzfazz-multipass.ps1 -Repo <git-url> -Tag <version> [options]
#
# Examples:
#   .\razzfazz-multipass.ps1 -Repo "https://gitlab.com/razzfazz.ai/stack.git" `
#       -Tag "v1.0.0" -Domain "mycompany.ai" -Password "SecurePass!"
# ==============================================================================

[CmdletBinding()]
param(
    # Repository settings (required)
    [Parameter(Mandatory=$false)]
    [string]$Repo,
    
    [Parameter(Mandatory=$false)]
    [string]$Token,  # GitLab Personal Access Token for private repos
    
    [Parameter(Mandatory=$false)]
    [string]$Tag,
    
    [Parameter(Mandatory=$false)]
    [string]$Branch,
    
    # VM Configuration
    [Parameter(Mandatory=$false)]
    [string]$VmName = "razzfazz-ai",
    
    [Parameter(Mandatory=$false)]
    [string]$VmCpus = "8",
    
    [Parameter(Mandatory=$false)]
    [string]$VmMemory = "16G",
    
    [Parameter(Mandatory=$false)]
    [string]$VmDisk = "100G",
    
    [Parameter(Mandatory=$false)]
    [string]$VmImage = "24.04",
    
    # Stack Configuration (passed to razzfazz-init.sh)
    [Parameter(Mandatory=$false)]
    [Alias("d")]
    [string]$Domain,
    
    [Parameter(Mandatory=$false)]
    [Alias("t")]
    [string]$Timezone,
    
    [Parameter(Mandatory=$false)]
    [Alias("p")]
    [string]$Password,
    
    [Parameter(Mandatory=$false)]
    [Alias("e")]
    [string]$Email,
    
    [Parameter(Mandatory=$false)]
    [string]$Profiles,
    
    [Parameter(Mandatory=$false)]
    [string]$Scenario,
    
    [Parameter(Mandatory=$false)]
    [string]$TlsMode,
    
    [Parameter(Mandatory=$false)]
    [string]$GpustackMode,
    
    [Parameter(Mandatory=$false)]
    [string]$GpustackServerUrl,
    
    [Parameter(Mandatory=$false)]
    [string]$GpustackToken,
    
    [Parameter(Mandatory=$false)]
    [string]$GoogleClientId,
    
    [Parameter(Mandatory=$false)]
    [string]$GoogleClientSecret,
    
    [Parameter(Mandatory=$false)]
    [string]$Package,
    
    [Parameter(Mandatory=$false)]
    [string]$SmtpMode,
    
    [Parameter(Mandatory=$false)]
    [string]$SmtpRelayHost,
    
    [Parameter(Mandatory=$false)]
    [string]$SmtpRelayUser,
    
    [Parameter(Mandatory=$false)]
    [string]$SmtpRelayPass,
    
    [Parameter(Mandatory=$false)]
    [switch]$SkipBuild,
    
    [Parameter(Mandatory=$false)]
    [switch]$NoSecrets,
    
    [Parameter(Mandatory=$false)]
    [switch]$Force,
    
    [Parameter(Mandatory=$false)]
    [switch]$Follow,  # Show cloud-init logs after deployment
    
    [Parameter(Mandatory=$false)]
    [Alias("h")]
    [switch]$Help
)

# ==============================================================================
# Color Functions
# ==============================================================================
function Write-Step {
    param([string]$Message)
    Write-Host "▶ $Message" -ForegroundColor Cyan
}

function Write-Success {
    param([string]$Message)
    Write-Host "✓ $Message" -ForegroundColor Green
}

function Write-Warning {
    param([string]$Message)
    Write-Host "⚠ $Message" -ForegroundColor Yellow
}

function Write-Error {
    param([string]$Message)
    Write-Host "✗ $Message" -ForegroundColor Red
}

# ==============================================================================
# Help
# ==============================================================================
function Show-Help {
    Write-Host @"

Usage: .\razzfazz-multipass.ps1 [OPTIONS]

Creates a Multipass VM and deploys the razzfazz.ai stack.

Required Options:
  -Repo URL                   Git repository URL to clone
  -Token TOKEN                GitLab Personal Access Token for private repos
  -Tag TAG                    Git tag to checkout (e.g., v1.0.0)
  OR
  -Branch BRANCH              Git branch to checkout (alternative to -Tag)

VM Options:
  -VmName NAME                VM name (default: razzfazz-ai)
  -VmCpus NUM                 Number of CPUs (default: 8)
  -VmMemory SIZE              Memory size (default: 16G)
  -VmDisk SIZE                Disk size (default: 100G)
  -VmImage IMAGE              Ubuntu image (default: 24.04)

Stack Configuration (passed to razzfazz-init.sh):
  -Domain, -d DOMAIN          Main domain for the stack
  -Timezone, -t TZ            Timezone (e.g., Europe/Berlin)
  -Password, -p PASSWORD      Admin password
  -Email, -e EMAIL            Admin email for SSL certificates
  -Profiles PROFILES          Comma-separated list of modules
  -Scenario SCENARIO          Authentik scenario (base or google)
  -TlsMode MODE               TLS mode (letsencrypt or selfsigned)
  -GpustackMode MODE          GPUStack mode (standalone, master, or worker)
  -GpustackServerUrl URL      Master server URL (for worker mode)
  -GpustackToken TOKEN        Master server token (for worker mode)
  -GoogleClientId ID          Google OAuth Client ID (for google scenario)
  -GoogleClientSecret SECRET  Google OAuth Client Secret (for google scenario)
  -Package PACKAGE            Package preset (single-box, master-cpu, testvm-cpu, worker-box)
  -SmtpMode MODE              SMTP relay mode: relay (via external SMTP) or direct (to MX servers)
  -SmtpRelayHost HOST         External SMTP relay host (e.g., smtp.gmail.com)
  -SmtpRelayUser USER         SMTP relay username (e.g., email address)
  -SmtpRelayPass PASS         SMTP relay password (e.g., app password)
  -SkipBuild                  Skip docker build/pull steps
  -NoSecrets                  Don't regenerate secrets (use existing values)
  -Force                      Force re-initialization on existing installation

Other Options:
  -Follow                     Show cloud-init logs after deployment completes
  -Help, -h                   Show this help message

Examples:
  # Deploy from private GitLab repo with token
  .\razzfazz-multipass.ps1 -Repo "https://gitlab.com/razzfazz.ai/stack.git" ``
      -Token "glpat-xxxxx" -Branch "main" -Domain "mycompany.ai" -Password "SecurePass123!" -Follow

  # Deploy with specific tag
  .\razzfazz-multipass.ps1 -Repo "https://gitlab.com/razzfazz.ai/stack.git" ``
      -Tag "v1.0.0" -Domain "mycompany.ai" -Password "SecurePass123!"

  # Deploy from branch with custom VM specs
  .\razzfazz-multipass.ps1 -Repo "git@gitlab.com:razzfazz.ai/stack.git" ``
      -Branch "main" -VmCpus 16 -VmMemory "32G" -Domain "test.local"

  # Deploy using a package preset
  .\razzfazz-multipass.ps1 -Repo "https://gitlab.com/razzfazz.ai/stack.git" ``
      -Token "glpat-xxxxx" -Branch "main" -Package "single-box" -Domain "myai.local" -Password "Secret!"

  # Deploy as a GPU worker node
  .\razzfazz-multipass.ps1 -Repo "https://gitlab.com/razzfazz.ai/stack.git" ``
      -Token "glpat-xxxxx" -Branch "main" -Package "worker-box" -Domain "worker.local" -Password "Secret!" ``
      -GpustackServerUrl "http://master:9090" -GpustackToken "MASTER_TOKEN"

"@
    exit 0
}

# ==============================================================================
# Check Prerequisites
# ==============================================================================
function Test-Prerequisites {
    Write-Step "Checking prerequisites..."
    
    $multipass = Get-Command multipass -ErrorAction SilentlyContinue
    if (-not $multipass) {
        Write-Error "Multipass is not installed."
        Write-Host "  Install it from: https://multipass.run/"
        exit 1
    }
    
    $version = & multipass version 2>&1 | Select-Object -First 1
    Write-Success "Multipass found: $version"
}

# ==============================================================================
# Generate Cloud-Init Configuration
# ==============================================================================
function New-CloudInitConfig {
    param([string]$OutputPath)
    
    # Build the init script command
    $initArgs = @()
    if ($Package) { $initArgs += "--package '$Package'" }
    if ($Domain) { $initArgs += "--domain '$Domain'" }
    if ($Timezone) { $initArgs += "--timezone '$Timezone'" }
    if ($Password) { $initArgs += "--password '$Password'" }
    if ($Email) { $initArgs += "--email '$Email'" }
    if ($Profiles) { $initArgs += "--profiles '$Profiles'" }
    if ($Scenario) { $initArgs += "--scenario '$Scenario'" }
    if ($TlsMode) { $initArgs += "--tls-mode '$TlsMode'" }
    if ($GpustackMode) { $initArgs += "--gpustack-mode '$GpustackMode'" }
    if ($GpustackServerUrl) { $initArgs += "--gpustack-server-url '$GpustackServerUrl'" }
    if ($GpustackToken) { $initArgs += "--gpustack-token '$GpustackToken'" }
    if ($GoogleClientId) { $initArgs += "--google-client-id '$GoogleClientId'" }
    if ($GoogleClientSecret) { $initArgs += "--google-client-secret '$GoogleClientSecret'" }
    if ($SmtpMode) { $initArgs += "--smtp-mode '$SmtpMode'" }
    if ($SmtpRelayHost) { $initArgs += "--smtp-relay-host '$SmtpRelayHost'" }
    if ($SmtpRelayUser) { $initArgs += "--smtp-relay-user '$SmtpRelayUser'" }
    if ($SmtpRelayPass) { $initArgs += "--smtp-relay-pass '$SmtpRelayPass'" }
    if ($SkipBuild) { $initArgs += "--skip-build" }
    if ($NoSecrets) { $initArgs += "--no-secrets" }
    if ($Force) { $initArgs += "--force" }
    $initArgs += "--skip-interactive"
    
    $initArgsString = $initArgs -join " "
    
    # Determine git checkout command
    $gitCheckout = ""
    if ($Tag) {
        $gitCheckout = "git checkout tags/$Tag"
    } elseif ($Branch) {
        $gitCheckout = "git checkout $Branch"
    }
    
    # Build git clone URL with token if provided
    $gitCloneUrl = $Repo
    if ($Token) {
        # Insert token into HTTPS URL: https://oauth2:token@gitlab.com/...
        if ($Repo -match "^https://(.+)") {
            $gitCloneUrl = "https://oauth2:${Token}@$($Matches[1])"
        }
    }
    
    $cloudInit = @"
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
  - git clone $gitCloneUrl razzfazz-ai-service-stack
  - cd razzfazz-ai-service-stack
  - $gitCheckout
  - chown -R ubuntu:ubuntu /home/ubuntu/razzfazz-ai-service-stack
  
  # Remove token from git remote (security)
  - cd /home/ubuntu/razzfazz-ai-service-stack
  - git remote set-url origin $Repo
  
  # Run init script
  - cd /home/ubuntu/razzfazz-ai-service-stack
  - chmod +x razzfazz-init.sh
  - sudo -u ubuntu rzfz init $initArgsString

final_message: "razzfazz.ai stack deployment complete after \`$UPTIME seconds"
"@
    
    $cloudInit | Out-File -FilePath $OutputPath -Encoding utf8 -NoNewline
    Write-Success "Generated cloud-init configuration"
}

# ==============================================================================
# Create and Launch VM
# ==============================================================================
function Start-MultipassVM {
    $cloudInitFile = Join-Path $env:TEMP "razzfazz-cloud-init-$(Get-Random).yaml"
    
    # Generate cloud-init
    New-CloudInitConfig -OutputPath $cloudInitFile
    
    Write-Step "Creating Multipass VM '$VmName'..."
    Write-Host "  CPUs:   $VmCpus"
    Write-Host "  Memory: $VmMemory"
    Write-Host "  Disk:   $VmDisk"
    Write-Host "  Image:  Ubuntu $VmImage"
    Write-Host ""
    
    # Check if VM already exists
    $vmList = & multipass list 2>&1
    if ($vmList -match "^$VmName\s") {
        Write-Warning "VM '$VmName' already exists."
        $confirm = Read-Host "Delete and recreate? [y/N]"
        if ($confirm -match "^[Yy]$") {
            Write-Step "Deleting existing VM..."
            & multipass delete $VmName --purge
        } else {
            Write-Error "Aborted. Use -VmName to specify a different name."
            Remove-Item -Path $cloudInitFile -Force -ErrorAction SilentlyContinue
            exit 1
        }
    }
    
    # Launch VM with appropriate timeout
    if ($Follow) {
        Write-Step "Launching VM and waiting for deployment (this takes 5-15 minutes)..."
        Write-Host ""
        Write-Warning "Tip: You can watch logs in another terminal with:"
        Write-Host "         multipass exec $VmName -- tail -f /var/log/cloud-init-output.log"
        Write-Host ""
        
        & multipass launch $VmImage `
            --name $VmName `
            --cpus $VmCpus `
            --memory $VmMemory `
            --disk $VmDisk `
            --timeout 1800 `
            --cloud-init $cloudInitFile
    } else {
        Write-Step "Launching VM (this may take several minutes)..."
        & multipass launch $VmImage `
            --name $VmName `
            --cpus $VmCpus `
            --memory $VmMemory `
            --disk $VmDisk `
            --timeout 300 `
            --cloud-init $cloudInitFile
    }
    
    # Cleanup
    Remove-Item -Path $cloudInitFile -Force -ErrorAction SilentlyContinue
    
    Write-Success "VM '$VmName' created successfully!"
}

# ==============================================================================
# Show VM Info
# ==============================================================================
function Show-VMInfo {
    Write-Host ""
    Write-Host "╔══════════════════════════════════════════════════════════════════╗" -ForegroundColor Green
    Write-Host "║               razzfazz.ai VM Created Successfully!               ║" -ForegroundColor Green
    Write-Host "╚══════════════════════════════════════════════════════════════════╝" -ForegroundColor Green
    Write-Host ""
    
    # Get VM IP
    $vmInfo = & multipass info $VmName 2>&1
    $vmIp = ($vmInfo | Select-String -Pattern "IPv4:\s+(.+)" | ForEach-Object { $_.Matches.Groups[1].Value }).Trim()
    
    Write-Host "VM Details:"
    Write-Host "  Name:     $VmName"
    Write-Host "  IP:       $vmIp"
    Write-Host ""
    Write-Host "Access VM:"
    Write-Host "  multipass shell $VmName"
    Write-Host ""
    Write-Host "View cloud-init logs:"
    Write-Host "  multipass exec $VmName -- tail -f /var/log/cloud-init-output.log"
    Write-Host ""
    Write-Host "Check deployment status:"
    Write-Host "  multipass exec $VmName -- docker compose -f /home/ubuntu/razzfazz-ai-service-stack/compose.yml ps"
    Write-Host ""
    
    if ($Domain) {
        Write-Host ""
        Write-Host "═══════════════════════════════════════════════════════════════════" -ForegroundColor Yellow
        Write-Host "                    hosts File Configuration                        " -ForegroundColor Yellow
        Write-Host "═══════════════════════════════════════════════════════════════════" -ForegroundColor Yellow
        Write-Host ""
        Write-Host "Add the following line to your hosts file:" -ForegroundColor White
        Write-Host "  Location: C:\Windows\System32\drivers\etc\hosts" -ForegroundColor Gray
        Write-Host ""
        
        $hostsEntry = "$vmIp  $Domain chat.$Domain dify.$Domain auth.$Domain llm.$Domain"
        Write-Host "  $hostsEntry" -ForegroundColor Yellow
        Write-Host ""
        
        Write-Host "PowerShell command (Run as Administrator):" -ForegroundColor White
        Write-Host ""
        Write-Host "  Add-Content -Path 'C:\Windows\System32\drivers\etc\hosts' -Value '$hostsEntry'" -ForegroundColor Cyan
        Write-Host ""
        
        Write-Host "After deployment, access:" -ForegroundColor White
        Write-Host "  • Chat:  https://chat.$Domain"
        Write-Host "  • Dify:  https://dify.$Domain"
        Write-Host "  • Auth:  https://auth.$Domain"
        Write-Host "  • LLM:   https://llm.$Domain"
        Write-Host ""
    }
    
    if (-not $Follow) {
        Write-Warning "Note: Cloud-init deployment continues in background (takes ~5-10 minutes)."
        Write-Host "         Use 'multipass exec $VmName -- tail -f /var/log/cloud-init-output.log' to monitor."
        Write-Host "         Or re-run with -Follow to wait and show the deployment log."
    }
}

# ==============================================================================
# Main Execution
# ==============================================================================

# Show help if requested
if ($Help) {
    Show-Help
}

# Validate required arguments
if (-not $Repo) {
    Write-Error "Repository URL is required. Use -Repo <url>"
    Write-Host "Use -Help for usage information."
    exit 1
}

if (-not $Tag -and -not $Branch) {
    Write-Error "Either -Tag or -Branch is required."
    Write-Host "Use -Help for usage information."
    exit 1
}

# Banner
Write-Host ""
Write-Host "╔══════════════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║            razzfazz.ai Multipass VM Launcher                     ║" -ForegroundColor Cyan
Write-Host "╚══════════════════════════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

Test-Prerequisites
Start-MultipassVM
Show-VMInfo

# Show cloud-init log if -Follow was specified
if ($Follow) {
    Write-Host ""
    Write-Host "═══════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
    Write-Host "                    Cloud-Init Deployment Log                       " -ForegroundColor Cyan
    Write-Host "═══════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
    Write-Host ""
    
    & multipass exec $VmName -- cat /var/log/cloud-init-output.log 2>$null
    
    Write-Host ""
    Write-Host "═══════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
    Write-Success "Deployment log complete!"
}

Write-Host ""
Write-Success "Done!"
