# Notion Automator — Windows Service Installer
# Run this script as Administrator after completing setup steps 1-5 in the README.
#
# Edit the two variables below before running:

$ProjectDir = "C:\Users\YOUR_USER\Documents\Notion_Automator"
$NssmPath   = "C:\Tools\nssm.exe"

# --- Do not edit below this line ---

$Python  = "$ProjectDir\venv\Scripts\python.exe"

& $NssmPath install NotionAutomator $Python "daemon.py"
& $NssmPath set NotionAutomator AppDirectory  $ProjectDir
& $NssmPath set NotionAutomator DisplayName   "Notion Automator"
& $NssmPath set NotionAutomator Description   "Runs the Notion Automator daemon. Syncs and automates Notion task databases on a continuous loop."
& $NssmPath set NotionAutomator Start         SERVICE_AUTO_START
& $NssmPath start NotionAutomator

Write-Host ""
Write-Host "Service installed. Verify with: Get-Service NotionAutomator"
