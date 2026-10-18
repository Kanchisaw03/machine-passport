param(
    [string]$Project = "project-sukoon",
    [string]$Region = "asia-south1",
    [string]$Service = "machine-passport",
    [string]$GeminiSecret = "machine-passport-gemini-api-key",
    [string]$StorageBucket = "",
    [string]$AuditTopic = ""
)

$ErrorActionPreference = "Stop"
$envVars = @(
    "APP_MODE=cloud",
    "GOOGLE_CLOUD_PROJECT=$Project",
    "GEMINI_QUOTA_BACKEND=firestore",
    "GEMINI_DAILY_CALL_CAP=60",
    "REQUESTS_PER_IP_PER_MINUTE=60",
    "DATA_TTL_DAYS=30"
)
if ($StorageBucket) { $envVars += "GCS_BUCKET=$StorageBucket" }
if ($AuditTopic) { $envVars += "PUBSUB_AUDIT_TOPIC=$AuditTopic" }

$deployArgs = @(
    "--source", ".",
    "--region", $Region,
    "--project", $Project,
    "--allow-unauthenticated",
    "--min", "1",
    "--max-instances", "1",
    "--cpu", "1",
    "--memory", "1Gi",
    "--concurrency", "20",
    "--timeout", "120s",
    "--no-cpu-throttling",
    "--set-env-vars=$($envVars -join ',')"
)

gcloud secrets describe $GeminiSecret --project $Project --quiet *> $null
if ($LASTEXITCODE -eq 0) {
    $deployArgs += "--set-secrets=GEMINI_API_KEY=${GeminiSecret}:latest"
    Write-Output "Gemini key will be read from Secret Manager (value is never displayed)."
} else {
    Write-Output "Secret '$GeminiSecret' was not found; Gemini remains disabled unless configured separately."
}

& gcloud run deploy $Service @deployArgs

if ($LASTEXITCODE -ne 0) {
    throw "Cloud Run deployment failed. Check gcloud auth, project billing, and Cloud Run / Cloud Build permissions."
}

$url = gcloud run services describe $Service `
    --region $Region `
    --project $Project `
    --format="value(status.url)"
if ($LASTEXITCODE -ne 0) { throw "Deployment succeeded but service URL lookup failed." }
Write-Output "Public Cloud Run URL: $url"
