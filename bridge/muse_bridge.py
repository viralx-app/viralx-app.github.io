#!/usr/bin/env python3
"""
Muse Bridge - PC side (v1 + library scanner)
--------------------------------------------
Small command bridge that runs on Keyz's Windows PC.

It listens on a TCP socket on the private Tailscale network, accepts
short text commands from Claude, and forwards transport commands as
MIDI notes to a virtual loopMIDI port. An FL Studio device script
(device_MuseBridge.py) listening on that same port turns the notes
into FL transport actions.

    Muse --(Tailscale TCP)--> muse_bridge.py --(MIDI notes)-->
    loopMIDI port "MuseBridge" --> FL device script --> FL transport

The bridge also maps Keyz's sound library: SCAN walks the folders in
LIBRARY_ROOTS and writes inventory.json (the map Muse builds CHOPS
presets from), and GET streams one audio file back over the socket so
Muse can pull an individual sound later.

Protocol (one connection):
    1. Client sends:  AUTH <token>
       Server replies: OK AUTH       (or ERR AUTH and closes)
    2. Client sends one command per line:
           PLAY / PAUSE / STOP / RECORD  -> OK <cmd>   (MIDI note fired)
           PING                          -> PONG       (no MIDI sent)
           SCAN                          -> SCAN_DONE files=<n> bytes=<total> inventory=inventory.json
           GET <path>                    -> FILE <size> + raw bytes + OK GET
           CLOSE_FL                      -> OK CLOSE_FL (graceful close; ERR FL_STILL_OPEN if FL waits on its save prompt)
           OPEN_FL                       -> OK OPEN_FL   (launches FL64.exe)
           RESTART_FL                    -> OK RESTART_FL (close, wait for exit, reopen)
       Errors reply:  ERR <reason>

GET paths are relative to a library root, e.g. "GET Kicks/kick.wav"
or "GET 0/Kicks/kick.wav" (leading number = which root). Paths that
escape a root ("..", absolute paths) are refused, and only audio
files are served. After the FILE header line the server sends
exactly <size> raw bytes, then a final "OK GET" line.

Only third-party deps: mido + python-rtmidi (see requirements.txt),
and only for the MIDI transport commands - the scanner (SCAN / GET /
scan_library.py) is pure standard library and works without them.
Everything else is Python 3 standard library.

Run it:  python muse_bridge.py
Stop it: Ctrl+C
"""

import datetime
import glob
import ipaddress
import json
import os
import socket
import socketserver
import subprocess
import sys
import time

# ------------------------------------------------------------------
# CONFIG - edit these if needed
# ------------------------------------------------------------------
# TCP port the bridge listens on.
TCP_PORT = 47800

# Bind address. Leave as "" to auto-pick this machine's Tailscale
# address (100.64.0.0/10 range) if one is found, else fall back to
# 0.0.0.0 (all interfaces). The fallback is acceptable ONLY because
# this PC is reached over a private tailnet; if the fallback ever
# triggers on an untrusted network, set this to the Tailscale IP
# by hand.
HOST = ""

# Shared token. Muse must send this exact word after AUTH.
# Change it to a private word of your own; Muse needs the same one.
TOKEN = "muse-bridge"

# Virtual MIDI port to send to: the first output port whose name
# contains this text wins. If none matches, any port whose name
# contains "loopMIDI" is used instead.
PORT_NAME = "MuseBridge"

# Command -> MIDI note number. Mackie Control Universal transport notes
# (channel 1): FL's built-in "Mackie Control Universal" controller type
# turns these into transport actions, so no custom FL script is needed.
# PAUSE maps to STOP (Mackie has no separate pause; same as before).
COMMAND_NOTES = {
    "PLAY": 94,
    "PAUSE": 93,
    "STOP": 93,
    "RECORD": 95,
}

# ------------------------------------------------------------------
# LIBRARY CONFIG - the sound pack folders SCAN / GET work on
# ------------------------------------------------------------------
# Folders on THIS PC that hold the sound pack(s). CHANGE THIS to the
# real pack folder(s) before the first scan, e.g.
#   LIBRARY_ROOTS = [r"C:\Users\GLOBA\Music\Sound Packs"]
# More than one folder is fine - each is a "root" and files in the
# inventory remember which root they came from (by index: 0, 1, ...).
LIBRARY_ROOTS = [
    r"F:\PACKS",  # Keyz's pack: external drive F: (Keys Beats), folder PACKS - set 2026-11-12
]

# Audio file extensions the scanner counts (lowercase, no dot).
AUDIO_EXTS = {"wav", "mp3", "flac", "aif", "aiff", "ogg"}

# inventory.json is written next to this script.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INVENTORY_PATH = os.path.join(SCRIPT_DIR, "inventory.json")

# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------


def log(msg):
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    print("[%s] %s" % (stamp, msg), flush=True)


# ------------------------------------------------------------------
# Network helpers
# ------------------------------------------------------------------

TAILNET = ipaddress.ip_network("100.64.0.0/10")


def find_tailscale_ip():
    """Return this machine's Tailscale IPv4 address, or None."""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except socket.gaierror:
        return None
    for info in infos:
        ip = info[4][0]
        try:
            if ipaddress.ip_address(ip) in TAILNET:
                return ip
        except ValueError:
            continue
    return None


def choose_bind_host():
    if HOST:
        return HOST, "from config"
    ts_ip = find_tailscale_ip()
    if ts_ip:
        return ts_ip, "Tailscale address (auto-detected)"
    return "0.0.0.0", "WARNING: no Tailscale address found, binding ALL interfaces"


# ------------------------------------------------------------------
# Library scanner (pure stdlib - shared with scan_library.py)
# ------------------------------------------------------------------


def scan_roots(roots):
    """Walk every root recursively and catalogue the audio files.

    Returns (entries, total_bytes, missing_roots). Each entry is:
        {path, root, folder, name, ext, size_bytes}
    where "path" and "folder" are relative to the entry's root
    (forward slashes), "root" is the root's index in `roots`, and
    "folder" is "" for a file sitting directly in the root - the
    folder is the category (Kicks, Snares, Loops, ...).
    """
    entries = []
    total_bytes = 0
    missing = []
    for index, root in enumerate(roots):
        if not os.path.isdir(root):
            missing.append(root)
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for filename in filenames:
                ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
                if ext not in AUDIO_EXTS:
                    continue
                full = os.path.join(dirpath, filename)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                folder = os.path.dirname(rel)
                entries.append({
                    "path": rel,
                    "root": index,
                    "folder": folder,
                    "name": filename,
                    "ext": ext,
                    "size_bytes": size,
                })
                total_bytes += size
    entries.sort(key=lambda e: (e["root"], e["path"].lower()))
    return entries, total_bytes, missing


def write_inventory(entries, roots, total_bytes, out_path=INVENTORY_PATH):
    """Write the inventory map as JSON. Returns the path written."""
    data = {
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "roots": [os.path.abspath(r) for r in roots],
        "file_count": len(entries),
        "total_bytes": total_bytes,
        "files": entries,
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    return out_path


def resolve_library_file(rel):
    """Resolve a GET path to a real file inside a library root.

    Accepts "Kicks/kick.wav" (roots searched in order) or
    "0/Kicks/kick.wav" (leading number picks the root by index).
    Returns (absolute_path, None) on success or (None, reason) where
    reason is FORBIDDEN (escapes a root / absolute / ".."),
    NO_SUCH_FILE, or NOT_AUDIO. Only audio files are served - this
    socket must never become a general file door into the PC.
    """
    rel = (rel or "").strip().replace("\\", "/")
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        return None, "FORBIDDEN"
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or ":" in parts[0]:
        return None, "FORBIDDEN"

    root_indexes = list(range(len(LIBRARY_ROOTS)))
    if parts[0].isdigit():
        wanted = int(parts[0])
        if wanted >= len(LIBRARY_ROOTS):
            return None, "NO_SUCH_FILE"
        root_indexes = [wanted]
        parts = parts[1:]
        if not parts:
            return None, "NO_SUCH_FILE"

    saw_forbidden = False
    for index in root_indexes:
        root_real = os.path.realpath(LIBRARY_ROOTS[index])
        target = os.path.realpath(os.path.join(root_real, *parts))
        if os.path.commonpath([root_real, target]) != root_real:
            saw_forbidden = True
            continue
        if not os.path.isfile(target):
            continue
        ext = target.rsplit(".", 1)[-1].lower() if "." in target else ""
        if ext not in AUDIO_EXTS:
            return None, "NOT_AUDIO"
        return target, None
    if saw_forbidden:
        return None, "FORBIDDEN"
    return None, "NO_SUCH_FILE"


# ------------------------------------------------------------------
# MIDI output (mido + python-rtmidi)
# ------------------------------------------------------------------

_midi_out = None


def open_midi_port():
    """Open (once) and return the virtual MIDI output port."""
    global _midi_out
    if _midi_out is not None:
        return _midi_out

    try:
        import mido  # imported late so the scanner never needs it
    except ImportError:
        log("MIDI ERROR: mido is not installed "
            "(transport commands unavailable; SCAN/GET still work)")
        return None

    names = mido.get_output_names()
    chosen = None
    for name in names:
        if PORT_NAME.lower() in name.lower():
            chosen = name
            break
    if chosen is None:
        for name in names:
            if "loopmidi" in name.lower():
                chosen = name
                break
    if chosen is None:
        log("MIDI ERROR: no output port matching %r or 'loopMIDI'. "
            "Ports seen: %s" % (PORT_NAME, names or "none"))
        return None

    _midi_out = mido.open_output(chosen)
    log("MIDI: sending to port %r" % chosen)
    return _midi_out


def send_note(note):
    """Send one short note-on/note-off pulse on the virtual port."""
    out = open_midi_port()
    if out is None:
        return False
    import mido

    out.send(mido.Message("note_on", note=note, velocity=127))
    time.sleep(0.05)
    # Mackie-standard release: note_on with velocity 0 (real MCU hardware
    # never sends 0x80 note_off; FL's Mackie script can read a note_off
    # as a second button press and stop the transport).
    out.send(mido.Message("note_on", note=note, velocity=0))
    return True


# ------------------------------------------------------------------
# FL Studio process control (CLOSE_FL / OPEN_FL / RESTART_FL)
# ------------------------------------------------------------------

FL_PROCESS = "FL64.exe"
FL_EXE_GLOBS = (
    "C:\\Program Files\\Image-Line\\FL Studio*\\FL64.exe",
    "C:\\Program Files (x86)\\Image-Line\\FL Studio*\\FL64.exe",
)


def fl_running():
    """True if FL Studio's process exists."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq %s" % FL_PROCESS, "/NH"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:
        return False
    return FL_PROCESS.lower() in (out or "").lower()


def fl_exe_path():
    """Find FL64.exe under Program Files\\Image-Line (last folder wins)."""
    found = []
    for pattern in FL_EXE_GLOBS:
        found.extend(glob.glob(pattern))
    if not found:
        return None
    return sorted(found)[-1]


def close_fl():
    """Ask FL to close gracefully (same as clicking the X).

    taskkill without /F sends WM_CLOSE, so FL shows its normal
    save-project prompt if the project has unsaved changes. Returns
    True once FL's process is gone (waits up to ~25 s).
    """
    if not fl_running():
        return True
    try:
        subprocess.run(["taskkill", "/IM", FL_PROCESS],
                       capture_output=True, timeout=15)
    except Exception:
        return False
    for _ in range(50):
        if not fl_running():
            return True
        time.sleep(0.5)
    return False


def open_fl():
    """Launch FL Studio. Returns (ok, detail)."""
    exe = fl_exe_path()
    if exe is None:
        return False, "NO_FL_FOUND"
    try:
        subprocess.Popen([exe], cwd=os.path.dirname(exe),
                         creationflags=0x00000008 | 0x00000200)
    except Exception as exc:
        return False, "LAUNCH_FAILED %s" % exc
    return True, exe


# ------------------------------------------------------------------
# TCP command server
# ------------------------------------------------------------------


class BridgeHandler(socketserver.StreamRequestHandler):
    def handle(self):
        peer = "%s:%s" % (self.client_address[0], self.client_address[1])
        log("connect from %s" % peer)
        try:
            self._session()
        except (ConnectionResetError, BrokenPipeError):
            log("connection from %s dropped" % peer)
        log("disconnect %s" % peer)

    def _reply(self, text):
        self.wfile.write((text + "\n").encode("utf-8"))

    def _session(self):
        # --- auth handshake: first line must be "AUTH <token>" ---
        first = self.rfile.readline()
        if not first:
            return
        parts = first.decode("utf-8", "replace").strip().split(None, 1)
        if len(parts) != 2 or parts[0].upper() != "AUTH" or parts[1] != TOKEN:
            self._reply("ERR AUTH")
            log("rejected connection (bad auth)")
            return
        self._reply("OK AUTH")

        # --- command loop: one command per line ---
        # The verb is matched case-insensitively; the argument (a GET
        # path) keeps its original case.
        for raw in self.rfile:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            fields = line.split(None, 1)
            verb = fields[0].upper()
            arg = fields[1] if len(fields) > 1 else ""
            if verb == "PING":
                log("PING -> PONG (no MIDI)")
                self._reply("PONG")
            elif verb in COMMAND_NOTES:
                note = COMMAND_NOTES[verb]
                if send_note(note):
                    log("%s -> MIDI note %d" % (verb, note))
                    self._reply("OK " + verb)
                else:
                    self._reply("ERR NO_MIDI_PORT")
            elif verb == "CLOSE_FL":
                if close_fl():
                    log("CLOSE_FL -> FL closed")
                    self._reply("OK CLOSE_FL")
                else:
                    log("CLOSE_FL -> FL still open (save prompt?)")
                    self._reply("ERR FL_STILL_OPEN")
            elif verb == "OPEN_FL":
                ok, detail = open_fl()
                if ok:
                    log("OPEN_FL -> %s" % detail)
                    self._reply("OK OPEN_FL")
                else:
                    self._reply("ERR " + detail)
            elif verb == "RESTART_FL":
                if not close_fl():
                    log("RESTART_FL -> FL still open (save prompt?)")
                    self._reply("ERR FL_STILL_OPEN")
                else:
                    ok, detail = open_fl()
                    if ok:
                        log("RESTART_FL -> reopened %s" % detail)
                        self._reply("OK RESTART_FL")
                    else:
                        self._reply("ERR " + detail)
            elif verb == "SCAN":
                self._do_scan()
            elif verb == "GET":
                self._do_get(arg)
            else:
                log("unknown command %r" % verb)
                self._reply("ERR UNKNOWN_COMMAND")

    def _do_scan(self):
        entries, total_bytes, missing = scan_roots(LIBRARY_ROOTS)
        for root in missing:
            log("SCAN: root not found, skipped: %s" % root)
        write_inventory(entries, LIBRARY_ROOTS, total_bytes)
        log("SCAN -> %d files, %d bytes, inventory.json written"
            % (len(entries), total_bytes))
        self._reply("SCAN_DONE files=%d bytes=%d inventory=inventory.json"
                    % (len(entries), total_bytes))

    def _do_get(self, rel):
        path, err = resolve_library_file(rel)
        if err is not None:
            log("GET %r -> %s" % (rel, err))
            self._reply("ERR " + err)
            return
        size = os.path.getsize(path)
        log("GET %r -> streaming %d bytes" % (rel, size))
        # Framing: header line, exactly <size> raw bytes, then a
        # closing "OK GET" line so the line protocol resumes cleanly.
        self._reply("FILE %d" % size)
        self.wfile.flush()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        self.wfile.flush()
        self._reply("OK GET")


class BridgeServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    try:
        import mido  # noqa: F401
    except ImportError:
        # Not fatal: transport commands will report ERR NO_MIDI_PORT,
        # but the library scanner (SCAN / GET) is pure stdlib and
        # keeps working. Run  pip install -r requirements.txt  to
        # enable transport.
        log("WARNING: mido is not installed - transport commands "
            "unavailable until you run  pip install -r requirements.txt")

    host, why = choose_bind_host()
    server = BridgeServer((host, TCP_PORT), BridgeHandler)
    log("Muse Bridge listening on %s:%d (%s)" % (host, TCP_PORT, why))
    log("Token check is ON. Commands: PLAY PAUSE STOP RECORD PING SCAN GET CLOSE_FL OPEN_FL RESTART_FL")
    # Warm up the MIDI port now so a missing loopMIDI port is
    # reported at startup, not on the first command.
    open_midi_port()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
