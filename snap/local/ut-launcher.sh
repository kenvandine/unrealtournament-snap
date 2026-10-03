#!/bin/bash
# Launcher for the Unreal Tournament (UE4 pre-alpha) Linux client. Epic's
# client data is never shipped in the snap; on first launch ut-installer
# downloads it (or unpacks a zip the user already has) into $SNAP_USER_COMMON.
#
# Usage: unrealtournament [game args...]
#        unrealtournament.setup        (re)install the client data
set -euo pipefail

INSTALL_DIR="$SNAP_USER_COMMON/game"
STAMP="$INSTALL_DIR/.installed"

# Staged libraries: libmanette for the installer's gamepad support; NSS,
# gconf, etc. for the client's CEF; OpenAL backends. Appended so the gnome
# runtime and gpu-2404 paths keep precedence (GTK comes from the runtime).
export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:+$LD_LIBRARY_PATH:}$SNAP/usr/lib/x86_64-linux-gnu"
export GI_TYPELIB_PATH="${GI_TYPELIB_PATH:+$GI_TYPELIB_PATH:}$SNAP/usr/lib/x86_64-linux-gnu/girepository-1.0"

# --setup reinstalls. The old data is only replaced once the new copy has
# unpacked successfully, so cancelling leaves a working install in place.
if [ "${1:-}" = "--setup" ]; then
  shift
  "$SNAP/bin/ut-installer" || exit 1
elif [ ! -f "$STAMP" ]; then
  "$SNAP/bin/ut-installer" || exit 1
fi

GAME_ROOT="$INSTALL_DIR/$(cat "$STAMP")"
BIN="$GAME_ROOT/Engine/Binaries/Linux/UE4-Linux-Shipping"
if [ ! -x "$BIN" ]; then
  echo "The installed client looks broken. Run unrealtournament.setup to reinstall it." >&2
  exit 1
fi

# UE 4.15's bundled SDL2 predates its Wayland backend; run under XWayland.
export SDL_VIDEODRIVER="${SDL_VIDEODRIVER:-x11}"
export SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS=1

exec "$BIN" UnrealTournament "$@"
