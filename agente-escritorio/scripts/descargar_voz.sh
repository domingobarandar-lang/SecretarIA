#!/usr/bin/env bash
#
# Aprovisiona una voz de Piper en models/. Se corre una vez por caja nueva.
#
# Los modelos NO se versionan: pesan sobre 60 MB y hay uno por idioma. Lo que
# se versiona es este script; el binario se baja acá.
#
# NO se clona el repositorio piper-voices: trae 35 idiomas y pesa varios gigas.
# Se bajan los dos archivos de UNA voz.
#
#   ./scripts/descargar_voz.sh                      # es_MX-claude-high
#   ./scripts/descargar_voz.sh es_ES-davefx-medium  # otra voz
#
set -euo pipefail

VOZ="${1:-es_MX-claude-high}"

# La raíz es la carpeta del proyecto, no desde dónde se invocó el script: correrlo
# desde otro directorio tiene que dejar los archivos en el mismo lugar igual.
RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DESTINO="$RAIZ/models"

BASE_URL="https://huggingface.co/rhasspy/piper-voices/resolve/main"
MINIMO_BYTES=$((10 * 1024 * 1024))  # 10 MB

rojo()  { printf '\033[31m%s\033[0m\n' "$*" >&2; }
verde() { printf '\033[32m%s\033[0m\n' "$*"; }
info()  { printf '%s\n' "$*"; }

# El nombre de una voz de Piper es <locale>-<hablante>-<calidad> y la ruta en el
# repositorio se deriva de ahí: es_MX-claude-high -> es/es_MX/claude/high/.
# Si el nombre no calza con ese patrón, la URL saldría mal armada y la descarga
# traería un 404 en HTML: mejor fallar acá, con el motivo claro.
if [[ ! "$VOZ" =~ ^([a-z]{2,3}_[A-Z]{2})-([A-Za-z0-9_]+)-(x_low|low|medium|high)$ ]]; then
  rojo "Nombre de voz inválido: «$VOZ»"
  rojo "Formato esperado: <locale>-<hablante>-<calidad>, por ejemplo es_MX-claude-high"
  rojo "Calidades válidas: x_low, low, medium, high"
  exit 1
fi
LOCALE="${BASH_REMATCH[1]}"
HABLANTE="${BASH_REMATCH[2]}"
CALIDAD="${BASH_REMATCH[3]}"
IDIOMA="${LOCALE%%_*}"

RUTA_REMOTA="$IDIOMA/$LOCALE/$HABLANTE/$CALIDAD/$VOZ"
ONNX="$DESTINO/$VOZ.onnx"
CONFIG="$DESTINO/$VOZ.onnx.json"

mkdir -p "$DESTINO"

# Portable: `stat` cambia de banderas entre GNU y BSD, `wc -c` no.
tamano() { wc -c < "$1" | tr -d ' '; }

# Ya está y es válido -> no se vuelve a bajar. Idempotencia: este script se corre
# en cada caja nueva y también cuando alguien no se acuerda si ya lo corrió.
if [[ -f "$ONNX" && -f "$CONFIG" ]] && (( $(tamano "$ONNX") >= MINIMO_BYTES )); then
  verde "La voz «$VOZ» ya está en models/ ($(( $(tamano "$ONNX") / 1024 / 1024 )) MB). No se baja de nuevo."
  exit 0
fi

# Se baja a un archivo temporal y se mueve al final. Una descarga cortada por
# la mitad no puede quedar con el nombre bueno: la próxima corrida la daría por
# buena y Piper reventaría al cargarla.
descargar() {
  local url="$1" salida="$2" temporal="$2.parcial"
  info "Bajando $(basename "$salida") ..."
  rm -f "$temporal"
  if ! curl --fail --location --retry 3 --retry-delay 2 --progress-bar \
            --output "$temporal" "$url"; then
    rm -f "$temporal"
    rojo "No se pudo descargar: $url"
    rojo "Revisa la conexión, o que la voz «$VOZ» exista en el repositorio."
    exit 1
  fi
  mv -f "$temporal" "$salida"
}

descargar "$BASE_URL/$RUTA_REMOTA.onnx"      "$ONNX"
descargar "$BASE_URL/$RUTA_REMOTA.onnx.json" "$CONFIG"

# Un .onnx de 3 KB no es un modelo: es una página de error de HuggingFace que
# se bajó con código 200. Se borra, porque dejarlo en disco hace que la próxima
# corrida lo dé por válido y el error aparezca recién al hablar.
BYTES="$(tamano "$ONNX")"
if (( BYTES < MINIMO_BYTES )); then
  rm -f "$ONNX"
  rojo "El modelo bajó con solo $BYTES bytes (mínimo esperado: $MINIMO_BYTES)."
  rojo "Eso no es un modelo: casi seguro es una página de error HTML."
  rojo "Archivo borrado. Verifica que la voz «$VOZ» exista y vuelve a intentar."
  exit 1
fi

# Sin el .onnx.json Piper no arranca: ahí vienen el sample rate, el idioma y el
# mapa de fonemas. No se declara en device.config.json porque Piper lo busca
# solo, al lado del .onnx y con el mismo nombre.
if [[ ! -f "$CONFIG" ]]; then
  rojo "Falta $VOZ.onnx.json. Piper no arranca sin él."
  exit 1
fi

verde "Voz «$VOZ» lista en models/ ($(( BYTES / 1024 / 1024 )) MB)"
info  "  modelo: $ONNX"
info  "  config: $CONFIG"
