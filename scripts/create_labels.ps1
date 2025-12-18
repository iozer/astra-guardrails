\
# Requires: gh auth login
$labels = Get-Content -Raw -Path "labels.json" | ConvertFrom-Json
foreach ($l in $labels) {
  try { gh label create $l.name --color $l.color --description $l.description | Out-Null } catch {}
}
Write-Host "Labels created (existing labels were left as-is)."
