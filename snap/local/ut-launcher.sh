#!/bin/bash
# First-run installer and launcher for the Unreal Tournament (UE4 pre-alpha)
# Linux client. Epic's client data is never shipped in the snap; it is
# downloaded from Epic's original CDN (or unpacked from a zip the user
# already has) into $SNAP_USER_COMMON on first launch.
#
# Usage: unrealtournament [game args...]
#        unrealtournament.setup        (re)install the client data
set -euo pipefail

TITLE="Unreal Tournament Setup"

# Last Linux client Epic shipped (Release-Next, CL 3525360, 2017-07).
DEFAULT_URL="https://s3.amazonaws.com/unrealtournament/ShippedBuilds/%2B%2BUT%2BRelease-Next-CL-3525360/UnrealTournament-Client-XAN-3525360-Linux.zip"

# $SNAP_USER_COMMON isn't versioned, so the ~23 GB of game data is not copied
# on every snap refresh the way $SNAP_USER_DATA would be.
INSTALL_DIR="$SNAP_USER_COMMON/game"
STAMP="$INSTALL_DIR/.installed"
DOWNLOAD="$SNAP_USER_COMMON/ut-client.zip"
# Extracted data plus headroom for config, shader cache and logs.
SPARE_BYTES=$((2 * 1024 * 1024 * 1024))

# Staged libraries (NSS, gconf, etc. for CEF; OpenAL backends). Appended so
# the gnome extension and gpu-2404 paths keep precedence.
export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:+$LD_LIBRARY_PATH:}$SNAP/usr/lib/x86_64-linux-gnu"

have_gui() {
  [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v zenity > /dev/null
}

info() {
  if have_gui; then
    zenity --info --title="$TITLE" --width=480 --text="$1" || true
  else
    echo -e "$1"
  fi
}

die() {
  if have_gui; then
    zenity --error --title="$TITLE" --width=480 --text="$1" || true
  else
    echo -e "error: $1" >&2
  fi
  exit 1
}

human() {
  numfmt --to=iec --suffix=B "$1"
}

free_bytes() {
  df --output=avail -B1 "$SNAP_USER_COMMON" | tail -n1 | tr -d ' '
}

download_url() {
  local url
  url="$(snapctl get download-url 2> /dev/null || true)"
  echo "${url:-$DEFAULT_URL}"
}

remote_size() {
  curl -sSIL "$1" | tr -d '\r' | awk 'tolower($1) == "content-length:" { n = $2 } END { print n + 0 }'
}

# Draw a progress dialog (or terminal progress) from "percent<TAB>message"
# lines on stdin. Returns non-zero if the user cancels.
progress() {
  if have_gui; then
    awk -F'\t' '{ print $1; print "# " $2; fflush() }' |
      zenity --progress --title="$TITLE" --width=480 --auto-close --text="$1"
  else
    awk -F'\t' '{ printf "\r%3d%%  %s\033[K", $1, $2; fflush() } END { print "" }'
  fi
}

do_download() {
  local url="$1" total pid
  total="$(remote_size "$url")"
  if [ "$total" -gt 0 ]; then
    local need=$((total - $(stat -c %s "$DOWNLOAD.part" 2> /dev/null || echo 0)))
    if [ "$(free_bytes)" -lt "$need" ]; then
      die "Not enough free space to download the client.\n\nNeeded: $(human "$need")\nAvailable: $(human "$(free_bytes)")"
    fi
  fi

  # Resumable: a cancelled or interrupted download continues next time.
  curl -fL --retry 5 --retry-delay 5 -C - -sS -o "$DOWNLOAD.part" "$url" &
  pid=$!

  set +e
  {
    while kill -0 "$pid" 2> /dev/null; do
      local have
      have="$(stat -c %s "$DOWNLOAD.part" 2> /dev/null || echo 0)"
      if [ "$total" -gt 0 ]; then
        printf '%d\tDownloaded %s of %s\n' $((have * 99 / total)) "$(human "$have")" "$(human "$total")"
      else
        printf '0\tDownloaded %s\n' "$(human "$have")"
      fi
      sleep 1
    done
    printf '100\tDownload finished\n'
  } | progress "Downloading Unreal Tournament..."
  local ui_status=${PIPESTATUS[1]}
  if [ "$ui_status" -ne 0 ]; then
    kill "$pid" 2> /dev/null
    wait "$pid" 2> /dev/null
    set -e
    die "Download cancelled. It will resume the next time you start Unreal Tournament."
  fi
  wait "$pid"
  local curl_status=$?
  set -e
  [ "$curl_status" -eq 0 ] || die "Download failed (curl exit $curl_status) from:\n$url"

  mv "$DOWNLOAD.part" "$DOWNLOAD"
}

do_extract() {
  local zip="$1" staging="$INSTALL_DIR.new" uncompressed entries bin root
  uncompressed="$(unzip -Zt "$zip" 2> /dev/null | awk '{ for (i = 1; i < NF; i++) if ($(i + 1) == "bytes") { print $i; exit } }')" || true
  entries="$(unzip -Z1 "$zip" 2> /dev/null | wc -l)" || true
  [ -n "$uncompressed" ] && [ "$entries" -gt 0 ] || die "$zip is not a readable zip archive."

  if [ "$(free_bytes)" -lt $((uncompressed + SPARE_BYTES)) ]; then
    die "Not enough free space to unpack the client.\n\nNeeded: $(human $((uncompressed + SPARE_BYTES)))\nAvailable: $(human "$(free_bytes)")"
  fi

  rm -rf "$staging"
  mkdir -p "$staging"
  set +e
  unzip -o "$zip" -d "$staging" |
    awk -v total="$entries" '/^ *(inflating|extracting|creating):/ { n++; printf "%d\tUnpacking file %d of %d\n", n * 99 / total, n, total; fflush() }
         END { print "100\tUnpacked" }' |
    progress "Unpacking Unreal Tournament..."
  local unzip_status=${PIPESTATUS[0]} ui_status=${PIPESTATUS[2]}
  set -e
  if [ "$ui_status" -ne 0 ] || [ "$unzip_status" -ne 0 ]; then
    rm -rf "$staging"
    [ "$ui_status" -ne 0 ] && die "Installation cancelled."
    die "Unpacking failed (unzip exit $unzip_status). The archive may be corrupt."
  fi

  bin="$(find "$staging" -path '*/Engine/Binaries/Linux/UE4-Linux-Shipping' -type f -print -quit)"
  if [ -z "$bin" ]; then
    rm -rf "$staging"
    die "The archive does not contain an Unreal Tournament Linux client\n(Engine/Binaries/Linux/UE4-Linux-Shipping is missing)."
  fi
  root="${bin%/Engine/Binaries/Linux/UE4-Linux-Shipping}"

  # Epic's zip was made on Windows and carries no Unix permissions.
  find "$root" -path '*/Binaries/Linux/*' -type f \
    ! -name '*.so' ! -name '*.modules' ! -name '*.debug' ! -name '*.sym' \
    -exec chmod +x {} +

  rm -rf "$INSTALL_DIR"
  mv "$staging" "$INSTALL_DIR"
  echo "${root#"$staging"/}" > "$STAMP"
}

install_client() {
  local choice url zip
  info "Unreal Tournament's game data is not included in this snap.\n\nIt will now be installed into:\n$INSTALL_DIR\n\nThe download is about 11 GB and needs roughly 35 GB of free space while installing."

  if have_gui; then
    choice="$(zenity --list --radiolist --title="$TITLE" --width=480 --height=240 \
      --text="Where should the game data come from?" \
      --column="" --column="Source" \
      TRUE "Download from Epic" \
      FALSE "Use an UnrealTournament Linux zip I already have")" || exit 1
  else
    choice="Download from Epic"
    [ -n "${UT_CLIENT_ZIP:-}" ] && choice="local"
  fi

  if [ "$choice" = "Download from Epic" ]; then
    url="$(download_url)"
    do_download "$url"
    do_extract "$DOWNLOAD"
    rm -f "$DOWNLOAD"
  else
    if have_gui; then
      zip="$(zenity --file-selection --title="Select UnrealTournament Linux zip archive" \
        --file-filter="Zip archives | *.zip")" || exit 1
    else
      zip="$UT_CLIENT_ZIP"
    fi
    [ -r "$zip" ] || die "Cannot read $zip.\n\nFiles outside your home folder need: snap connect unrealtournament:removable-media"
    do_extract "$zip"
  fi
}

# --setup reinstalls. The old data is only replaced once the new copy has
# unpacked successfully, so cancelling leaves a working install in place.
if [ "${1:-}" = "--setup" ]; then
  shift
  install_client
elif [ ! -f "$STAMP" ]; then
  install_client
fi

GAME_ROOT="$INSTALL_DIR/$(cat "$STAMP")"
BIN="$GAME_ROOT/Engine/Binaries/Linux/UE4-Linux-Shipping"
[ -x "$BIN" ] || die "The installed client looks broken. Run unrealtournament.setup to reinstall it."

# UE 4.15's bundled SDL2 predates its Wayland backend; run under XWayland.
export SDL_VIDEODRIVER="${SDL_VIDEODRIVER:-x11}"
export SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS=1

exec "$BIN" UnrealTournament "$@"
