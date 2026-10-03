# Starts everything the payroll chatbot needs and opens the chat in the browser.
#   1. Ollama (local AI model)      - if LLM_PROVIDER=ollama and it is not running
#   2. API      http://127.0.0.1:8000 - in its own window (keep it open)
#   3. Chat UI  http://127.0.0.1:5173 - in its own window (keep it open)
# Run: right-click > "Run with PowerShell", or double-click "Start Payroll Chatbot.bat".

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

function Test-Url($url) {
    try { Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 2 | Out-Null; return $true } catch { return $false }
}

function Wait-Url($url, $seconds, $what) {
    for ($i = 0; $i -lt $seconds; $i++) {
        if (Test-Url $url) { return $true }
        Start-Sleep -Seconds 1
    }
    Write-Host "  $what did not start in $seconds seconds - check its window for errors." -ForegroundColor Red
    return $false
}

Write-Host "Payroll Chatbot" -ForegroundColor Green

if (-not (Test-Path "$root\.venv\Scripts\payroll-chatbot.exe")) {
    Write-Host "Python environment missing. Run 'uv sync' in $root first." -ForegroundColor Red
    exit 1
}

# 1. Ollama -------------------------------------------------------------------
$provider = (Get-Content "$root\.env" -ErrorAction SilentlyContinue |
    Where-Object { $_ -match '^\s*LLM_PROVIDER\s*=' } | Select-Object -First 1) -replace '^\s*LLM_PROVIDER\s*=\s*([a-z]+).*', '$1'
if (-not $provider) { $provider = "ollama" }
if ($provider -eq "ollama") {
    if (Test-Url "http://127.0.0.1:11434/api/version") {
        Write-Host "[1/3] Ollama already running"
    } else {
        Write-Host "[1/3] Starting Ollama..."
        $ollama = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
        if (Test-Path $ollama) {
            Start-Process $ollama -ArgumentList "serve" -WindowStyle Minimized
            [void](Wait-Url "http://127.0.0.1:11434/api/version" 30 "Ollama")
        } else {
            Write-Host "  Ollama is not installed (https://ollama.com). The chat will say the AI is unavailable." -ForegroundColor Yellow
        }
    }
} else {
    Write-Host "[1/3] LLM_PROVIDER=$provider - Ollama not needed"
}

# 2. API ----------------------------------------------------------------------
if (Test-Url "http://127.0.0.1:8000/health") {
    Write-Host "[2/3] API already running"
} else {
    Write-Host "[2/3] Starting API window..."
    Start-Process powershell -ArgumentList "-NoExit", "-Command",
        "`$host.UI.RawUI.WindowTitle = 'Payroll API (keep open)'; Set-Location '$root'; .\.venv\Scripts\payroll-chatbot.exe api serve"
    if (-not (Wait-Url "http://127.0.0.1:8000/health" 60 "API")) { exit 1 }
}

# 3. Chat UI ------------------------------------------------------------------
if (Test-Url "http://127.0.0.1:5173") {
    Write-Host "[3/3] Chat UI already running"
} else {
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        Write-Host "Node.js is not installed (https://nodejs.org) - needed for the chat screen." -ForegroundColor Red
        exit 1
    }
    if (-not (Test-Path "$root\frontend\node_modules")) {
        Write-Host "  First run: installing the chat screen (1-2 minutes)..."
        Push-Location "$root\frontend"; npm install --no-fund --no-audit; Pop-Location
    }
    Write-Host "[3/3] Starting chat UI window..."
    Start-Process powershell -ArgumentList "-NoExit", "-Command",
        "`$host.UI.RawUI.WindowTitle = 'Payroll Chat UI (keep open)'; Set-Location '$root\frontend'; npm run dev"
    if (-not (Wait-Url "http://127.0.0.1:5173" 60 "Chat UI")) { exit 1 }
}

Start-Process "http://127.0.0.1:5173"
Write-Host ""
Write-Host "Opened http://127.0.0.1:5173 - keep the 'Payroll API' and 'Payroll Chat UI' windows open." -ForegroundColor Green
Write-Host "Demo login: rahul.verma@example.com / Employee@123   (HR: anjali.mehta@example.com / Hr@12345)"
