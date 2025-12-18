\
# Requires: gh auth login
$manifest = Get-Content -Raw -Path "backlog_manifest.json" | ConvertFrom-Json
$mdFiles = Get-ChildItem -Path "backlog_issues" -Filter "*.md"
foreach ($item in $manifest) {
  $body = ($mdFiles | Where-Object { $_.Name.StartsWith($item.id + "_") } | Select-Object -First 1)
  if (-not $body) { throw "Missing md for $($item.id)" }
  $args = @("issue","create","--title",$item.title,"--body-file",$body.FullName)
  foreach ($lab in $item.labels) { $args += @("--label",$lab) }
  if ($item.milestone) { $args += @("--milestone",$item.milestone) }
  try { gh @args | Out-Null } catch { }
}
Write-Host "Done. Review created issues in GitHub UI."
