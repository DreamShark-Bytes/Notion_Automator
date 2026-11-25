# Notion Automator — Windows Service Installer
# Run this script as Administrator after completing setup steps 1-5 in the README.
#
# Create config_secrets.toml in $ProjectDir first (copy config_secrets_example.toml and fill in the token).
#
# Edit the two variables below before running:

$ProjectDir = "C:\Users\YOUR_USER\Documents\Notion_Automator"
$NssmPath   = "C:\Tools\nssm.exe"

# --- Do not edit below this line ---

$Python  = "$ProjectDir\venv\Scripts\python.exe"

& $NssmPath install NotionAutomator $Python "daemon.py"
& $NssmPath set NotionAutomator AppDirectory           $ProjectDir
& $NssmPath set NotionAutomator DisplayName            "Notion Automator"
& $NssmPath set NotionAutomator Description            "Runs the Notion Automator daemon. Syncs and automates Notion task databases on a continuous loop."
& $NssmPath set NotionAutomator Start                  SERVICE_AUTO_START
# Give the daemon up to 8 seconds to exit cleanly before NSSM force-kills it.
# Without this, Stop-Service returns before the Python process exits, and a
# rapid Start-Service can launch a second instance alongside the old one.
& $NssmPath set NotionAutomator AppStopMethodConsole   8000
& $NssmPath set NotionAutomator AppStopMethodWindow    8000
& $NssmPath set NotionAutomator AppStopMethodThreads   8000

# Lock config_secrets.toml to the service account (LocalSystem), Administrators and the current user.
$Secrets = "$ProjectDir\config_secrets.toml"
if (Test-Path $Secrets) {
    icacls $Secrets /inheritance:r /grant:r "SYSTEM:(R)" "Administrators:(F)" "${env:USERNAME}:(F)" | Out-Null
    Write-Host "Locked $Secrets to SYSTEM, Administrators and $env:USERNAME."
} else {
    Write-Warning "$Secrets not found. Create it from config_secrets_example.toml, then re-run: icacls `"$Secrets`" /inheritance:r /grant:r `"SYSTEM:(R)`" `"Administrators:(F)`" `"${env:USERNAME}:(F)`""
}
& $NssmPath start NotionAutomator

Write-Host ""
Write-Host "Service installed. Verify with: Get-Service NotionAutomator"
