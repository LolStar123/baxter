# Baxter desktop notification (system tray balloon -> Win11 routes to notification center).
# No modules needed. Called fire-and-forget by baxter_triage.py.
param([string]$Title = "Baxter", [string]$Message = "")
$ErrorActionPreference = "SilentlyContinue"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$ni = New-Object System.Windows.Forms.NotifyIcon
$ni.Icon = [System.Drawing.SystemIcons]::Information
$ni.Visible = $true
$ni.ShowBalloonTip(6000, $Title, $Message, [System.Windows.Forms.ToolTipIcon]::Info)
Start-Sleep -Seconds 6
$ni.Dispose()
