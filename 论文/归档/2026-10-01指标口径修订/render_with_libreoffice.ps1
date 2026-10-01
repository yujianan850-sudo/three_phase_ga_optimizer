param(
    [Parameter(Mandatory = $true)][string]$InputPath,
    [Parameter(Mandatory = $true)][string]$OutputDirectory
)
$ErrorActionPreference = 'Stop'
$taskLoDir = 'C:\Program Files\LibreOffice\program'
$taskRuntimeDir = 'C:\Users\30866\.cache\codex-runtimes\codex-primary-runtime\dependencies'
$taskRenderer = 'C:\Users\30866\.codex\plugins\cache\openai-primary-runtime\documents\26.909.12148\skills\documents\render_docx.py'
$taskPreviousPath = $env:Path
try {
    $env:Path = "$taskLoDir;$taskRuntimeDir\native\poppler\Library\bin;$taskPreviousPath"
    & "$taskRuntimeDir\python\python.exe" $taskRenderer $InputPath --output_dir $OutputDirectory --emit_pdf
    if ($LASTEXITCODE -ne 0) { throw "LibreOffice rendering failed: $LASTEXITCODE" }
} finally {
    $env:Path = $taskPreviousPath
}
