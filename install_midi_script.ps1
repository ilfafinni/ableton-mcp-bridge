# Instala el MIDI Remote Script de AbletonMCP en Ableton Live (Windows).
#
# Uso:
#   .\install_midi_script.ps1
#
# Copia midi-remote-script\AbletonMCP a la User Library de Live.
# Después: Preferencias -> Link/Tempo/MIDI -> Control Surface = AbletonMCP

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$source = Join-Path $scriptDir "midi-remote-script\AbletonMCP"

if (-not (Test-Path $source)) {
    Write-Error "No encuentro $source"
    exit 1
}

# Localiza la User Library de Live
$candidates = @(
    (Join-Path $env:USERPROFILE "Documents\Ableton\User Library\Remote Scripts"),
    (Join-Path $env:USERPROFILE "OneDrive\Documents\Ableton\User Library\Remote Scripts"),
    (Join-Path $env:USERPROFILE "Music\Ableton\User Library\Remote Scripts")
)

$target = $candidates | Where-Object { Test-Path (Split-Path $_ -Parent) } | Select-Object -First 1

if (-not $target) {
    Write-Host "No encontre la User Library automaticamente." -ForegroundColor Yellow
    Write-Host "Candidatos probados:"
    $candidates | ForEach-Object { Write-Host "  $_" }
    $manual = Read-Host "Pega la ruta a 'Remote Scripts' de tu User Library"
    $target = $manual
}

if (-not (Test-Path $target)) {
    New-Item -ItemType Directory -Force -Path $target | Out-Null
}

$dest = Join-Path $target "AbletonMCP"
if (Test-Path $dest) {
    Write-Host "Ya existe $dest — lo reemplazo." -ForegroundColor Yellow
    Remove-Item -Recurse -Force $dest
}

Copy-Item -Recurse $source $dest
Write-Host ""
Write-Host "Instalado en:" -ForegroundColor Green
Write-Host "  $dest"
Write-Host ""
Write-Host "Ahora en Ableton Live:"
Write-Host "  Preferencias -> Link/Tempo/MIDI -> Control Surface = AbletonMCP"
Write-Host "  (debe indicar que escucha en el puerto 9878)"
