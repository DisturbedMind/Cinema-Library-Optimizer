param(
    [switch]$OneFile
)

Set-Location -LiteralPath $PSScriptRoot

if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) {
    Write-Host "PyInstaller is not installed. Install it with:"
    Write-Host "  python -m pip install pyinstaller"
    exit 1
}

$mode = if ($OneFile) { "--onefile" } else { "--onedir" }
pyinstaller $mode --windowed --name "Cinema Library Optimizer" --add-data "assets;assets" .\cinema_library_optimizer.py
