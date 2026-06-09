# issue_monitor auto-start launcher
# Order: llama-server -> playwright collector -> (wait llama health) -> issue_monitor
# Each component runs in its own console window. This launcher only orchestrates start order.

$ErrorActionPreference = 'Continue'

$LlamaExe   = '[LOCAL]\Desktop\llama-cpp\llama-server.exe'
$LlamaModel = '[LOCAL]\Desktop\llama-cpp\Qwen3.5-9B-Q4_K_M.gguf'
$LlamaDir   = '[LOCAL]\Desktop\llama-cpp'
$PwDir      = '[LOCAL]\Desktop\playwright_chat_reader'
$PwPython   = '[LOCAL]\Desktop\playwright_chat_reader\.venv\Scripts\python.exe'
$ImDir      = '[LOCAL]\Desktop\issue_monitor'
$ImPython   = '[LOCAL]\Desktop\issue_monitor\.venv\Scripts\python.exe'
$HealthUrl  = 'http://127.0.0.1:8080/health'

function Test-PortListening([int]$port) {
    return [bool](Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
}

function Test-PythonScriptRunning([string]$scriptName) {
    # True if any python.exe is running with $scriptName in its command line.
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue
    foreach ($p in $procs) {
        if ($p.CommandLine -and ($p.CommandLine -like "*$scriptName*")) { return $true }
    }
    return $false
}

Write-Host "===== issue_monitor start_monitor ====="

# 1. llama-server (skip if already listening on 8080)
if (Test-PortListening 8080) {
    Write-Host "[SKIP] llama-server already listening on :8080"
} else {
    Write-Host "[START] llama-server (ctx-size 32768)"
    Start-Process -FilePath $LlamaExe `
        -ArgumentList '--model', $LlamaModel, '-ngl', '99', '--port', '8080', '--ctx-size', '32768', '--reasoning', 'on' `
        -WorkingDirectory $LlamaDir
}

# 2. playwright collector (skip if login_check.py already running -> avoids chrome profile lock)
if (Test-PythonScriptRunning 'login_check.py') {
    Write-Host "[SKIP] playwright login_check.py already running"
} else {
    Write-Host "[START] playwright_chat_reader (login_check.py)"
    Start-Process -FilePath $PwPython -ArgumentList 'login_check.py' -WorkingDirectory $PwDir
}

# 3. wait for llama model load via /health (max 5 min)
Write-Host "[WAIT] polling llama /health (max 5 min)..."
$ready = $false
for ($i = 0; $i -lt 150; $i++) {
    try {
        $r = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 3 -ErrorAction Stop
        if ($r.status -eq 'ok') { $ready = $true; break }
    } catch { }
    Start-Sleep -Seconds 2
}
if ($ready) {
    Write-Host "[OK] llama /health = ok"
} else {
    Write-Host "[WARN] llama /health not ready after 5 min; starting issue_monitor anyway (recovers next cycle)"
}

# 4. issue_monitor main loop (skip if main.py already running)
if (Test-PythonScriptRunning 'main.py') {
    Write-Host "[SKIP] issue_monitor main.py already running"
} else {
    Write-Host "[START] issue_monitor (main.py)"
    Start-Process -FilePath $ImPython -ArgumentList 'main.py' -WorkingDirectory $ImDir
}

Write-Host "[DONE] start_monitor complete"
