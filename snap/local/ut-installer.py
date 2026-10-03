#!/usr/bin/env python3
"""First-run installer for the Unreal Tournament (UE4 pre-alpha) Linux client.

Epic's client data is never shipped in the snap. This downloads Epic's last
Linux client build (or unpacks a zip the user already has) into
$SNAP_USER_COMMON, showing a GTK4/libadwaita window, which runs natively on
Wayland, or plain terminal output when there is no display.

Exit status is 0 once the client is installed, non-zero otherwise.
"""

import os
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile

# Last Linux client Epic shipped (Release-Next, CL 3525360, 2017-07).
DEFAULT_URL = (
    "https://s3.amazonaws.com/unrealtournament/ShippedBuilds/"
    "%2B%2BUT%2BRelease-Next-CL-3525360/UnrealTournament-Client-XAN-3525360-Linux.zip"
)
DEFAULT_SIZE = 11_369_141_375

# $SNAP_USER_COMMON isn't versioned, so the ~23 GB of game data is not copied
# on every snap refresh the way $SNAP_USER_DATA would be.
COMMON = os.environ.get("SNAP_USER_COMMON") or os.path.expanduser("~/.local/share/unrealtournament")
INSTALL_DIR = os.path.join(COMMON, "game")
STAGING_DIR = INSTALL_DIR + ".new"
STAMP = os.path.join(INSTALL_DIR, ".installed")
DOWNLOAD = os.path.join(COMMON, "ut-client.zip")
PARTIAL = DOWNLOAD + ".part"
BINARY = "Engine/Binaries/Linux/UE4-Linux-Shipping"

# Headroom for config, shader cache and logs on top of the unpacked data.
SPARE_BYTES = 2 * 1024**3
CHUNK = 1024**2


class InstallError(Exception):
    pass


class Cancelled(Exception):
    pass


def human_size(n):
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1000


def time_left(seconds):
    if seconds < 60:
        return "less than a minute left"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"about {minutes} minute{'s' if minutes != 1 else ''} left"
    hours, minutes = divmod(minutes, 60)
    return f"about {hours} h {minutes:02d} min left"


def free_bytes():
    return shutil.disk_usage(COMMON).free


def download_url():
    try:
        url = subprocess.run(
            ["snapctl", "get", "download-url"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        url = ""
    return url or DEFAULT_URL


class Rate:
    """Smoothed transfer rate and time remaining."""

    def __init__(self):
        self.last_time = time.monotonic()
        self.last_done = None
        self.speed = 0.0

    def update(self, done):
        now = time.monotonic()
        if self.last_done is None:
            self.last_done = done
        elapsed = now - self.last_time
        if elapsed >= 0.5:
            sample = (done - self.last_done) / elapsed
            self.speed = sample if self.speed == 0 else 0.8 * self.speed + 0.2 * sample
            self.last_time, self.last_done = now, done
        return self.speed

    def eta(self, done, total):
        return (total - done) / self.speed if self.speed > 0 else None


class Installer:
    """Downloads and unpacks the client. UI-agnostic; reports through callbacks.

    report(phase, done, total, detail) is called from the worker thread with
    phase "download" or "unpack".
    """

    def __init__(self, report):
        self.report = report
        self.cancel = threading.Event()

    def _check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled()

    def _progress(self, phase, done, total, rate, verb):
        speed = rate.update(done)
        parts = [f"{human_size(done)} of {human_size(total)}"]
        if speed > 0:
            parts.append(f"{human_size(speed)}/s")
            eta = rate.eta(done, total)
            if eta is not None:
                parts.append(time_left(eta))
        self.report(phase, done, total, f"{verb} " + " · ".join(parts))

    def download(self, url):
        os.makedirs(COMMON, exist_ok=True)
        have = os.path.getsize(PARTIAL) if os.path.exists(PARTIAL) else 0
        request = urllib.request.Request(url, headers={"User-Agent": "unrealtournament-snap"})
        if have:
            request.add_header("Range", f"bytes={have}-")
        try:
            response = urllib.request.urlopen(request, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code == 416 and have:  # Already complete.
                os.replace(PARTIAL, DOWNLOAD)
                return
            raise InstallError(f"The server refused the download (HTTP {e.code}).\n{url}") from e
        except (urllib.error.URLError, OSError) as e:
            raise InstallError(f"Could not reach the download server.\n{e}") from e

        with response:
            if response.status != 206:  # Server ignored the range; start over.
                have = 0
            length = int(response.headers.get("Content-Length") or 0)
            total = have + length if length else 0
            if length and free_bytes() < length:
                raise InstallError(
                    "There isn't enough free space to download the game.\n"
                    f"Needed: {human_size(length)} · Available: {human_size(free_bytes())}"
                )
            rate = Rate()
            done = have
            with open(PARTIAL, "ab" if have else "wb") as out:
                while True:
                    self._check_cancel()
                    try:
                        chunk = response.read(CHUNK)
                    except OSError as e:
                        raise InstallError(f"The download was interrupted.\n{e}") from e
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    self._progress("download", done, total or done, rate, "Downloaded")
        if total and done < total:
            raise InstallError("The download ended early. Start the installer again to resume it.")
        os.replace(PARTIAL, DOWNLOAD)

    def unpack(self, path):
        try:
            archive = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError) as e:
            raise InstallError(f"{os.path.basename(path)} is not a readable zip archive.") from e

        with archive:
            members = archive.infolist()
            if not any(m.filename.endswith(BINARY) for m in members):
                raise InstallError(
                    "This archive doesn't contain an Unreal Tournament Linux client\n"
                    f"({BINARY} is missing)."
                )
            total = sum(m.file_size for m in members)
            if free_bytes() < total + SPARE_BYTES:
                raise InstallError(
                    "There isn't enough free space to unpack the game.\n"
                    f"Needed: {human_size(total + SPARE_BYTES)} · Available: {human_size(free_bytes())}"
                )

            shutil.rmtree(STAGING_DIR, ignore_errors=True)
            os.makedirs(STAGING_DIR)
            staging_root = os.path.realpath(STAGING_DIR)
            rate = Rate()
            done = 0
            for member in members:
                self._check_cancel()
                target = os.path.realpath(os.path.join(STAGING_DIR, member.filename))
                if not target.startswith(staging_root + os.sep):
                    raise InstallError(f"Refusing to unpack unsafe path {member.filename!r}.")
                if member.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                try:
                    with archive.open(member) as src, open(target, "wb") as dst:
                        while chunk := src.read(CHUNK):
                            self._check_cancel()
                            dst.write(chunk)
                            done += len(chunk)
                            self._progress("unpack", done, total, rate, "Unpacked")
                except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError) as e:
                    raise InstallError(f"Unpacking failed; the archive may be corrupt.\n{e}") from e

        root = None
        for dirpath, _dirs, files in os.walk(STAGING_DIR):
            if dirpath.endswith("Engine/Binaries/Linux") and "UE4-Linux-Shipping" in files:
                root = dirpath[: -len("/Engine/Binaries/Linux")]
                break
        if root is None:
            raise InstallError(f"The unpacked archive is missing {BINARY}.")

        # Epic's zip was made on Windows and carries no Unix permissions.
        for dirpath, _dirs, files in os.walk(root):
            if not dirpath.endswith("/Binaries/Linux"):
                continue
            for name in files:
                if name.endswith((".so", ".modules", ".debug", ".sym")):
                    continue
                path = os.path.join(dirpath, name)
                os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        relative_root = os.path.relpath(root, STAGING_DIR)
        # Only now replace any previous install, so a failure keeps it working.
        shutil.rmtree(INSTALL_DIR, ignore_errors=True)
        os.replace(STAGING_DIR, INSTALL_DIR)
        with open(STAMP, "w") as f:
            f.write(relative_root + "\n")

    def install_from_download(self, url):
        self.download(url)
        self.unpack(DOWNLOAD)
        os.remove(DOWNLOAD)

    def install_from_zip(self, path):
        self.unpack(path)


def run_text():
    """Terminal fallback for when no display is available."""
    last = [0.0]

    def report(phase, done, total, detail):
        now = time.monotonic()
        if now - last[0] < 0.25 and done < total:
            return
        last[0] = now
        percent = done * 100 // total if total else 0
        print(f"\r{percent:3d}%  {detail}\033[K", end="", flush=True)

    installer = Installer(report)
    zip_path = os.environ.get("UT_CLIENT_ZIP")
    print(f"Installing the Unreal Tournament client into {INSTALL_DIR}")
    try:
        if zip_path:
            installer.install_from_zip(zip_path)
        else:
            print("Downloading Epic's Linux client (about 11.4 GB). Set UT_CLIENT_ZIP to use a local zip.")
            installer.install_from_download(download_url())
    except KeyboardInterrupt:
        print("\nCancelled. The download will resume next time.", file=sys.stderr)
        return 1
    except InstallError as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return 1
    print()
    return 0


def run_gui():
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio, GLib, Gtk

    class InstallerWindow(Adw.ApplicationWindow):
        def __init__(self, app):
            super().__init__(application=app, title="Unreal Tournament Setup")
            self.set_default_size(560, 600)
            self.installer = None
            self.worker = None
            self.last_ui_update = 0.0
            self.connect("close-request", self.on_close_request)

            self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
            toolbar = Adw.ToolbarView()
            toolbar.add_top_bar(Adw.HeaderBar(show_title=False))
            toolbar.set_content(self.stack)
            self.set_content(toolbar)

            self.stack.add_named(self.build_choose_page(), "choose")
            self.stack.add_named(self.build_progress_page(), "progress")
            self.stack.add_named(self.build_error_page(), "error")

        @staticmethod
        def pill(label, style=None):
            button = Gtk.Button(label=label, halign=Gtk.Align.CENTER)
            button.add_css_class("pill")
            if style:
                button.add_css_class(style)
            return button

        @staticmethod
        def page(icon_name, title, description, child):
            """A centred icon / title / description / child column.

            Built by hand rather than with Adw.StatusPage so the clamp can be
            sized in pixels: the default font-relative units collapse to the
            minimum width when no font DPI setting is available (compositors
            without a settings portal), squeezing the text into a thin column.
            """
            icon = Gtk.Image(icon_name=icon_name, pixel_size=96, margin_bottom=12)
            icon.add_css_class("dim-label")
            title_label = Gtk.Label(label=title, wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=60)
            title_label.add_css_class("title-1")
            description_label = Gtk.Label(
                label=description, wrap=True, justify=Gtk.Justification.CENTER, margin_bottom=18,
                max_width_chars=60,
            )
            column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER)
            for widget in (icon, title_label, description_label, child):
                column.append(widget)
            clamp = Adw.Clamp(maximum_size=420, unit=Adw.LengthUnit.PX, child=column, margin_top=24, margin_bottom=24,
                              margin_start=24, margin_end=24)
            scroller = Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
            scroller.icon, scroller.title, scroller.description = icon, title_label, description_label
            return scroller

        def build_choose_page(self):
            resuming = os.path.exists(PARTIAL)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
            download = self.pill("Resume Download" if resuming else "Download", "suggested-action")
            download.connect("clicked", self.on_download)
            local = self.pill("Use a Zip File…")
            local.connect("clicked", self.on_choose_zip)
            box.append(download)
            box.append(local)
            return self.page(
                "folder-download-symbolic",
                "Install Unreal Tournament",
                "The game data isn't included in this snap. Download Epic's Linux client, "
                "or use a client zip you already have.\n\n"
                f"The download is {human_size(DEFAULT_SIZE)} and needs about 35 GB of free "
                f"space while installing. {human_size(free_bytes())} is available.",
                box,
            )

        def build_progress_page(self):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            self.bar = Gtk.ProgressBar(margin_start=24, margin_end=24)
            self.detail = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=60)
            self.detail.add_css_class("dim-label")
            self.detail.add_css_class("numeric")
            self.cancel_button = self.pill("Cancel")
            self.cancel_button.set_margin_top(12)
            self.cancel_button.connect("clicked", self.on_cancel)
            box.append(self.bar)
            box.append(self.detail)
            box.append(self.cancel_button)
            self.progress_page = self.page("folder-download-symbolic", "", "", box)
            return self.progress_page

        def build_error_page(self):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
            retry = self.pill("Try Again", "suggested-action")
            retry.connect("clicked", lambda *_: self.reset_to_choose())
            close = self.pill("Close")
            close.connect("clicked", lambda *_: self.close())
            box.append(retry)
            box.append(close)
            self.error_page = self.page("dialog-error-symbolic", "Installation Failed", "", box)
            return self.error_page

        def reset_to_choose(self):
            # Rebuild so the free-space figure and Resume label are current.
            self.stack.remove(self.stack.get_child_by_name("choose"))
            self.stack.add_named(self.build_choose_page(), "choose")
            self.stack.set_visible_child_name("choose")

        def show_progress(self, title, description):
            self.progress_page.icon.set_from_icon_name("folder-download-symbolic")
            self.progress_page.title.set_label(title)
            self.progress_page.description.set_label(description)
            self.bar.set_fraction(0)
            self.bar.set_show_text(False)
            self.detail.set_label("Starting…")
            self.cancel_button.set_sensitive(True)
            self.stack.set_visible_child_name("progress")

        def on_download(self, _button):
            url = download_url()
            self.show_progress("Downloading", "Fetching the Unreal Tournament client from Epic.")
            self.start(lambda installer: installer.install_from_download(url))

        def on_choose_zip(self, _button):
            dialog = Gtk.FileDialog(title="Select an Unreal Tournament Linux Zip", modal=True)
            zip_filter = Gtk.FileFilter(name="Zip archives")
            zip_filter.add_suffix("zip")
            filters = Gio.ListStore.new(Gtk.FileFilter)
            filters.append(zip_filter)
            dialog.set_filters(filters)
            dialog.open(self, None, self.on_zip_chosen)

        def on_zip_chosen(self, dialog, result):
            try:
                path = dialog.open_finish(result).get_path()
            except GLib.Error:
                return  # Dismissed.
            self.show_progress("Installing", f"Unpacking {os.path.basename(path)}.")
            self.start(lambda installer: installer.install_from_zip(path))

        def start(self, job):
            self.installer = Installer(self.report)
            self.worker = threading.Thread(target=self.run_job, args=(job,), daemon=True)
            self.worker.start()

        def run_job(self, job):
            try:
                job(self.installer)
            except Cancelled:
                GLib.idle_add(self.reset_to_choose)
            except InstallError as e:
                GLib.idle_add(self.show_error, str(e))
            except Exception as e:  # Keep the window usable on unexpected failures.
                GLib.idle_add(self.show_error, f"Unexpected error: {e}")
            else:
                GLib.idle_add(self.finish)

        def report(self, phase, done, total, detail):
            now = time.monotonic()
            if now - self.last_ui_update < 0.1 and done < total:
                return
            self.last_ui_update = now
            GLib.idle_add(self.update_progress, phase, done, total, detail)

        def update_progress(self, phase, done, total, detail):
            if phase == "unpack":
                self.progress_page.icon.set_from_icon_name("package-x-generic-symbolic")
                self.progress_page.title.set_label("Unpacking")
                self.progress_page.description.set_label("Almost there. Unpacking the game data.")
            else:
                self.progress_page.icon.set_from_icon_name("folder-download-symbolic")
            fraction = done / total if total else 0
            self.bar.set_fraction(fraction)
            self.bar.set_show_text(True)
            self.bar.set_text(f"{fraction:.0%}")
            self.detail.set_label(detail)
            return GLib.SOURCE_REMOVE

        def show_error(self, message):
            self.error_page.description.set_label(message)
            self.stack.set_visible_child_name("error")
            return GLib.SOURCE_REMOVE

        def finish(self):
            self.get_application().succeeded = True
            self.get_application().quit()
            return GLib.SOURCE_REMOVE

        def on_cancel(self, _button):
            if self.installer:
                self.cancel_button.set_sensitive(False)
                self.detail.set_label("Cancelling…")
                self.installer.cancel.set()

        def on_close_request(self, _window):
            # Closing mid-download keeps the partial file so it resumes later.
            if self.installer:
                self.installer.cancel.set()
            return False

    class InstallerApp(Adw.Application):
        def __init__(self):
            super().__init__(
                application_id="io.github.kenvandine.UnrealTournamentSetup",
                flags=Gio.ApplicationFlags.NON_UNIQUE,
            )
            self.succeeded = False

        def do_activate(self):
            InstallerWindow(self).present()

    app = InstallerApp()
    app.run([sys.argv[0]])
    return 0 if app.succeeded else 1


def main():
    os.makedirs(COMMON, exist_ok=True)
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        try:
            return run_gui()
        except (ImportError, ValueError) as e:
            print(f"GTK unavailable ({e}); falling back to the terminal.", file=sys.stderr)
    return run_text()


if __name__ == "__main__":
    sys.exit(main())
