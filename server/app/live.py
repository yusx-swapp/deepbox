"""LiveSession — the platform value layer.

For every session we keep:
  - a pyte headless terminal emulator that holds the AUTHORITATIVE current screen
    (so a reconnecting/late viewer can be restored instantly, bounded by screen size)
  - an on-disk asciicast v2 recording (DVR) of every output chunk with timestamps
    (so the full session can be replayed / audited — something a local terminal lacks)
  - the set of subscribers (browser connections currently watching)

Viewers come and go freely; the PTY (the live process) lives on the devbox and is
NOT affected by viewers detaching. This module never touches the PTY directly — it
only records what flows through and broadcasts it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pyte

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "sessions"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_COLS = 120
DEFAULT_ROWS = 30


def _sgr_prefix(char) -> str:
    """Build an ANSI SGR sequence for a pyte Char's attributes."""
    codes = ["0"]
    fg = char.fg
    bg = char.bg
    if char.bold:
        codes.append("1")
    if char.italics:
        codes.append("3")
    if char.underscore:
        codes.append("4")
    if char.reverse:
        codes.append("7")
    if fg and fg != "default":
        codes.append(_color_code(fg, fgbg="fg"))
    if bg and bg != "default":
        codes.append(_color_code(bg, fgbg="bg"))
    return "\x1b[" + ";".join(c for c in codes if c) + "m"


_NAMED = {
    "black": 0, "red": 1, "green": 2, "brown": 3, "blue": 4,
    "magenta": 5, "cyan": 6, "white": 7,
}


def _color_code(col: str, fgbg: str) -> str:
    base = 30 if fgbg == "fg" else 40
    if col in _NAMED:
        return str(base + _NAMED[col])
    # pyte gives 6-hex-digit truecolor strings for xterm-256/truecolor
    try:
        r = int(col[0:2], 16); g = int(col[2:4], 16); b = int(col[4:6], 16)
        lead = 38 if fgbg == "fg" else 48
        return f"{lead};2;{r};{g};{b}"
    except Exception:
        return ""


def _render_line(line, columns: int) -> str:
    out: list[str] = []
    last = ""
    for col in range(columns):
        ch = line[col]
        pre = _sgr_prefix(ch)
        if pre != last:
            out.append(pre)
            last = pre
        out.append(ch.data or " ")
    out.append("\x1b[0m")
    return "".join(out)


def serialize_screen(screen: pyte.Screen) -> str:
    """Reproduce scrollback + exact current picture in a fresh terminal.

    ED(2) clears only the viewport (not xterm scrollback), so historical lines
    are emitted first, then the viewport is cleared/repainted and cursor placed.
    """
    out: list[str] = []
    history = getattr(screen, "history", None)
    if history:
        for line in history.top:
            out.append(_render_line(line, screen.columns))
            out.append("\r\n")
    out.append("\x1b[2J\x1b[H")  # clear viewport + home, preserve scrollback
    for row in range(screen.lines):
        line = screen.buffer[row]
        out.append(f"\x1b[{row + 1};1H")
        out.append(_render_line(line, screen.columns))
    out.append("\x1b[0m")
    # place cursor where it belongs
    out.append(f"\x1b[{screen.cursor.y + 1};{screen.cursor.x + 1}H")
    return "".join(out)


class LiveSession:
    def __init__(self, session_id: str, cols: int, rows: int):
        self.session_id = session_id
        self.cols = cols
        self.rows = rows
        self.screen = pyte.HistoryScreen(cols, rows, history=5000)
        self.stream = pyte.ByteStream(self.screen)
        self.subscribers: set = set()
        self.ended = False
        self.exit_code: int | None = None
        self.start_time = time.time()
        self.cast_path = DATA_DIR / f"{session_id}.cast"
        self._open_cast()

    # ---- DVR (asciicast v2) ----
    def _open_cast(self):
        new = not self.cast_path.exists()
        self._cast = open(self.cast_path, "a", encoding="utf-8")
        if new:
            header = {"version": 2, "width": self.cols, "height": self.rows,
                      "timestamp": int(self.start_time), "env": {"TERM": "xterm-256color"}}
            self._cast.write(json.dumps(header) + "\n")
            self._cast.flush()

    def _record(self, kind: str, data: str):
        t = round(time.time() - self.start_time, 6)
        self._cast.write(json.dumps([t, kind, data]) + "\n")
        self._cast.flush()

    def replay_into_screen(self):
        """Rebuild current screen from an existing .cast (used after server restart)."""
        if not self.cast_path.exists():
            return
        with open(self.cast_path, encoding="utf-8") as fp:
            first = True
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                if first:
                    first = False  # header
                    continue
                try:
                    _, kind, data = json.loads(line)
                except Exception:
                    continue
                if kind == "o":
                    self.stream.feed(data.encode("utf-8", "replace"))

    # ---- live path ----
    def feed_output(self, data: str):
        """A chunk of PTY output arrived: update screen + record + (caller broadcasts)."""
        self.stream.feed(data.encode("utf-8", "replace"))
        self._record("o", data)

    def record_input(self, data: str):
        self._record("i", data)

    def resize(self, cols: int, rows: int):
        if cols == self.cols and rows == self.rows:
            return
        self.cols, self.rows = cols, rows
        self.screen.resize(rows, cols)
        self._record("r", f"{cols}x{rows}")

    def restore_bytes(self) -> str:
        return serialize_screen(self.screen)

    def mark_ended(self, code: int | None):
        self.ended = True
        self.exit_code = code
        try:
            self._record("x", str(code))
            self._cast.close()
        except Exception:
            pass


class LiveRegistry:
    def __init__(self):
        self._sessions: dict[str, LiveSession] = {}

    def get(self, session_id: str) -> LiveSession | None:
        return self._sessions.get(session_id)

    def get_or_create(self, session_id: str, cols: int = DEFAULT_COLS,
                      rows: int = DEFAULT_ROWS) -> LiveSession:
        ls = self._sessions.get(session_id)
        if ls is None:
            ls = LiveSession(session_id, cols, rows)
            # server restarted but session had prior history → rebuild screen
            if ls.cast_path.exists() and ls.cast_path.stat().st_size > 0:
                try:
                    ls.replay_into_screen()
                except Exception:
                    pass
            self._sessions[session_id] = ls
        return ls

    def drop(self, session_id: str):
        ls = self._sessions.pop(session_id, None)
        if ls:
            try:
                ls._cast.close()
            except Exception:
                pass


live_registry = LiveRegistry()
