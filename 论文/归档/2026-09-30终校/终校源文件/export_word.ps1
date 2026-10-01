param([string]$InputDoc,[string]$OutputPdf)
$ErrorActionPreference = 'Stop'
$wordTask = New-Object -ComObject Word.Application
$wordTask.Visible = $false
$wordTask.DisplayAlerts = 0
try {
  $docTask = $wordTask.Documents.Open($InputDoc, $false, $true, $false)
  $docTask.Repaginate()
  $docTask.ExportAsFixedFormat($OutputPdf, 17)
  Write-Output ('Pages: ' + $docTask.ComputeStatistics(2))
  $docTask.Close(0)
} finally {
  try { $wordTask.Quit(0) } catch { Write-Output 'Word export completed; application already closed.' }
  [System.Runtime.InteropServices.Marshal]::ReleaseComObject($wordTask) | Out-Null
}
