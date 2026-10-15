#!/usr/bin/env python3
"""
mini-quark TUI — tela cheia a 60 fps, no estilo do Gemini CLI.

Requer: pip install rich
Coloque este arquivo na mesma pasta do mini_quark_cli.py (a lógica vem de lá).

    python mini_quark_tui.py
    python mini_quark_tui.py --yes            # sem confirmar execução de ferramentas
    python mini_quark_tui.py --art meu.txt    # usa essa arte só nesta sessão

Como funciona: a tela inteira é recomposta 60x por segundo num buffer alternativo do
terminal e só as linhas que mudaram são reescritas (sem os.system("clear"), que abre um
processo a cada frame e pisca). O modelo roda em outra thread, então a interface
continua viva (spinner, banner animado, rolagem) enquanto ele responde.

Teclas
    Enter envia · Ctrl+J / Alt+Enter nova linha · colar texto grande funciona
    ← → ↑ ↓ Home End Del · Ctrl+A/E · Ctrl+K/U/W · ↑/↓ nas bordas = histórico
    PgUp / PgDn rola a conversa · Ctrl+L limpa a tela · Ctrl+C interrompe / limpa / sai
    Ctrl+D sai (caixa vazia)

Comandos novos: /cls (limpa a tela), /cls all (limpa também o histórico de ↑), /ui
"""
import argparse
import atexit
import codecs
import ctypes
import io
import json
import os
import re
import sys
import signal
import threading
import time
import traceback
from ctypes import wintypes
from functools import lru_cache

from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

import mini_quark_cli as mq

console = Console(highlight=False)
IS_WIN = os.name == "nt"
REAL_OUT = sys.__stdout__
FPS = 60
FRAME = 1.0 / FPS
RESET = "\033[0m"
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# ═════════════════════════ configuração da interface ═════════════════════════
TUI_FILE = os.path.join(mq.HOME, "tui.json")
DEFAULT_COLORS = ["#4e8fe8", "#8a6fd1", "#c9657f"]
POSITIONS = ("top-right", "top-left", "bottom-right", "bottom-left")
UI = {}
APP = None


def ui_defaults():
    return {"art": "", "art_on": True, "pos": "top-right", "art_colors": list(DEFAULT_COLORS),
            "art_max_width": 0, "banner_colors": list(DEFAULT_COLORS), "border": "#5b8def",
            "banner": True, "tips": True, "anim": "banner", "mouse": True, "cfg_v": 2}


def load_ui():
    global UI
    UI = ui_defaults()
    try:
        with open(TUI_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("cfg_v", 1) < 2:   # versões antigas salvaram mouse=False por padrão
            data["mouse"] = True
        UI.update({k: v for k, v in data.items() if k in UI})
        UI["cfg_v"] = 2
    except (OSError, ValueError):
        pass


def save_ui():
    os.makedirs(mq.HOME, exist_ok=True)
    with open(TUI_FILE, "w", encoding="utf-8") as f:
        json.dump(UI, f, ensure_ascii=False, indent=2)


# ═════════════════════════ cores / gradiente ═════════════════════════
def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def gradient_at(colors, t):
    if not colors:
        return None
    if len(colors) == 1:
        return colors[0]
    t = min(max(t, 0.0), 1.0) * (len(colors) - 1)
    i = min(int(t), len(colors) - 2)
    a, b, f = hex_rgb(colors[i]), hex_rgb(colors[i + 1]), t - i
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * f) for x, y in zip(a, b))


def colorize(lines, colors, phase=None):
    """Gradiente horizontal. Com `phase` (0..1) o gradiente desliza em loop (animação)."""
    width = max((cell_len(l) for l in lines), default=1)
    palette = colors + colors[-2::-1] if (phase is not None and len(colors) >= 2) else None
    out = []
    for line in lines:
        t = Text(no_wrap=True, overflow="crop")
        x = 0
        for ch in line:
            if ch != " " and colors:
                pos = x / max(width - 1, 1)
                col = gradient_at(palette, (pos * 0.5 + phase) % 1.0) if palette else gradient_at(colors, pos)
                t.append(ch, col)
            else:
                t.append(ch)
            x += cell_len(ch)
        out.append(t)
    return out


# ═════════════════════════ cabeçalho (banner + dicas + arte) ═════════════════════════
CHEVRON = ["██╗   ", " ╚██╗ ", "  ╚██╗", "  ██╔╝", " ██╔╝ ", "██╔╝  "]
QUARK = [
    " ██████╗ ██╗   ██╗ █████╗ ██████╗ ██╗  ██╗",
    "██╔═══██╗██║   ██║██╔══██╗██╔══██╗██║ ██╔╝",
    "██║   ██║██║   ██║███████║██████╔╝█████╔╝ ",
    "██║▄▄ ██║██║   ██║██╔══██║██╔══██╗██╔═██╗ ",
    "╚██████╔╝╚██████╔╝██║  ██║██║  ██║██║  ██╗",
    " ╚══▀▀═╝  ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝",
]
DEMO_ART = r"""
      .  *  .
   *    _____    .
  .   .'  o  '.   *
     /  \ | /  \
 *  |  --(o)--  |  .
     \  / | \  /
  .   '._____.'   *
    *    .    .
""".strip("\n")


def left_block_lines(phase=None):
    lines = []
    if UI["banner"]:
        lines += colorize([c + " " + q for c, q in zip(CHEVRON, QUARK)], UI["banner_colors"], phase)
    if UI["tips"]:
        if lines:
            lines.append(Text(""))
        t = Text()
        t.append("Tips for getting started:\n", "bold")
        t.append("1. Ask questions, edit files, or run commands.\n")
        t.append("2. Be specific for the best results.\n")
        t.append("3. Use ")
        t.append("@path/to/file", "#c792ea")
        t.append(" to attach a file to your message.\n")
        t.append("4. ")
        t.append("/ui", "#c792ea")
        t.append(" to customize · ")
        t.append("/cls", "#c792ea")
        t.append(" clears the screen · ")
        t.append("/help", "#c792ea")
        t.append(" for more.")
        lines += t.split("\n")
    return lines


def art_lines(phase=None):
    raw = UI["art"]
    if not (UI["art_on"] and raw.strip()):
        return []
    raw = raw.expandtabs(4).replace("\r", "")
    rows = raw.split("\n")
    while rows and not rows[-1].strip():
        rows.pop()
    if "\x1b" in raw:
        texts = [Text.from_ansi(r) for r in rows]
        for t in texts:
            t.no_wrap, t.overflow = True, "crop"
    else:
        mw = UI["art_max_width"]
        if mw and mw > 0:
            rows = [r[:mw] for r in rows]
        texts = colorize(rows, UI["art_colors"], phase) if UI["art_colors"] else \
            [Text(r, no_wrap=True, overflow="crop") for r in rows]
    width = max((t.cell_len for t in texts), default=0)
    for t in texts:
        t.pad_right(width - t.cell_len)
    return texts


def compose_header(cols, phase_banner=None, phase_art=None):
    avail = max(cols - 1, 20)
    left = left_block_lines(phase_banner)
    art = art_lines(phase_art)
    if not art:
        return left
    art_w = art[0].cell_len
    left_w = max((t.cell_len for t in left), default=0)
    gap = 3
    right = UI["pos"].endswith("right")
    bottom = UI["pos"].startswith("bottom")

    if left and art_w + left_w + gap <= avail:
        n = max(len(left), len(art))

        def align(block):
            pad = n - len(block)
            blank = Text("")
            return ([blank] * pad + block) if bottom else (block + [blank] * pad)

        rows = []
        for l, a in zip(align(left), align(art)):
            row = Text(no_wrap=True, overflow="crop")
            if right:
                l = l.copy(); l.pad_right(avail - art_w - l.cell_len)
                row.append_text(l); row.append_text(a if a.cell_len else Text(" " * art_w))
            else:
                a = a.copy(); a.pad_right(art_w - a.cell_len)
                row.append_text(a); row.append(" " * gap); row.append_text(l)
            rows.append(row)
        return rows

    stacked = []
    for t in art:
        t = t.copy()
        t.truncate(avail, overflow="crop")
        if right and t.cell_len < avail:
            t.pad_left(avail - t.cell_len)
        stacked.append(t)
    if not left:
        return stacked
    return (left + [Text("")] + stacked) if bottom else (stacked + [Text("")] + left)


# ═════════════════════════ utilidades de render ═════════════════════════
def render_ansi(text):
    with console.capture() as cap:
        console.print(text, end="", no_wrap=True, overflow="crop", soft_wrap=True)
    return cap.get()


@lru_cache(maxsize=512)
def sty(s, style):
    return render_ansi(Text(s, style=style)) if s else ""


def wrap_ansi(raw, width):
    """Quebra uma linha (com ANSI) em linhas visuais de no máximo `width` colunas."""
    raw = raw.expandtabs(4)
    if cell_len(ANSI_RE.sub("", raw)) <= width:
        return [raw + RESET if "\x1b" in raw else raw]
    with console.capture() as cap:
        console.print(Text.from_ansi(raw), end="", width=width)
    return cap.get().split("\n")


def short_cwd():
    cwd, home = os.getcwd(), os.path.expanduser("~")
    return "~" + cwd[len(home):] if cwd.startswith(home) else cwd


def footer_ansi(width):
    left = short_cwd()
    auto = mq.AUTO or mq.cfg("auto")
    mid, mid_style = ("auto-exec (cuidado)", "#e2685c") if auto else ("confirm before exec", "#e8a94c")
    right = f"{mq.cfg('model')} ({len(mq.active_chat()['history'])} msgs)"
    total = len(left) + len(mid) + len(right)
    if total + 2 > width:
        mid, total = "", len(left) + len(right)
    gap1 = max((width - total) // 2, 1) if mid else max(width - total, 1)
    gap2 = max(width - total - gap1, 1) if mid else 0
    t = Text(no_wrap=True, overflow="crop")
    t.append(left, "dim"); t.append(" " * gap1)
    if mid:
        t.append(mid, mid_style); t.append(" " * gap2)
    t.append(right, "#7fb2ff")
    t.truncate(width, overflow="crop")
    return render_ansi(t)


def term_size():
    try:
        s = os.get_terminal_size(REAL_OUT.fileno())
        return s.columns, s.lines
    except OSError:
        return 100, 30


# ═════════════════════════ log da conversa (saída do mq) ═════════════════════════
class Log:
    """Recebe tudo que o mq imprime (de qualquer thread) e entrega linhas já quebradas."""

    def __init__(self):
        self.lock = threading.Lock()
        self.lines = []        # linhas completas (texto cru, com ANSI)
        self.partial = ""      # linha em andamento (streaming)
        self.rows = []         # linhas visuais das completas
        self.rows_upto = 0     # quantas de self.lines já estão em self.rows
        self.width = None
        self.dirty = False
        self._p_key, self._p_rows = None, []

    def write(self, s):
        s = s.replace("\r\n", "\n").replace("\r", "")
        with self.lock:
            parts = s.split("\n")
            self.partial += parts[0]
            for p in parts[1:]:
                self.lines.append(self.partial)
                self.partial = p
            if len(self.lines) > 4000:
                del self.lines[:1000]
                self.dirty = True  # a thread principal refaz as linhas no próximo sync

    def clear(self):
        with self.lock:
            self.lines, self.partial = [], ""
            self.dirty = True

    def sync(self, width):
        with self.lock:
            lines = list(self.lines[self.rows_upto:]) if not (self.dirty or width != self.width) else list(self.lines)
            partial = self.partial
            if self.dirty or width != self.width:
                self.width, self.rows, self.rows_upto, self.dirty = width, [], 0, False
            self.rows_upto += len(lines)
        # o trabalho pesado (rich) fica fora do lock: a thread do modelo nunca espera por ele
        for raw in lines:
            self.rows.extend(wrap_ansi(raw, width))
        key = (partial, width)
        if key != self._p_key:
            self._p_key = key
            self._p_rows = wrap_ansi(partial, width) if partial else []

    def count(self):
        return len(self.rows) + len(self._p_rows)

    def get(self, i):
        return self.rows[i] if i < len(self.rows) else self._p_rows[i - len(self.rows)]

    @property
    def empty(self):
        return not self.lines and not self.partial


class LogOut(io.TextIOBase):
    """Substitui sys.stdout/stderr: tudo que o mq imprime vai para o Log."""

    encoding = "utf-8"

    def __init__(self, log):
        self.log = log

    def write(self, s):
        self.log.write(s)
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return True

    def fileno(self):
        return REAL_OUT.fileno()

    def writable(self):
        return True


# ═════════════════════════ editor de linha ═════════════════════════
def wrap_rows(text, tw):
    rows, start, width, last_sp = [], 0, 0, -1
    for i, ch in enumerate(text):
        if ch == "\n":
            rows.append((start, i)); start, width, last_sp = i + 1, 0, -1
            continue
        w = cell_len(ch)
        if width + w > tw and i > start:
            if last_sp >= start:
                cut = last_sp + 1
                rows.append((start, cut)); start = cut
                width = cell_len(text[start:i])
            else:
                rows.append((start, i)); start, width = i, 0
            last_sp = -1
        if ch == " ":
            last_sp = i
        width += w
    rows.append((start, len(text)))
    return rows


def locate(rows, c):
    for r, (s, e) in enumerate(rows):
        if s <= c < e:
            return r
        if c == e and (r == len(rows) - 1 or rows[r + 1][0] != e):
            return r
    return len(rows) - 1


def index_at_col(text, s, e, col):
    x = 0
    for i in range(s, e):
        w = cell_len(text[i])
        if x + w > col:
            return i
        x += w
    return e


TOKEN = re.compile(r"\x1b\[[0-9;?<]*[A-Za-z~]|\x1bO[A-Za-z]|\x1b[\r\n]|\x1b.|[\s\S]")
HISTORY = []


class Editor:
    def __init__(self):
        self.text, self.cur, self.view = "", 0, 0
        self.hist_i, self.draft, self.tw = None, "", 60

    def frame(self, w, max_rows):
        """Caixa de entrada (sem rodapé). Retorna (linhas, linha_do_cursor, coluna)."""
        tw = self.tw = w - 6
        rows = wrap_rows(self.text, tw)
        crow = locate(rows, self.cur)
        if crow < self.view:
            self.view = crow
        if crow >= self.view + max_rows:
            self.view = crow - max_rows + 1
        self.view = max(0, min(self.view, max(len(rows) - max_rows, 0)))
        vis = rows[self.view:self.view + max_rows]
        b = UI["border"]
        above, below = self.view, len(rows) - (self.view + len(vis))

        def edge(l, r, label):
            lab = f" {label} " if label else ""
            return sty(l + "─" + lab + "─" * (w - 3 - len(lab)) + r, b)

        lines = [edge("╭", "╮", f"↑ {above} mais" if above else "")]
        side_l, side_r = sty("│ ", b), sty(" │", b)
        for k, (s, e) in enumerate(vis):
            absr = self.view + k
            prompt = sty("> ", "bold " + b) if absr == 0 else "  "
            if not self.text and absr == 0:
                ph = "Type your message or @path/to/file"
                while cell_len(ph) > tw:
                    ph = ph[:-1]
                body = sty(ph, "dim") + " " * (tw - cell_len(ph))
            else:
                seg = self.text[s:e].replace("\t", " ")
                body = seg + " " * (tw - cell_len(seg))
            lines.append(side_l + prompt + body + side_r)
        lines.append(edge("╰", "╯", f"↓ {below} mais" if below else ""))
        row_idx = 1 + (crow - self.view)
        s, _ = rows[crow]
        return lines, row_idx, 4 + cell_len(self.text[s:self.cur])

    def insert(self, s):
        self.text = self.text[:self.cur] + s + self.text[self.cur:]
        self.cur += len(s)
        self.hist_i = None

    def backspace(self):
        if self.cur:
            self.text = self.text[:self.cur - 1] + self.text[self.cur:]
            self.cur -= 1

    def delete(self):
        self.text = self.text[:self.cur] + self.text[self.cur + 1:]

    def line_bounds(self):
        s = self.text.rfind("\n", 0, self.cur) + 1
        e = self.text.find("\n", self.cur)
        return s, (len(self.text) if e < 0 else e)

    def move_vertical(self, d):
        rows = wrap_rows(self.text, self.tw)
        r = locate(rows, self.cur)
        col = cell_len(self.text[rows[r][0]:self.cur])
        nr = r + d
        if nr < 0:
            return self.history(-1)
        if nr >= len(rows):
            return self.history(+1)
        s, e = rows[nr]
        self.cur = index_at_col(self.text, s, e, col)

    def history(self, d):
        h = HISTORY
        if not h:
            return
        if self.hist_i is None:
            if d > 0:
                return
            self.draft, self.hist_i = self.text, len(h)
        ni = self.hist_i + d
        if ni < 0:
            return
        if ni >= len(h):
            self.hist_i, self.text = None, self.draft
        else:
            self.hist_i, self.text = ni, h[ni]
        self.cur = len(self.text)

    def handle(self, chunk, state):
        """'submit' ao Enter; levanta EOFError (Ctrl+D) / KeyboardInterrupt (Ctrl+C)."""
        if chunk == "\r":
            return "submit"
        if chunk == "\n":
            self.insert("\n"); return None
        chunk = chunk.replace("\r\n", "\n").replace("\r", "\n")
        for m in TOKEN.finditer(chunk):
            k = m.group(0)
            if k == "\x1b[200~": state["paste"] = True; continue
            if k == "\x1b[201~": state["paste"] = False; continue
            if k == "\n" or k in ("\x1b\n", "\x1b\r"):
                self.insert("\n"); continue
            if k.startswith("\x1b"):
                key = k[-1] if k[-1] != "~" else k[2:-1]
                s, e = self.line_bounds()
                if key == "A": self.move_vertical(-1)
                elif key == "B": self.move_vertical(+1)
                elif key == "C": self.cur = min(self.cur + 1, len(self.text))
                elif key == "D": self.cur = max(self.cur - 1, 0)
                elif key in ("H", "1"): self.cur = s
                elif key in ("F", "4"): self.cur = e
                elif key == "3": self.delete()
                continue
            o = ord(k)
            if k in ("\x7f", "\x08"): self.backspace()
            elif o == 3: raise KeyboardInterrupt
            elif o == 4:
                if not self.text: raise EOFError
                self.delete()
            elif o == 1: self.cur = self.line_bounds()[0]
            elif o == 5: self.cur = self.line_bounds()[1]
            elif o == 11:
                s, e = self.line_bounds(); self.text = self.text[:self.cur] + self.text[e:]
            elif o == 21:
                s, e = self.line_bounds(); self.text = self.text[:s] + self.text[self.cur:]; self.cur = s
            elif o == 23:
                i = self.cur
                while i > 0 and self.text[i - 1] in " \n": i -= 1
                while i > 0 and self.text[i - 1] not in " \n": i -= 1
                self.text = self.text[:i] + self.text[self.cur:]; self.cur = i
            elif k == "\t": self.insert("  ")
            elif o >= 32 and o != 127: self.insert(k)
        return None


# ═════════════════════════ leitura de teclas sem bloquear ═════════════════════════
# Estruturas da API de console do Windows (definidas sempre, para poder testar em qualquer SO)
class _COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class _KEY_EVENT(ctypes.Structure):
    _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD),
                ("wVirtualKeyCode", wintypes.WORD), ("wVirtualScanCode", wintypes.WORD),
                ("uChar", ctypes.c_wchar), ("dwControlKeyState", wintypes.DWORD)]


class _MOUSE_EVENT(ctypes.Structure):
    _fields_ = [("dwMousePosition", _COORD), ("dwButtonState", wintypes.DWORD),
                ("dwControlKeyState", wintypes.DWORD), ("dwEventFlags", wintypes.DWORD)]


class _EVENT_UNION(ctypes.Union):
    _fields_ = [("KeyEvent", _KEY_EVENT), ("MouseEvent", _MOUSE_EVENT), ("raw", wintypes.DWORD * 4)]


class _INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("Event", _EVENT_UNION)]


_VK_SEQ = {0x26: "A", 0x28: "B", 0x27: "C", 0x25: "D", 0x24: "H", 0x23: "F"}


def translate_win_records(records):
    """INPUT_RECORDs do console → texto com sequências ANSI (o mesmo formato do Unix)."""
    out = []
    for r in records:
        if r.EventType == 1:  # KEY_EVENT
            k = r.Event.KeyEvent
            if not k.bKeyDown:
                continue
            vk, ch, st = k.wVirtualKeyCode, k.uChar, k.dwControlKeyState
            ctrl, alt, shift = bool(st & 0x0C), bool(st & 0x03), bool(st & 0x10)
            seq = None
            if vk in _VK_SEQ:
                seq = f"\x1b[1;5{_VK_SEQ[vk]}" if (ctrl or alt) and vk in (0x26, 0x28) else f"\x1b[{_VK_SEQ[vk]}"
            elif vk == 0x2E: seq = "\x1b[3~"
            elif vk == 0x21: seq = "\x1b[5~"
            elif vk == 0x22: seq = "\x1b[6~"
            if seq:
                out.append(seq * max(k.wRepeatCount, 1) if vk in _VK_SEQ else seq)
            elif vk == 0x0D:  # Enter; Shift/Ctrl/Alt+Enter = nova linha
                out.append("\n" if (shift or ctrl or alt) else "\r")
            elif ch and ch != "\x00":
                out.append(ch)
        elif r.EventType == 2:  # MOUSE_EVENT
            m = r.Event.MouseEvent
            if m.dwEventFlags & 0x4:  # MOUSE_WHEELED
                delta = ctypes.c_short((m.dwButtonState >> 16) & 0xFFFF).value
                out.append("\x1b[<64;1;1M" if delta > 0 else "\x1b[<65;1;1M")
    s = "".join(out)
    try:  # junta pares substitutos (emojis chegam como duas teclas)
        s = s.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    except Exception:
        pass
    return s


if IS_WIN:
    _k32 = ctypes.windll.kernel32
    _k32.GetStdHandle.restype = wintypes.HANDLE

    class RawTerm:
        """Console do Windows: lê eventos de teclado/mouse direto (Ctrl+C vira tecla, não sinal)."""

        def __enter__(self):
            if ctypes.sizeof(_INPUT_RECORD) != 20:
                raise RuntimeError(f'INPUT_RECORD com tamanho inesperado: {ctypes.sizeof(_INPUT_RECORD)}')
            self.hin = _k32.GetStdHandle(-10)
            self.hout = _k32.GetStdHandle(-11)
            self.old_in, self.old_out = wintypes.DWORD(), wintypes.DWORD()
            _k32.GetConsoleMode(self.hin, ctypes.byref(self.old_in))
            _k32.GetConsoleMode(self.hout, ctypes.byref(self.old_out))
            _k32.SetConsoleMode(self.hout, self.old_out.value | 0x0001 | 0x0004)  # saída ANSI
            self.mouse = False
            self._apply_in()
            return self

        def _apply_in(self):
            mode = self.old_in.value | 0x0080                      # EXTENDED_FLAGS
            mode &= ~(0x0001 | 0x0002 | 0x0004)                    # sem PROCESSED/LINE/ECHO
            if self.mouse:
                mode = (mode | 0x0010) & ~0x0040                   # MOUSE_INPUT, sem QUICK_EDIT
            else:
                mode &= ~0x0010
            _k32.SetConsoleMode(self.hin, mode)

        def set_mouse(self, on):
            self.mouse = bool(on)
            self._apply_in()

        def __exit__(self, *a):
            _k32.SetConsoleMode(self.hin, self.old_in.value | 0x0080)
            _k32.SetConsoleMode(self.hout, self.old_out.value)

        def poll(self, timeout):
            end = time.time() + timeout
            text = ""
            while True:
                n = wintypes.DWORD(0)
                _k32.GetNumberOfConsoleInputEvents(self.hin, ctypes.byref(n))
                if n.value:
                    buf = (_INPUT_RECORD * min(n.value, 1024))()
                    got = wintypes.DWORD(0)
                    _k32.ReadConsoleInputW(self.hin, buf, len(buf), ctypes.byref(got))
                    text += translate_win_records(buf[:got.value])
                    continue          # esvazia tudo o que chegou (colar texto grande)
                if text:
                    return text
                if time.time() >= end:
                    return ""
                time.sleep(0.001)
else:
    import select
    import termios

    class RawTerm:
        def __enter__(self):
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            new = termios.tcgetattr(self.fd)
            new[0] &= ~(termios.ICRNL | termios.IXON | termios.INLCR)
            new[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN)
            new[6][termios.VMIN], new[6][termios.VTIME] = 1, 0
            termios.tcsetattr(self.fd, termios.TCSADRAIN, new)
            self.dec = codecs.getincrementaldecoder("utf-8")("replace")
            return self

        def set_mouse(self, on):
            REAL_OUT.write("\033[?1000h\033[?1006h" if on else "\033[?1006l\033[?1000l")
            REAL_OUT.flush()

        def __exit__(self, *a):
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

        def poll(self, timeout):
            if not select.select([self.fd], [], [], max(timeout, 0))[0]:
                return ""
            data = os.read(self.fd, 65536)
            if not data:
                raise EOFError
            s = self.dec.decode(data)
            while select.select([self.fd], [], [], 0.002)[0]:  # junta rajadas (colar)
                more = os.read(self.fd, 65536)
                if not more:
                    break
                s += self.dec.decode(more)
            return s


# ═════════════════════════ @arquivo ═════════════════════════
def expand_at_files(text):
    def repl(m):
        p = os.path.expanduser(m.group(1))
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    mq.ATTACH.append({"name": os.path.basename(p), "content": f.read()})
                console.print(Text(f"  📎 {os.path.basename(p)}", "dim"))
                return os.path.basename(p)
            except OSError:
                pass
        return m.group(0)
    return re.sub(r"@(\S+)", repl, text)


# ═════════════════════════ /ui ═════════════════════════
UI_HELP = """[bold]Customização — /ui[/bold]
  /ui                       mostra as configurações atuais
  /ui art <arquivo>         carrega uma arte ASCII de um arquivo (qualquer tamanho; ANSI colorido é aceito)
  /ui art paste             a PRÓXIMA mensagem enviada vira a arte (cole à vontade)
  /ui art demo              arte de exemplo
  /ui art on | off | clear  liga, desliga ou apaga a arte
  /ui pos <top-right|top-left|bottom-right|bottom-left>
  /ui color <#hex[,#hex..]|none>   cores (gradiente horizontal) da arte
  /ui width <n|0>           largura máxima da arte (0 = sem limite)
  /ui banner-color <#hex[,#hex..]>  gradiente do banner QUARK
  /ui anim <off|banner|all> anima o gradiente (só o banner, ou banner + arte)
  /ui border <#hex>         cor da caixa de entrada
  /ui banner on|off         mostra/esconde o banner QUARK
  /ui tips on|off           mostra/esconde as dicas
  /ui mouse on|off          roda do mouse rola a conversa (desliga a seleção com o mouse; use Shift)
  /ui preview               volta ao topo (cabeçalho)
  /ui reset                 volta ao padrão

A arte faz parte do cabeçalho: nunca cobre a caixa de entrada nem as respostas. Se não couber
ao lado do banner, é empilhada (e recortada na largura do terminal)."""

HEX = re.compile(r"#[0-9a-fA-F]{6}$")


def parse_colors(s):
    parts = [p.strip() for p in s.split(",") if p.strip()]
    return parts if parts and all(HEX.match(p) for p in parts) else None


def read_art_file(path):
    with open(os.path.expanduser(path.strip("\"'")), encoding="utf-8", errors="replace") as f:
        return f.read()


def handle_ui(arg):
    err = lambda m: console.print(f"[#e2685c]{m}[/]")
    parts = arg.split(None, 1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""
    try:
        if sub == "":
            art = UI["art"]
            n = art.count("\n") + 1 if art.strip() else 0
            console.print(f"  arte        {'ligada' if UI['art_on'] else 'desligada'} · {n} linhas · "
                          f"largura máx {UI['art_max_width'] or 'sem limite'}")
            console.print(f"  posição     {UI['pos']}")
            console.print(f"  cor da arte {','.join(UI['art_colors']) or 'original'}")
            console.print(f"  banner      {'on' if UI['banner'] else 'off'} · {','.join(UI['banner_colors'])}")
            console.print(f"  animação    {UI['anim']}")
            console.print(f"  dicas       {'on' if UI['tips'] else 'off'} · mouse {'on' if UI['mouse'] else 'off'}")
            console.print(f"  borda       {UI['border']}")
            console.print(f"[dim]  arquivo: {TUI_FILE} · /ui help para opções[/dim]")
            return
        if sub in ("help", "?"):
            console.print(UI_HELP); return
        if sub == "art":
            low = rest.lower()
            if low == "on": UI["art_on"] = True
            elif low == "off": UI["art_on"] = False
            elif low == "clear": UI["art"] = ""
            elif low == "demo": UI["art"], UI["art_on"] = DEMO_ART, True
            elif low == "paste":
                APP.mode = "art_paste"
                console.print("[dim]Cole a arte e envie com Enter (cancelar: /cancel).[/dim]"); return
            elif rest:
                UI["art"], UI["art_on"] = read_art_file(rest), True
            else:
                err("Uso: /ui art <arquivo> | paste | demo | on | off | clear"); return
        elif sub == "pos":
            if rest not in POSITIONS: err(f"Use: {' | '.join(POSITIONS)}"); return
            UI["pos"] = rest
        elif sub == "color":
            if rest.lower() == "none": UI["art_colors"] = []
            else:
                cs = parse_colors(rest)
                if not cs: err("Use cores hex: /ui color #ff0000,#0000ff  ou  none"); return
                UI["art_colors"] = cs
        elif sub == "banner-color":
            cs = parse_colors(rest)
            if not cs: err("Use cores hex: /ui banner-color #4e8fe8,#c9657f"); return
            UI["banner_colors"] = cs
        elif sub == "anim":
            if rest.lower() not in ("off", "banner", "all"): err("Uso: /ui anim off|banner|all"); return
            UI["anim"] = rest.lower()
        elif sub == "border":
            if not HEX.match(rest): err("Use uma cor hex: /ui border #5b8def"); return
            UI["border"] = rest
        elif sub == "width":
            if not rest.isdigit(): err("Uso: /ui width 60  (0 = sem limite)"); return
            UI["art_max_width"] = int(rest)
        elif sub in ("banner", "tips", "mouse"):
            if rest.lower() not in ("on", "off"): err(f"Uso: /ui {sub} on|off"); return
            UI[sub] = rest.lower() == "on"
            if sub == "mouse":
                APP.apply_mouse()
        elif sub == "preview":
            APP.stick, APP.top = False, 0
            return
        elif sub == "reset":
            UI.clear(); UI.update(ui_defaults()); APP.apply_mouse()
        else:
            err(f"Opção desconhecida: {sub}. Veja /ui help"); return
    except OSError as e:
        err(f"Não consegui ler o arquivo: {e}"); return
    save_ui()
    APP.invalidate()
    APP.stick, APP.top = False, 0
    console.print("[dim]ok — veja o topo (/ui preview).[/dim]")


# ═════════════════════════ aplicativo (loop de 60 fps) ═════════════════════════
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
CRASH_LOG = os.path.join(mq.HOME, "crash.log")


def log_crash(where):
    """Grava o traceback em ~/.mini_quark/crash.log (a tela cheia esconde o stderr)."""
    try:
        os.makedirs(mq.HOME, exist_ok=True)
        with open(CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} · {where} · {sys.platform} py{sys.version.split()[0]}\n")
            f.write(traceback.format_exc())
    except Exception:
        pass



class App:
    def __init__(self):
        self.ed = Editor()
        self.state = {"paste": False}
        self.log = Log()
        self.hdr_cache = {}
        self.ver = 0
        self.stick, self.top = False, 0
        self.busy, self.worker = False, None
        self.confirm = None
        self.mode = None
        self.prev, self.prev_size = [], None
        self.quit = False
        self.ctrlc_t = 0.0
        self.notice, self.notice_t = "", 0.0
        self.term = None
        self.frame_no = 0
        self.errors = 0

    # ---- utilidades ----
    def invalidate(self):
        self.ver += 1
        self.hdr_cache.clear()

    def say(self, msg, secs=2.5):
        self.notice, self.notice_t = msg, time.time() + secs

    def apply_mouse(self):
        if self.term:
            self.term.set_mouse(UI["mouse"])

    def header(self, cols, now):
        anim = UI["anim"]
        pb = pa = None
        bucket = 0
        if anim != "off":
            ph = (now / 8.0) % 1.0
            bucket = int(ph * 96)
            ph = bucket / 96.0
            pb = ph
            pa = ph if anim == "all" else None
        key = (cols, self.ver, bucket)
        rows = self.hdr_cache.get(key)
        if rows is None:
            if len(self.hdr_cache) > 120:
                self.hdr_cache.clear()
            rows = []
            for t in compose_header(cols, pb, pa):
                t.truncate(cols, overflow="crop")
                rows.append(render_ansi(t) + RESET)
            self.hdr_cache[key] = rows
        return rows

    # ---- renderização ----
    def render(self):
        cols, rows = term_size()
        out = []
        if (cols, rows) != self.prev_size:
            self.prev, self.prev_size = [], (cols, rows)
            out.append("\033[2J")
        self.frame_no += 1
        if self.frame_no % 120 == 0:   # a cada ~2 s reescreve tudo: cura qualquer dessincronia
            self.prev = []
        if cols < 30 or rows < 8:
            msg = f"Terminal pequeno ({cols}x{rows}); precisa de 30x8"
            if self.prev != [msg]:
                REAL_OUT.write("\033[2J\033[H" + msg[:max(cols - 1, 1)])
                REAL_OUT.flush()
                self.prev = [msg]
            return
        now = time.time()
        w = max(min(cols - 1, 140), 24)
        max_text_rows = max(1, min(rows // 2, rows - 7))
        box, row_idx, ccol = self.ed.frame(w, max_text_rows)
        avail = rows - len(box) - 2

        hdr = self.header(cols - 1, now)
        self.log.sync(cols - 1)
        nlog = self.log.count()
        nh = len(hdr)
        total = nh + (1 + nlog if nlog else 0)
        maxtop = max(total - avail, 0)
        top = maxtop if self.stick else min(self.top, maxtop)
        self.top = top
        if not self.stick and top >= maxtop and not self.log.empty:
            self.stick = True  # chegou ao fim: volta a acompanhar a conversa

        def get(i):
            if i < nh:
                return hdr[i]
            if i == nh:
                return ""
            return self.log.get(i - nh - 1)

        content = [get(i) for i in range(top, min(top + avail, total))]
        content += [""] * (avail - len(content))

        # linha de status
        if self.confirm:
            status = sty("Permitir? ", "bold #e8a94c") + sty("[s]im  [N]ão  [a]sempre nesta sessão", "#e8a94c")
        elif self.busy:
            sp = SPIN[int(now * 12) % len(SPIN)]
            status = sty(f"{sp} pensando…", "#7fb2ff") + sty("  (Ctrl+C interrompe)", "dim")
        elif self.notice and now < self.notice_t:
            status = sty(self.notice, "#e8a94c")
        elif self.mode == "art_paste":
            status = sty("modo arte: cole a arte e envie · /cancel cancela", "#e8a94c")
        elif not self.stick and total > avail and top < maxtop:
            status = sty(f"↑ rolando ({maxtop - top} linhas abaixo) · PgDn volta ao fim", "dim")
        else:
            status = ""

        frame = content + [status] + box + [footer_ansi(w)]
        cur_line = avail + 1 + row_idx  # 0-based
        for i, line in enumerate(frame[:rows]):
            if i >= len(self.prev) or self.prev[i] != line:
                out.append(f"\033[{i + 1};1H{line}{RESET}\033[K")
        self.prev = frame[:rows]
        if out:
            REAL_OUT.write("\033[?2026h\033[?25l" + "".join(out) +
                           f"\033[{cur_line + 1};{ccol + 1}H\033[?25h\033[?2026l")
            REAL_OUT.flush()
        else:
            # nada mudou, mas mantém o cursor no lugar certo (barato)
            REAL_OUT.write(f"\033[{cur_line + 1};{ccol + 1}H")
            REAL_OUT.flush()

    # ---- entrada ----
    def page(self):
        return max(term_size()[1] // 2, 3)

    def scroll(self, delta):
        """delta < 0 sobe, > 0 desce. self.top já guarda a posição efetiva do último frame."""
        self.stick = False
        self.top = max(self.top + delta, 0)

    def cls(self, arg=""):
        self.log.clear()
        self.stick, self.top = False, 0
        if arg.strip().lower() in ("all", "tudo", "history", "historico", "histórico"):
            HISTORY.clear()
            self.say("tela e histórico de comandos (↑) limpos")
        else:
            self.say("tela limpa")

    def interrupt(self):
        if self.confirm:
            self.confirm["ans"] = False
            self.confirm["ev"].set()
        if self.worker and self.worker.is_alive():
            ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_long(self.worker.ident), ctypes.py_object(KeyboardInterrupt))

    def on_ctrl_c(self):
        if self.busy:
            self.interrupt()
        elif self.ed.text:
            self.ed.text, self.ed.cur = "", 0
        elif self.mode:
            self.mode = None
        elif time.time() - self.ctrlc_t < 2:
            self.quit = True
        else:
            self.ctrlc_t = time.time()
            self.say("Ctrl+C de novo (ou Ctrl+D) para sair")

    def on_chunk(self, chunk):
        if self.confirm:
            ans = chunk.strip().lower()[:1]
            if ans == "\x03":
                ans = "n"
            if ans in ("a",):
                mq.AUTO = True
            self.confirm["ans"] = ans in ("s", "y", "a")
            self.confirm["ev"].set()
            return
        if chunk == "\x1b" and self.busy:
            self.interrupt(); return
        rest = []
        for t in TOKEN.findall(chunk):
            if t == "\x1b[5~": self.scroll(-self.page())
            elif t == "\x1b[6~": self.scroll(+self.page())
            elif t.startswith("\x1b[<"):
                m = re.match(r"\x1b\[<(\d+);\d+;\d+([Mm])", t)
                if m and m.group(2) == "M" and int(m.group(1)) & 64:   # roda (com ou sem Shift/Ctrl)
                    self.scroll(+3 if int(m.group(1)) & 1 else -3)
            elif re.fullmatch(r"\x1b\[1;[2-8][AB]", t):             # Ctrl/Alt/Shift + ↑↓
                self.scroll(-1 if t.endswith("A") else +1)
            elif t == "\x0c": self.cls()
            else: rest.append(t)
        chunk = "".join(rest)
        if not chunk:
            return
        if self.state["paste"]:
            chunk = chunk.replace("\r", "\n")
        try:
            res = self.ed.handle(chunk, self.state)
        except KeyboardInterrupt:
            self.on_ctrl_c(); return
        except EOFError:
            self.quit = True; return
        if res == "submit":
            text = self.ed.text.strip()
            self.ed.text, self.ed.cur, self.ed.view, self.ed.hist_i = "", 0, 0, None
            if text:
                self.submit(text)

    # ---- envio ----
    def submit(self, text):
        self.stick = True
        if self.mode == "art_paste":
            self.mode = None
            if text.lower() == "/cancel":
                self.say("cancelado"); return
            UI["art"], UI["art_on"] = text, True
            save_ui(); self.invalidate()
            self.stick, self.top = False, 0
            self.say("arte aplicada — veja o topo")
            return
        if text.lower() == "/cancel":
            return
        if not text.startswith("/key ") and (not HISTORY or HISTORY[-1] != text):
            HISTORY.append(text)

        shown = "/key ********" if text.startswith("/key ") else text
        lines = shown.split("\n")
        if len(lines) > 12:
            shown = "\n".join(lines[:12]) + f"\n… (+{len(lines) - 12} linhas)"
        print(mq.bold(mq.blue("> ")) + shown + "\n")

        if text.startswith("/"):
            cmd, _, arg = text[1:].partition(" ")
            cmd = cmd.lower()
            if cmd in ("cls", "limpar"):
                self.cls(arg); return
            if cmd == "ui":
                handle_ui(arg.strip()); print(); return
            if self.busy:
                print(mq.amber("Aguarde a resposta terminar (Ctrl+C interrompe).\n")); return
            if cmd == "key" and not arg.strip():
                print(mq.amber("Aqui use: /key <sua-chave>   (ou exporte OPENROUTER_API_KEY)\n")); return
            try:
                mq.handle_command(text)
                if cmd in ("help", "h", "?"):
                    print("\n" + mq.bold("Interface"))
                    print("  /cls [all]               limpa a tela (all = e o histórico do ↑)")
                    print("  /ui                      customização (arte ASCII, cores, animação...)")
                    print("  PgUp/PgDn                rola a conversa · Ctrl+L = /cls")
            except EOFError:
                self.quit = True
            except Exception as e:
                print(mq.red(f"Erro: {e}"))
            print()
            return

        if self.busy:
            print(mq.amber("Aguarde a resposta terminar (Ctrl+C interrompe).\n")); return
        if not mq.api_key():
            print(mq.red("Configure a API key: /key <chave>\n")); return

        full = mq.build_user_text(expand_at_files(text))

        def target():
            try:
                mq.chat_turn(full)
            except BaseException as e:  # inclui KeyboardInterrupt injetado
                print(mq.red(f"\n[interrompido: {e.__class__.__name__}]"))
            finally:
                self.busy = False
                print()

        self.busy = True
        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()

    # ---- confirmação de ferramentas (chamada pela thread do modelo) ----
    def ask_confirm(self, kind, detail):
        if mq.AUTO or mq.cfg("auto"):
            return True
        print(mq.amber(f"\n⚠ O modelo quer: {kind}"))
        ls = str(detail).splitlines()
        for line in ls[:25]:
            print("   " + mq.dim(line))
        if len(ls) > 25:
            print("   " + mq.dim("..."))
        c = {"ev": threading.Event(), "ans": False}
        self.confirm = c
        self.stick = True
        c["ev"].wait()
        self.confirm = None
        print(mq.green("   → permitido") if c["ans"] else mq.red("   → negado"))
        return c["ans"]

    # ---- ciclo principal ----
    def run(self):
        REAL_OUT.write("\033[?1049h\033[?2004h\033[2J\033[H")
        REAL_OUT.flush()
        if not mq.api_key():
            print(mq.amber("Sem API key — use /key <chave> ou exporte OPENROUTER_API_KEY."))
        with RawTerm() as term:
            self.term = term
            self.apply_mouse()
            nxt = time.perf_counter()
            while not self.quit:
                try:
                    self.render()
                    self.errors = 0
                except Exception:
                    self.errors += 1
                    if self.errors == 1:
                        log_crash("render")
                    self.prev, self.prev_size = [], None   # força repintura completa
                    self.say("erro ao desenhar (veja crash.log)", 5)
                    if self.errors > 30:
                        raise
                nxt += FRAME
                delay = nxt - time.perf_counter()
                if delay < -0.25:   # ficou muito para trás: não tenta compensar
                    nxt = time.perf_counter(); delay = 0
                try:
                    chunk = term.poll(delay)
                except EOFError:
                    break
                except KeyboardInterrupt:   # Ctrl+C que escapou (ex.: sinal) vira a tecla normal
                    chunk = "\x03"
                if chunk:
                    try:
                        self.on_chunk(chunk)
                    except Exception:
                        log_crash("input")
                        self.say("erro ao processar tecla (veja crash.log)", 5)
        self.interrupt()


def restore_terminal():
    try:
        REAL_OUT.write("\033[?1006l\033[?1000l\033[?2004l\033[?25h\033[?2026l\033[?1049l")
        REAL_OUT.flush()
    except Exception:
        pass


def main():
    global APP
    ap = argparse.ArgumentParser()
    ap.add_argument("-y", "--yes", action="store_true")
    ap.add_argument("-m", "--model")
    ap.add_argument("--art", help="arquivo com arte ASCII (só nesta sessão)")
    args = ap.parse_args()

    if not (sys.stdin.isatty() and REAL_OUT.isatty()):
        sys.argv = [sys.argv[0]]
        return mq.main()  # sem terminal de verdade: cai no CLI simples

    mq.load_state()
    load_ui()
    if args.model:
        mq.S["config"]["model"] = args.model
    if args.yes:
        mq.AUTO = True
    if args.art:
        try:
            UI["art"], UI["art_on"] = read_art_file(args.art), True
        except OSError as e:
            sys.exit(f"Não consegui ler {args.art}: {e}")

    APP = App()
    mq.confirm = APP.ask_confirm
    proxy = LogOut(APP.log)
    sys.stdout = sys.stderr = proxy
    atexit.register(restore_terminal)

    def _stop(*_):
        APP.quit = True
    for sig in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, sig):
            try:
                signal.signal(getattr(signal, sig), _stop)
            except (ValueError, OSError):
                pass
    crashed = False
    try:
        APP.run()
    except BaseException:
        crashed = True
        log_crash("main")
    finally:
        sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
        restore_terminal()
    if crashed:
        print(f"A interface travou. Detalhes em: {CRASH_LOG}")
        print(open(CRASH_LOG, encoding="utf-8").read().split("=== ")[-1])
        sys.exit(1)
    print("Até mais.")


if __name__ == "__main__":
    main()