# Run the API on this computer (no VM). Usage, from the folder that contains app\ :
#   powershell -ExecutionPolicy Bypass -File .\run_local.ps1
# Then, in a SECOND window:  cloudflared tunnel --url http://localhost:8000
# Copy the https://....trycloudflare.com address into Base44 as KIDNEY_API_URL.

param([switch]$Local, [int]$Port = 8000)   # -Local: this computer only; Port lets multiple local instances coexist

if (-not (Test-Path ".env")) { Write-Error "No .env file here. Copy .env.example to .env and fill it in."; exit 1 }

# Load .env into this window's environment (ignores comments and blank lines)
Get-Content ".env" | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
        $parts = $line.Split("=", 2)
        $key = $parts[0].Trim()
        $val = ($parts[1] -split "\s+#")[0].Trim().Trim('"')
        [Environment]::SetEnvironmentVariable($key, $val, "Process")
    }
}

if ($Local) {
    $env:APP_SHARED_SECRET = ""   # the local screens don't send it; the API only listens on 127.0.0.1
} elseif (-not $env:APP_SHARED_SECRET) {
    Write-Error "APP_SHARED_SECRET is empty in .env. Without it, anyone with the tunnel address could use the API."
    exit 1
}
if (-not (Test-Path "data\reference.duckdb")) {
    Write-Warning "data\reference.duckdb not found. Run the ingest steps first, or the chat tabs will have no data."
}

Write-Host "Starting API on http://localhost:$Port (only reachable from this computer and through the tunnel)"
python -m uvicorn app.api:app --host 127.0.0.1 --port $Port
