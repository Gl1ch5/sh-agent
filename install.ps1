$ErrorActionPreference = "Stop"

$RepoUrl = "https://github.com/Gl1ch5/sh-agent.git"

function Write-Step([int]$Pct, [string]$Msg) {
    $Filled = [math]::Floor($Pct / 5)
    $Empty = 20 - $Filled
    $Bar = ("#" * $Filled) + ("-" * $Empty)
    Write-Host "[$Bar] $Pct% " -ForegroundColor Green -NoNewline
    Write-Host $Msg
}

Write-Host ""
Write-Host "  >_ sh-agent (shx)" -ForegroundColor Green -NoNewline
Write-Host " -- bash-only ultra-economical AI agent" -ForegroundColor DarkGray
Write-Host "  -----------------------------------------------------" -ForegroundColor DarkGray

Write-Step 15 "Detected Windows ($env:PROCESSOR_ARCHITECTURE)"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Step 35 "Installing uv package manager..."
    irm https://astral.sh/uv/install.ps1 | iex | Out-Null
    $env:Path = "$env:USERPROFILE\.local\bin;$env:USERPROFILE\.cargo\bin;$env:Path"
}

if (Get-Command uv -ErrorAction SilentlyContinue) {
    Write-Step 65 "Installing sh-agent via uv..."
    uv tool install --force "git+$RepoUrl"
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    Write-Step 65 "Installing sh-agent via pip..."
    python -m pip install --user --upgrade "git+$RepoUrl"
} else {
    Write-Host "x Neither uv nor python found. Install Python 3.9+ or uv first." -ForegroundColor Red
    exit 1
}

Write-Step 100 "Installation complete!"
Write-Host ""
Write-Host "[OK] shx is ready." -ForegroundColor Green
Write-Host "Run " -NoNewline
Write-Host "shx" -ForegroundColor Green -NoNewline
Write-Host " to connect your custom provider and start."
Write-Host ""
