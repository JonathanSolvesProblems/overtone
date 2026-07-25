# Reads .env and pushes the values to Fly as app secrets.
# Nothing here is committed; .env is gitignored. Run after `fly apps create`.

$ErrorActionPreference = "Stop"

if (-not (Test-Path ".env")) {
    Write-Error "No .env file found. Copy .env.example to .env and fill it in first."
    exit 1
}

# Only these keys are pushed to the server. B2_APPLICATION_KEY is also sent as
# B2_APP_KEY because that's the name genblaze-s3's for_backblaze() reads.
$wanted = @(
    "B2_KEY_ID", "B2_APPLICATION_KEY", "B2_BUCKET", "B2_ENDPOINT", "B2_REGION",
    "ASSEMBLYAI_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY",
    "ELEVENLABS_API_KEY", "HUME_API_KEY", "GMI_CLOUD_API_KEY"
)

$pairs = @()
foreach ($line in Get-Content ".env") {
    $t = $line.Trim()
    if ($t -eq "" -or $t.StartsWith("#") -or ($t -notmatch "=")) { continue }
    $k, $v = $t -split "=", 2
    $k = $k.Trim(); $v = $v.Trim().Trim('"').Trim("'")
    if ($v -and ($wanted -contains $k)) {
        $pairs += "$k=$v"
        if ($k -eq "B2_APPLICATION_KEY") { $pairs += "B2_APP_KEY=$v" }
    }
}

if ($pairs.Count -eq 0) {
    Write-Error "No recognized keys found in .env."
    exit 1
}

Write-Host "Setting $($pairs.Count) secrets on the Fly app..."
# Set them all in one call so the app restarts once.
fly secrets set @pairs
Write-Host "Done. Run 'fly deploy' next."
