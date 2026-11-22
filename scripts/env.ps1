# Dot-source in a new PowerShell before using uv or pdftr:   . .\scripts\env.ps1
# Keeps every download, model and temp file on drive D (drive C is nearly full).
$root = Split-Path -Parent $PSScriptRoot
$env:UV_PYTHON_INSTALL_DIR = "$root\.cache\python"
$env:UV_CACHE_DIR = "$root\.cache\uv"
$env:HF_HOME = "$root\.cache\huggingface"
$env:TORCH_HOME = "$root\.cache\torch"
$env:TMP = "$root\.cache\tmp"
$env:TEMP = "$root\.cache\tmp"
$env:PYTHONUTF8 = "1"
Write-Host "pdftr environment set (caches under $root\.cache)"
