# Instala el MIDI Remote Script de AbletonMCP en Ableton Live (macOS / Linux).
#
# Uso:
#   ./install_midi_script.sh
#
# Después: Preferencias -> Link/Tempo/MIDI -> Control Surface = AbletonMCP

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="$SCRIPT_DIR/midi-remote-script/AbletonMCP"

if [ ! -d "$SOURCE" ]; then
  echo "No encuentro $SOURCE" >&2
  exit 1
fi

for TARGET in \
  "$HOME/Music/Ableton/User Library/Remote Scripts" \
  "$HOME/Documents/Ableton/User Library/Remote Scripts"
do
  PARENT="$(dirname "$TARGET")"
  if [ -d "$PARENT" ]; then
    DEST="$TARGET/AbletonMCP"
    mkdir -p "$TARGET"
    [ -d "$DEST" ] && rm -rf "$DEST"
    cp -r "$SOURCE" "$DEST"
    echo "Instalado en:"
    echo "  $DEST"
    echo
    echo "Ahora en Ableton Live:"
    echo "  Preferencias -> Link/Tempo/MIDI -> Control Surface = AbletonMCP"
    exit 0
  fi
done

echo "No encontre la User Library automaticamente." >&2
echo "Copia manualmente la carpeta a:" >&2
echo "  ~/Music/Ableton/User Library/Remote Scripts/AbletonMCP" >&2
exit 1
