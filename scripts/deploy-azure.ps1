<#
.SYNOPSIS
  Deploy Deepbox to an Azure App Service (Linux, B1, Python 3.12).

.DESCRIPTION
  Creates the resource group (if needed), deploys infra/main.bicep, then zip
  deploys the source so Oryx installs the root requirements.txt.

  DEEPBOX_SECRET is generated here at deploy time (or you may pass -DeepboxSecret
  / source it from Key Vault). It is never written to Git.

  This script does NOT run automatically in this task — it is provided for the
  operator. Run it manually when you are ready to create Azure resources.

.EXAMPLE
  ./scripts/deploy-azure.ps1 -WebAppName my-deepbox-42 -ResourceGroup deepbox-rg -Location eastus
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$WebAppName,
    [Parameter(Mandatory = $true)][string]$ResourceGroup,
    [string]$Location = 'eastus',
    [securestring]$DeepboxSecret,
    [switch]$RegistrationEnabled
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$gitCommit = (& git -C $repoRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $gitCommit -notmatch '^[0-9a-fA-F]{40}$') {
    throw 'Could not resolve the source Git commit.'
}

# Generate a strong session secret if one was not supplied.
if (-not $DeepboxSecret) {
    $bytes = New-Object 'System.Byte[]' 48
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $plain = [System.Convert]::ToBase64String($bytes)
    $DeepboxSecret = ConvertTo-SecureString $plain -AsPlainText -Force
    Write-Host 'Generated a new DEEPBOX_SECRET (stored only in Azure app settings).'
}

Write-Host "Ensuring resource group '$ResourceGroup' in '$Location'..."
az group create --name $ResourceGroup --location $Location | Out-Null

$secretPlain = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($DeepboxSecret))

Write-Host 'Deploying Bicep template...'
az deployment group create `
    --resource-group $ResourceGroup `
    --template-file (Join-Path $repoRoot 'infra/main.bicep') `
    --parameters (Join-Path $repoRoot 'infra/main.parameters.json') `
    --parameters webAppName=$WebAppName `
        deepboxSecret=$secretPlain `
        registrationEnabled=$($RegistrationEnabled.IsPresent) `
        allowedOrigins="https://$WebAppName.azurewebsites.net" `
        publicUrl="https://$WebAppName.azurewebsites.net" `
        gitCommit=$gitCommit | Out-Null

# Build a deployment zip (server, web UI, startup script, and server requirements).
$zipPath = Join-Path $env:TEMP "deepbox-deploy-$([guid]::NewGuid()).zip"
Write-Host "Packaging source to $zipPath..."
$include = @('server', 'web', 'requirements.txt', 'azure-startup.sh')
Push-Location $repoRoot
try {
    # bsdtar writes ZIP entry names with POSIX separators. Compress-Archive on
    # Windows writes backslashes, which Linux treats as literal filename text.
    & tar.exe -a -c -f $zipPath --exclude=*/__pycache__ --exclude=*.pyc @include
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create deployment zip.' }
}
finally {
    Pop-Location
}

Write-Host 'Zip deploying (Oryx will install requirements.txt)...'
az webapp deploy `
    --resource-group $ResourceGroup `
    --name $WebAppName `
    --src-path $zipPath `
    --type zip | Out-Null

Remove-Item $zipPath -Force
Write-Host "Done. App: https://$WebAppName.azurewebsites.net  (health: /api/ready)"
