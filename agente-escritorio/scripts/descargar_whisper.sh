#!/usr/bin/env bash
#
# Aprovisiona un modelo de Whisper (convertido a CTranslate2) en models/, para
# que voz/stt.py lo cargue sin tocar la red. Se corre una vez por caja nueva,
# o antes de armar un pendrive para una instalación sin internet.
#
# Sin esto, faster-whisper baja el modelo solo la primera vez que se usa
# (~500 MB para «small», desde HuggingFace) y lo cachea en ~/.cache. Este
# script hace esa misma descarga por adelantado y la deja en models/, que es
# donde voz/stt.py (_origen_modelo) la busca antes de pedirle nada a la red.
#
#   ./scripts/descargar_whisper.sh          # small (el default de device.config)
#   ./scripts/descargar_whisper.sh medium   # otro tamaño
#
set -euo pipefail

TAMANO="${1:-small}"

# La raíz es la carpeta del proyecto, no desde dónde se invocó el script: correrlo
# desde otro directorio tiene que dejar los archivos en el mismo lugar igual.
RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DESTINO="$RAIZ/models/whisper-$TAMANO"

BASE_URL="https://huggingface.co/Systran/faster-whisper-$TAMANO/resolve/main"
MINIMO_BYTES=$((1024 * 1024))  # 1 MB: un modelo real pesa mucho más; un error HTML no.

rojo()  { printf '\033[31m%s\033[0m\n' "$*" >&2; }
verde() { printf '\033[32m%s\033[0m\n' "$*"; }
info()  { printf '%s\n' "$*"; }

# Los cuatro tamaños que device.config.json acepta en voz.modelo_stt (ver
# agent/config.py). Fallar acá con un mensaje claro es mejor que armar una URL
# que capaz ni existe en Systran/faster-whisper-*.
case "$TAMANO" in
  tiny|base|small|medium) ;;
  *)
    rojo "Tamaño de Whisper inválido: «$TAMANO»"
    rojo "Válidos: tiny, base, small, medium (los mismos que acepta device.config.json)"
    exit 1
    ;;
esac

mkdir -p "$DESTINO"

# Portable: `stat` cambia de banderas entre GNU y BSD, `wc -c` no.
tamano_bytes() { wc -c < "$1" | tr -d ' '; }

# Ya está y es válido -> no se vuelve a bajar. Este script se corre en cada caja
# nueva y también cuando alguien no se acuerda si ya lo corrió.
if [[ -f "$DESTINO/model.bin" ]] && (( $(tamano_bytes "$DESTINO/model.bin") >= MINIMO_BYTES )); then
  verde "Whisper «$TAMANO» ya está en $DESTINO. No se baja de nuevo."
  exit 0
fi

# Se baja a un archivo temporal y se mueve al final. Una descarga cortada por
# la mitad no puede quedar con el nombre bueno: la próxima corrida la daría por
# buena y faster-whisper reventaría al cargarla, en medio de una conversación.
descargar() {
  local url="$1" salida="$2" temporal="$2.parcial"
  info "Bajando $(basename "$salida") ..."
  rm -f "$temporal"
  if ! curl --fail --location --retry 3 --retry-delay 2 --progress-bar \
            --output "$temporal" "$url"; then
    rm -f "$temporal"
    return 1
  fi
  mv -f "$temporal" "$salida"
}

# Obligatorios: sin cualquiera de estos cuatro, WhisperModel no carga.
for archivo in config.json preprocessor_config.json tokenizer.json model.bin; do
  if ! descargar "$BASE_URL/$archivo" "$DESTINO/$archivo"; then
    rojo "No se pudo descargar: $BASE_URL/$archivo"
    rojo "Revisa la conexión, o que exista Systran/faster-whisper-$TAMANO en HuggingFace."
    exit 1
  fi
done

# El vocabulario cambia de nombre entre modelos (vocabulary.json en algunos,
# vocabulary.txt en otros): se prueba el primero y, si no está, el segundo. Que
# falten los dos sí es fatal.
if ! descargar "$BASE_URL/vocabulary.json" "$DESTINO/vocabulary.json"; then
  if ! descargar "$BASE_URL/vocabulary.txt" "$DESTINO/vocabulary.txt"; then
    rojo "No se encontró vocabulary.json ni vocabulary.txt en Systran/faster-whisper-$TAMANO."
    exit 1
  fi
fi

# Un model.bin de un par de KB no es un modelo: es una página de error de
# HuggingFace que se bajó con código 200. Se borra todo, porque dejarlo a medias
# hace que la próxima corrida lo dé por válido y el error aparezca recién al
# transcribir la primera frase.
BYTES="$(tamano_bytes "$DESTINO/model.bin")"
if (( BYTES < MINIMO_BYTES )); then
  rm -rf "$DESTINO"
  rojo "model.bin bajó con solo $BYTES bytes (mínimo esperado: $MINIMO_BYTES)."
  rojo "Eso no es un modelo: casi seguro es una página de error HTML."
  rojo "Carpeta borrada. Verifica que el tamaño «$TAMANO» exista y vuelve a intentar."
  exit 1
fi

verde "Whisper «$TAMANO» listo en $DESTINO ($(( BYTES / 1024 / 1024 )) MB el model.bin)"
info  "voz/stt.py lo va a usar directo de ahí, sin tocar HuggingFace."
