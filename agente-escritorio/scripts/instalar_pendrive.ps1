<#
.SYNOPSIS
    Instala SecretarIA en un PC Windows sin internet, desde una copia del
    proyecto armada de antemano (típicamente en un pendrive).

.DESCRIPTION
    Este script asume que ya se resolvieron, en otro lado, las dos cosas que
    necesitan internet real:

      1. Python 3.11+ instalado en ESTE PC (con «Add python.exe to PATH»
         marcado en el instalador de python.org). Este script no lo instala:
         si no lo tenés, bajalo una vez con internet antes de seguir.
      2. La carpeta del proyecto trae, al lado de este script:
           wheels\              — las .whl de requirements.txt para
                                   win_amd64 / cp311 (armadas con
                                   `pip download --platform win_amd64
                                   --python-version 311 --only-binary=:all:`)
           models\<voz>.onnx(.json)   — bajado con scripts/descargar_voz.sh
           models\whisper-<tamano>\  — bajado con scripts/descargar_whisper.sh

    Con eso, todo lo que sigue corre sin tocar la red: crear el venv, instalar
    las dependencias (`pip install --no-index`, nunca sale a PyPI) y armar
    device.config.json / .env desde las plantillas. Es la misma garantía que
    `voz/stt.py` ya tiene en código (`_origen_modelo`): si el modelo está en
    disco, no se pide por red.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\instalar_pendrive.ps1
#>

$ErrorActionPreference = "Stop"

# La raíz es la carpeta del proyecto (un nivel arriba de scripts\), no desde
# dónde se invocó: correrlo desde otro directorio tiene que funcionar igual.
$Raiz = Split-Path -Parent $PSScriptRoot
Set-Location $Raiz

function Info($msg)  { Write-Host $msg }
function Ok($msg)    { Write-Host $msg -ForegroundColor Green }
function Mal($msg)   { Write-Host $msg -ForegroundColor Red }

Info "SecretarIA — instalación sin internet"
Info "Raíz del proyecto: $Raiz"
Info ""

# -- 1. Python ---------------------------------------------------------------
# `py -3.11` es el lanzador oficial de Windows y encuentra la versión aunque
# haya varias instaladas; si no está, se cae a `python` del PATH. Cualquiera
# de los dos sirve: el proyecto corre con 3.11 o 3.12 (ver CLAUDE.md). Se
# guardan exe y argumentos por separado (no un string a partir) para no
# depender de cómo PowerShell trocea texto con espacios al invocar.
$Candidatos = @(
    @{ Exe = "py"; Args = @("-3.11") },
    @{ Exe = "py"; Args = @("-3.12") },
    @{ Exe = "py"; Args = @("-3") },
    @{ Exe = "python"; Args = @() },
    @{ Exe = "python3"; Args = @() }
)

$PythonExe = $null
$PythonArgs = @()
foreach ($c in $Candidatos) {
    if (Get-Command $c.Exe -ErrorAction SilentlyContinue) {
        try {
            $version = & $c.Exe @($c.Args) --version 2>&1
            if ($version -match "Python 3\.(1[1-9]|[2-9]\d)") {
                $PythonExe = $c.Exe
                $PythonArgs = $c.Args
                break
            }
        } catch {}
    }
}
if (-not $PythonExe) {
    Mal "No encontré Python 3.11+ instalado en este PC."
    Mal "Este script no lo instala: bajalo una vez de python.org (con internet)"
    Mal "y marcá «Add python.exe to PATH» al instalar. Después volvé a correr esto."
    exit 1
}
Ok "Python: $PythonExe $PythonArgs"

# -- 2. El entorno virtual, instalado 100% desde wheels\ local ---------------
$WheelsDir = Join-Path $Raiz "wheels"
if (-not (Test-Path $WheelsDir)) {
    Mal "Falta la carpeta wheels\ al lado de este script."
    Mal "Sin eso, pip tendría que salir a internet — que es justo lo que no hay."
    exit 1
}

$VenvPython = Join-Path $Raiz ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Info "Creando entorno virtual (.venv)..."
    & $PythonExe @($PythonArgs) -m venv .venv
} else {
    Info ".venv ya existe, no se vuelve a crear."
}

Info "Instalando dependencias desde wheels\ (sin salir a internet)..."
& $VenvPython -m pip install --no-index --find-links "$WheelsDir" -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Mal "pip install falló. Revisá que wheels\ tenga TODAS las .whl que pide"
    Mal "requirements.txt (incluye las dependencias transitivas, no solo las"
    Mal "10 de arriba) — se arman con:"
    Mal "  pip download --dest wheels --platform win_amd64 --python-version 311 \"
    Mal "    --implementation cp --abi cp311 --only-binary=:all: -r requirements.txt"
    exit 1
}
Ok "Dependencias instaladas."

# -- 3. Los modelos: si no están, no hay wheel que los reemplace -------------
$vozOk = (Get-ChildItem -Path "models" -Filter "*.onnx" -ErrorAction SilentlyContinue).Count -gt 0
$whisperOk = (Get-ChildItem -Path "models" -Filter "whisper-*" -Directory -ErrorAction SilentlyContinue).Count -gt 0

if (-not $vozOk) {
    Mal "No hay ningún .onnx en models\. Falta la voz de Piper."
    Mal "Se arma (con internet, en OTRO PC) con: ./scripts/descargar_voz.sh"
}
if (-not $whisperOk) {
    Mal "No hay ninguna carpeta whisper-* en models\. Falta el modelo de Whisper."
    Mal "Se arma (con internet, en OTRO PC) con: ./scripts/descargar_whisper.sh"
}
if ((-not $vozOk) -or (-not $whisperOk)) {
    Mal "El agente por texto (main.py) funciona igual sin esto. La voz (main_voz.py), no."
} else {
    Ok "Modelos de voz encontrados en models\."
}

# -- 4. Config y .env: se crean solo si no existen ----------------------------
# Nunca se pisa un device.config.json o un .env que la persona ya haya tocado.
if (-not (Test-Path "device.config.json")) {
    Copy-Item "device.config.example.json" "device.config.json"
    # El ejemplo trae la voz apagada (`"habilitada": false`) porque `main.py`
    # es el REPL de texto por defecto. Acá el objetivo es el prototipo hablado,
    # así que se prende — es el único campo que este instalador cambia.
    (Get-Content "device.config.json") -replace '"habilitada":\s*false', '"habilitada": true' |
        Set-Content "device.config.json"
    Ok "device.config.json creado desde la plantilla, con la voz encendida."
    Info "  Editalo para poner el nombre real del ejecutivo y del agente."
} else {
    Info "device.config.json ya existe, no se toca."
}

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    (Get-Content ".env") `
        -replace '^LLM_PROVIDER=.*', 'LLM_PROVIDER=anthropic' |
        Set-Content ".env"
    Add-Content ".env" ""
    Add-Content ".env" "# Prototipo: sin datos reales de Google todavia."
    Add-Content ".env" "FUENTE_DATOS=mock"
    Ok ".env creado desde la plantilla (LLM_PROVIDER=anthropic, FUENTE_DATOS=mock)."
    Mal "  Falta ANTHROPIC_API_KEY: abrí .env y completala a mano."
} else {
    Info ".env ya existe, no se toca."
}

Info ""
Ok "Instalación lista. Próximos pasos:"
Info "  1) Completá ANTHROPIC_API_KEY en .env si no lo hiciste."
Info "  2) .venv\Scripts\python.exe scripts\verificar_audio.py --listar"
Info "     (anotá los índices y ponelos en voz.dispositivo_entrada/_salida"
Info "      dentro de device.config.json)"
Info "  3) .venv\Scripts\python.exe main_voz.py --probar-voz"
Info "  4) .venv\Scripts\python.exe main_voz.py"
