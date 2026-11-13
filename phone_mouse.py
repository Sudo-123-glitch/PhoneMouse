#!/usr/bin/env python3
"""
PhoneMouse - use your phone (iPhone or Android) as a laser-pointer style air mouse +
keyboard for a Windows PC or a Mac.

HOW TO RUN:
  1. Install Python 3 from https://www.python.org (on Windows tick "Add python.exe to PATH").
  2. Run:   python phone_mouse.py      (on a Mac:   python3 phone_mouse.py)
  3. If Windows Firewall asks, click "Allow" (private networks).
     On a Mac, allow your terminal app under System Settings > Privacy & Security > Accessibility.
  4. On your phone (same Wi-Fi), open Safari or Chrome and type the https:// address shown.
     The browser will warn "This connection is not private" - that's expected for a
     home-made certificate: in Safari tap "Show Details" -> "visit this website" -> "Visit Website",
     in Chrome tap "Advanced" -> "Proceed".
  5. Enter the PIN shown in the console, tap Start, and allow Motion access.

Needs one package (first run only):   python -m pip install cryptography
Options:
  python phone_mouse.py --port 8443      use a different port
  python phone_mouse.py --new-pin        make a new PIN
  python phone_mouse.py --dry-run        print actions instead of moving the mouse
"""

import argparse
import datetime
import ipaddress
import json
import os
import secrets
import socket
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CERT_FILE = os.path.join(HERE, "phonemouse_cert.pem")
KEY_FILE = os.path.join(HERE, "phonemouse_key.pem")
PIN_FILE = os.path.join(HERE, "phonemouse_pin.txt")


# =============================================================================
#  Windows mouse / keyboard control (pure ctypes, no packages)
# =============================================================================

VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "shift": 0x10, "ctrl": 0x11,
    "alt": 0x12, "esc": 0x1B, "space": 0x20, "pageup": 0x21, "pagedown": 0x22,
    "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27,
    "down": 0x28, "delete": 0x2E, "win": 0x5B,
    "f5": 0x74, "f11": 0x7A,
    "mute": 0xAD, "voldown": 0xAE, "volup": 0xAF, "next": 0xB0, "prev": 0xB1,
    "playpause": 0xB3,
}
for _c in "abcdefghijklmnopqrstuvwxyz":
    VK[_c] = ord(_c.upper())
for _d in "0123456789":
    VK[_d] = ord(_d)

# Keys that need the "extended" flag so Windows doesn't treat them as numpad keys.
EXTENDED = {"pageup", "pagedown", "end", "home", "left", "up", "right", "down",
            "delete", "win", "mute", "voldown", "volup", "next", "prev", "playpause"}


class WindowsInput:
    MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
    MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
    MOUSEEVENTF_WHEEL = 0x0800
    KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x1, 0x2, 0x4

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.wintypes = ctypes, wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)

        # Make cursor coordinates match real pixels on high-DPI screens.
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                self.user32.SetProcessDPIAware()
            except Exception:
                pass

        ULONG_PTR = ctypes.c_size_t

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ULONG_PTR)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                        ("wParamH", wintypes.WORD)]

        class _U(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]

        self.MOUSEINPUT, self.KEYBDINPUT, self.INPUT = MOUSEINPUT, KEYBDINPUT, INPUT
        self.user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        self.user32.SendInput.restype = wintypes.UINT
        self.fx = 0.0  # sub-pixel remainders so slow, precise movement still works
        self.fy = 0.0

    def _send(self, inputs):
        arr = (self.INPUT * len(inputs))(*inputs)
        self.user32.SendInput(len(inputs), arr, self.ctypes.sizeof(self.INPUT))

    def _mouse(self, flags, data=0):
        inp = self.INPUT(type=0)
        inp.u.mi = self.MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flags, 0, 0)
        return inp

    def _key(self, vk=0, scan=0, flags=0):
        inp = self.INPUT(type=1)
        inp.u.ki = self.KEYBDINPUT(vk, scan, flags, 0, 0)
        return inp

    # --- mouse ---
    def move(self, dx, dy):
        self.fx += dx
        self.fy += dy
        ix, iy = int(self.fx), int(self.fy)
        self.fx -= ix
        self.fy -= iy
        if not ix and not iy:
            return
        pt = self.wintypes.POINT()
        self.user32.GetCursorPos(self.ctypes.byref(pt))
        self.user32.SetCursorPos(pt.x + ix, pt.y + iy)

    def move_abs(self, nx, ny):
        """Laser mode: nx/ny are -1..1 from the centre of the main screen (ny uses the
        same scale as nx, so 1 degree is the same distance both ways)."""
        gsm = self.user32.GetSystemMetrics
        w, h = gsm(0), gsm(1)
        vx, vy, vw, vh = gsm(76), gsm(77), gsm(78), gsm(79)
        x = int(round(w / 2 + nx * w / 2))
        y = int(round(h / 2 + ny * w / 2))
        x = max(vx, min(vx + vw - 1, x))
        y = max(vy, min(vy + vh - 1, y))
        self.fx = self.fy = 0.0
        self.user32.SetCursorPos(x, y)

    def button(self, which, down):
        if which == "l":
            f = self.MOUSEEVENTF_LEFTDOWN if down else self.MOUSEEVENTF_LEFTUP
        else:
            f = self.MOUSEEVENTF_RIGHTDOWN if down else self.MOUSEEVENTF_RIGHTUP
        self._send([self._mouse(f)])

    def click(self, which):
        self.button(which, True)
        self.button(which, False)

    def wheel(self, amount):
        self._send([self._mouse(self.MOUSEEVENTF_WHEEL, int(amount))])

    # --- keyboard ---
    def key_down(self, name):
        flags = self.KEYEVENTF_EXTENDEDKEY if name in EXTENDED else 0
        self._send([self._key(vk=VK[name], flags=flags)])

    def key_up(self, name):
        flags = self.KEYEVENTF_KEYUP | (self.KEYEVENTF_EXTENDEDKEY if name in EXTENDED else 0)
        self._send([self._key(vk=VK[name], flags=flags)])

    def text(self, s):
        for ch in s:
            if ch == "\n":
                self.tap("enter")
                continue
            if ch == "\t":
                self.tap("tab")
                continue
            data = ch.encode("utf-16-le")
            units = [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]
            ins = []
            for u in units:
                ins.append(self._key(scan=u, flags=self.KEYEVENTF_UNICODE))
                ins.append(self._key(scan=u, flags=self.KEYEVENTF_UNICODE | self.KEYEVENTF_KEYUP))
            self._send(ins)

    def tap(self, name):
        self.key_down(name)
        self.key_up(name)

    def combo(self, keys):
        for k in keys:
            self.key_down(k)
        for k in reversed(keys):
            self.key_up(k)


# =============================================================================
#  macOS mouse / keyboard control (pure ctypes, no packages)
# =============================================================================

MAC_KEYCODE = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19,
    "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26, "8": 28, "0": 29, "o": 31,
    "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46,
    "enter": 36, "tab": 48, "space": 49, "backspace": 51, "esc": 53,
    "cmd": 55, "shift": 56, "alt": 58, "ctrl": 59,
    "f5": 96, "f11": 103, "home": 115, "pageup": 116, "delete": 117, "end": 119,
    "pagedown": 121, "left": 123, "right": 124, "down": 125, "up": 126,
}
MAC_FLAG = {"shift": 0x20000, "ctrl": 0x40000, "alt": 0x80000, "cmd": 0x100000}
MAC_MEDIA = {"volup": 0, "voldown": 1, "mute": 7, "playpause": 16, "next": 17, "prev": 18}

# The phone sends Windows shortcuts. These are the Mac shortcuts that do the same job.
MAC_SHORTCUT = {
    ("win",): ["cmd", "space"],      # Start menu -> Spotlight
    ("win", "d"): ["f11"],           # Show desktop
    ("alt", "tab"): ["cmd", "tab"],  # Switch app
    ("f5",): ["cmd", "r"],           # Reload
}
MAC_WHEEL_TO_PIXELS = 0.8   # a Windows wheel notch is 120 units, about 96 pixels of scrolling
MAC_DOUBLE_CLICK_SECONDS = 0.5
MAC_CURSOR_TRUST_SECONDS = 0.2
MAC_DOUBLE_CLICK_PIXELS = 5


def mac_keys(keys):
    """Translate the key names the phone sends into the keys a Mac needs for the same action."""
    special = MAC_SHORTCUT.get(tuple(keys))
    if special:
        return list(special)
    return ["cmd" if k in ("ctrl", "win") else k for k in keys]


class MacEvents:
    """The macOS calls that read the cursor and post mouse and keyboard events."""
    MOUSE = {"move": (5, 0), "ldown": (1, 0), "lup": (2, 0), "ldrag": (6, 0),
             "rdown": (3, 1), "rup": (4, 1), "rdrag": (7, 1)}

    def __init__(self):
        import ctypes as c

        class CGPoint(c.Structure):
            _fields_ = [("x", c.c_double), ("y", c.c_double)]

        class CGRect(c.Structure):
            _fields_ = [("x", c.c_double), ("y", c.c_double), ("w", c.c_double), ("h", c.c_double)]

        self.c, self.CGPoint = c, CGPoint
        cg = self.cg = c.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        ptr = c.c_void_p
        for name, restype, argtypes in [
            ("CGEventCreate", ptr, [ptr]),
            ("CGEventGetLocation", CGPoint, [ptr]),
            ("CFRelease", None, [ptr]),
            ("CGEventCreateMouseEvent", ptr, [ptr, c.c_uint32, CGPoint, c.c_uint32]),
            ("CGEventSetIntegerValueField", None, [ptr, c.c_uint32, c.c_int64]),
            ("CGEventPost", None, [c.c_uint32, ptr]),
            ("CGEventCreateScrollWheelEvent2", ptr, [ptr, c.c_uint32, c.c_uint32, c.c_int32, c.c_int32, c.c_int32]),
            ("CGEventCreateKeyboardEvent", ptr, [ptr, c.c_uint16, c.c_bool]),
            ("CGEventSetFlags", None, [ptr, c.c_uint64]),
            ("CGEventKeyboardSetUnicodeString", None, [ptr, c.c_ulong, c.POINTER(c.c_uint16)]),
            ("CGMainDisplayID", c.c_uint32, []),
            ("CGDisplayBounds", CGRect, [c.c_uint32]),
            ("CGGetActiveDisplayList", c.c_int32, [c.c_uint32, c.POINTER(c.c_uint32), c.POINTER(c.c_uint32)]),
            ("CGPreflightPostEventAccess", c.c_bool, []),
            ("CGRequestPostEventAccess", c.c_bool, []),
        ]:
            fn = getattr(cg, name)
            fn.restype, fn.argtypes = restype, argtypes

        # Volume and playback keys only exist as AppKit "system defined" events.
        c.CDLL("/System/Library/Frameworks/AppKit.framework/AppKit")
        objc = self.objc = c.CDLL("/usr/lib/libobjc.A.dylib")
        objc.objc_getClass.restype, objc.objc_getClass.argtypes = ptr, [c.c_char_p]
        objc.sel_registerName.restype, objc.sel_registerName.argtypes = ptr, [c.c_char_p]
        objc.objc_autoreleasePoolPush.restype = ptr
        objc.objc_autoreleasePoolPop.argtypes = [ptr]
        self.NSEvent = objc.objc_getClass(b"NSEvent")
        self.sel_make = objc.sel_registerName(
            b"otherEventWithType:location:modifierFlags:timestamp:windowNumber:context:subtype:data1:data2:")
        self.sel_cgevent = objc.sel_registerName(b"CGEvent")
        self.make_event = c.CFUNCTYPE(ptr, ptr, ptr, c.c_ulong, CGPoint, c.c_ulong, c.c_double, c.c_long,
                                      ptr, c.c_short, c.c_long, c.c_long)(("objc_msgSend", objc))
        self.get_cgevent = c.CFUNCTYPE(ptr, ptr, ptr)(("objc_msgSend", objc))

    def allowed(self):
        return bool(self.cg.CGPreflightPostEventAccess())

    def ask_permission(self):
        self.cg.CGRequestPostEventAccess()

    def _post(self, ev):
        self.cg.CGEventPost(0, ev)
        self.cg.CFRelease(ev)

    def cursor(self):
        ev = self.cg.CGEventCreate(None)
        pt = self.cg.CGEventGetLocation(ev)
        self.cg.CFRelease(ev)
        return (pt.x, pt.y)

    def main_display(self):
        r = self.cg.CGDisplayBounds(self.cg.CGMainDisplayID())
        return (r.x, r.y, r.w, r.h)

    def desktop(self):
        """The rectangle that covers every connected screen."""
        ids = (self.c.c_uint32 * 16)()
        n = self.c.c_uint32(0)
        self.cg.CGGetActiveDisplayList(16, ids, self.c.byref(n))
        rects = [self.cg.CGDisplayBounds(ids[i]) for i in range(n.value)]
        if not rects:
            return self.main_display()
        x0, y0 = min(r.x for r in rects), min(r.y for r in rects)
        x1, y1 = max(r.x + r.w for r in rects), max(r.y + r.h for r in rects)
        return (x0, y0, x1 - x0, y1 - y0)

    def post_mouse(self, kind, x, y, clicks=1):
        etype, button = self.MOUSE[kind]
        ev = self.cg.CGEventCreateMouseEvent(None, etype, self.CGPoint(x, y), button)
        self.cg.CGEventSetIntegerValueField(ev, 1, clicks)   # 1 = click count field
        self._post(ev)

    def post_scroll(self, pixels):
        self._post(self.cg.CGEventCreateScrollWheelEvent2(None, 0, 1, pixels, 0, 0))   # 0 = pixel units

    def post_key(self, code, down, flags):
        ev = self.cg.CGEventCreateKeyboardEvent(None, code, down)
        self.cg.CGEventSetFlags(ev, flags)
        self._post(ev)

    def post_text(self, ch):
        data = ch.encode("utf-16-le")
        units = [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]
        arr = (self.c.c_uint16 * len(units))(*units)
        for down in (True, False):
            ev = self.cg.CGEventCreateKeyboardEvent(None, 0, down)
            self.cg.CGEventSetFlags(ev, 0)
            self.cg.CGEventKeyboardSetUnicodeString(ev, len(units), arr)
            self._post(ev)

    def post_media(self, name):
        pool = self.objc.objc_autoreleasePoolPush()
        try:
            for state in (0xA, 0xB):   # key down, key up
                ev = self.make_event(self.NSEvent, self.sel_make, 14, self.CGPoint(0, 0), state << 8,
                                     0.0, 0, None, 8, (MAC_MEDIA[name] << 16) | (state << 8), -1)
                self.cg.CGEventPost(0, self.get_cgevent(ev, self.sel_cgevent))
        finally:
            self.objc.objc_autoreleasePoolPop(pool)


class MacInput:
    """Turns phone actions into macOS events. `events` does the posting, `clock` returns seconds."""

    def __init__(self, events, clock=time.monotonic):
        self.os, self.clock = events, clock
        self.pos = None        # where PhoneMouse last put the cursor, and when
        self.pos_time = 0.0
        self.held = None       # the button being held, so movement becomes a drag
        self.wheel_acc = 0.0
        self.last_click = None  # (button, time, x, y, count) of the latest press

    # --- mouse ---
    def _where(self):
        """The cursor position. macOS applies posted moves a moment later, so during a
        stream of moves the position PhoneMouse set is more current than the one macOS
        reports. After a pause the real cursor wins, in case a real mouse moved it."""
        if self.pos is None or self.clock() - self.pos_time > MAC_CURSOR_TRUST_SECONDS:
            self.pos = self.os.cursor()
        return self.pos

    def _goto(self, x, y):
        dx, dy, dw, dh = self.os.desktop()
        x = max(dx, min(dx + dw - 1, x))
        y = max(dy, min(dy + dh - 1, y))
        self.pos, self.pos_time = (x, y), self.clock()
        self.os.post_mouse(self.held + "drag" if self.held else "move", x, y)

    def move(self, dx, dy):
        # Positions are fractional: a Retina screen has two pixels per point.
        x, y = self._where()
        self._goto(x + dx, y + dy)

    def move_abs(self, nx, ny):
        """Laser mode: nx/ny are -1..1 from the centre of the main screen (ny uses the
        same scale as nx, so 1 degree is the same distance both ways)."""
        mx, my, w, h = self.os.main_display()
        self._goto(round(mx + w / 2 + nx * w / 2), round(my + h / 2 + ny * w / 2))

    def button(self, which, down):
        x, y = self._where()
        if down:
            count, now, last = 1, self.clock(), self.last_click
            if (last and last[0] == which and now - last[1] <= MAC_DOUBLE_CLICK_SECONDS
                    and abs(x - last[2]) <= MAC_DOUBLE_CLICK_PIXELS
                    and abs(y - last[3]) <= MAC_DOUBLE_CLICK_PIXELS):
                count = last[4] + 1
            self.last_click = (which, now, x, y, count)
            self.held = which
        else:
            count = self.last_click[4] if self.last_click and self.last_click[0] == which else 1
            if self.held == which:
                self.held = None
        self.os.post_mouse(which + ("down" if down else "up"), x, y, count)

    def click(self, which):
        self.button(which, True)
        self.button(which, False)

    def wheel(self, amount):
        self.wheel_acc += amount
        pixels = int(self.wheel_acc * MAC_WHEEL_TO_PIXELS)
        if pixels:
            self.wheel_acc -= pixels / MAC_WHEEL_TO_PIXELS
            self.os.post_scroll(pixels)

    # --- keyboard ---
    def combo(self, keys):
        keys = mac_keys(keys)
        if len(keys) == 1 and keys[0] in MAC_MEDIA:
            self.os.post_media(keys[0])
            return
        flags = 0
        for k in keys:
            flags |= MAC_FLAG.get(k, 0)
            self.os.post_key(MAC_KEYCODE[k], True, flags)
        for k in reversed(keys):
            flags &= ~MAC_FLAG.get(k, 0)
            self.os.post_key(MAC_KEYCODE[k], False, flags)

    def tap(self, name):
        self.combo([name])

    def text(self, s):
        for ch in s:
            if ch == "\n":
                self.tap("enter")
            elif ch == "\t":
                self.tap("tab")
            else:
                self.os.post_text(ch)


class DryRunInput:
    """Prints what would happen. Used with --dry-run or on systems that are not supported."""
    def __init__(self):
        self.acc = [0.0, 0.0]
        self.last = time.time()

    def move(self, dx, dy):
        self.acc[0] += dx
        self.acc[1] += dy
        if time.time() - self.last > 0.5:
            print("  move  dx=%+.0f dy=%+.0f" % tuple(self.acc))
            self.acc = [0.0, 0.0]
            self.last = time.time()

    def move_abs(self, nx, ny):
        if time.time() - self.last > 0.5:
            print("  laser x=%+.2f y=%+.2f" % (nx, ny))
            self.last = time.time()

    def button(self, which, down): print("  mouse %s %s" % (which, "down" if down else "up"))
    def click(self, which): print("  click %s" % which)
    def wheel(self, amount): print("  wheel %d" % amount)
    def key_down(self, name): print("  key down %s" % name)
    def key_up(self, name): print("  key up %s" % name)
    def text(self, s): print("  type %r" % s)
    def tap(self, name): print("  key %s" % name)

    def combo(self, keys):
        for k in keys:
            self.key_down(k)
        for k in reversed(keys):
            self.key_up(k)


# =============================================================================
#  Applying events coming from the phone
# =============================================================================

INPUT_LOCK = threading.Lock()


def apply_message(dev, msg):
    dx = float(msg.get("dx") or 0)
    dy = float(msg.get("dy") or 0)
    dx = max(-3000.0, min(3000.0, dx))
    dy = max(-3000.0, min(3000.0, dy))
    ab = msg.get("abs")
    with INPUT_LOCK:
        if isinstance(ab, list) and len(ab) == 2:
            nx = max(-3.0, min(3.0, float(ab[0])))
            ny = max(-3.0, min(3.0, float(ab[1])))
            dev.move_abs(nx, ny)
        if dx or dy:
            dev.move(dx, dy)
        for ev in msg.get("ev") or []:
            if not isinstance(ev, list) or not ev:
                continue
            kind = ev[0]
            arg = ev[1] if len(ev) > 1 else None
            if kind == "ld":
                dev.button("l", True)
            elif kind == "lu":
                dev.button("l", False)
            elif kind == "rd":
                dev.button("r", True)
            elif kind == "ru":
                dev.button("r", False)
            elif kind == "click":
                dev.click("r" if arg == "r" else "l")
            elif kind == "wheel":
                dev.wheel(max(-2400, min(2400, int(arg or 0))))
            elif kind == "text" and isinstance(arg, str):
                dev.text(arg[:500])
            elif kind == "bs":
                for _ in range(max(0, min(500, int(arg or 1)))):
                    dev.tap("backspace")
            elif kind == "key" and arg in VK:
                dev.tap(arg)
            elif kind == "combo" and isinstance(arg, list) and arg and all(k in VK for k in arg):
                dev.combo(arg)
            # "ping" and unknown kinds are ignored


# =============================================================================
#  Web server (HTTPS, so phone browsers allow motion sensors)
# =============================================================================

class State:
    host = "windows"
    pin = ""
    dev = None
    seen = set()
    fails = {}  # ip -> (count, locked_until)
    lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # keep-alive: one connection reused for every event
    server_version = "PhoneMouse"
    timeout = 60

    def setup(self):
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def log_message(self, *args):
        pass

    def _reply(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] in ("/", "/index.html"):
            self._reply(200, page_html(State.host).encode("utf-8"), "text/html; charset=utf-8")
        else:
            self._reply(404, b"not found")

    def do_POST(self):
        if self.path != "/e":
            return self._reply(404, b"not found")
        n = int(self.headers.get("Content-Length") or 0)
        if n > 65536:
            return self._reply(413, b"too big")
        raw = self.rfile.read(n)
        ip = self.client_address[0]

        with State.lock:
            count, until = State.fails.get(ip, (0, 0))
        if until > time.time():
            return self._reply(429, b"too many wrong PINs, wait a minute")

        try:
            msg = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._reply(400, b"bad json")

        if not secrets.compare_digest(str(msg.get("pin", "")), State.pin):
            with State.lock:
                count += 1
                State.fails[ip] = (count, time.time() + 60 if count >= 10 else 0)
            time.sleep(0.5)
            return self._reply(401, b"wrong pin")

        with State.lock:
            State.fails.pop(ip, None)
            new = ip not in State.seen
            State.seen.add(ip)
        if new:
            print("[+] Phone connected from %s" % ip)

        try:
            apply_message(State.dev, msg)
        except Exception as e:
            print("[!] Error applying input: %s" % e)
        self._reply(200, b"ok")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, ctx):
        super().__init__(addr, handler)
        self.ctx = ctx

    def finish_request(self, request, client_address):
        # Do the TLS handshake in the worker thread, not the accept loop.
        try:
            request.settimeout(15)
            tls = self.ctx.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return  # e.g. Safari closing the connection at the certificate warning
        try:
            self.RequestHandlerClass(tls, client_address, self)
        finally:
            try:
                tls.close()
            except OSError:
                pass

    def handle_error(self, request, client_address):
        err = sys.exc_info()[1]
        if isinstance(err, (ssl.SSLError, ConnectionError, TimeoutError, socket.timeout)):
            return
        super().handle_error(request, client_address)


# =============================================================================
#  Setup helpers
# =============================================================================

def local_ips():
    ips = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the Wi-Fi/LAN adapter
        ips.append(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips or ["127.0.0.1"]


def ensure_cert(ip):
    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        return
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError:
        print("One extra package is needed to create the security certificate.")
        print("Run this once, then start PhoneMouse again:")
        print()
        print("    python -m pip install cryptography")
        print()
        input("Press Enter to exit...")
        sys.exit(1)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PhoneMouse")])
    sans = [x509.DNSName("localhost")]
    try:
        sans.append(x509.IPAddress(ipaddress.ip_address(ip)))
    except ValueError:
        pass
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=820))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    with open(KEY_FILE, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    with open(CERT_FILE, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


def load_pin(new):
    if not new and os.path.exists(PIN_FILE):
        with open(PIN_FILE) as f:
            pin = f.read().strip()
        if pin.isdigit() and len(pin) >= 4:
            return pin
    pin = "%06d" % secrets.randbelow(1000000)
    with open(PIN_FILE, "w") as f:
        f.write(pin)
    return pin


def device_kind(platform, dry_run):
    """Which input device to use: "windows", "mac", or "dry" (print only)."""
    if dry_run:
        return "dry"
    return {"win32": "windows", "darwin": "mac"}.get(platform, "dry")


def page_html(host):
    """The phone page, told which kind of computer it controls ("windows" or "mac")."""
    return PAGE.replace("__HOST__", host)


def main():
    ap = argparse.ArgumentParser(description="Use your phone as an air mouse + keyboard.")
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--new-pin", action="store_true", help="generate a new PIN")
    ap.add_argument("--dry-run", action="store_true", help="print actions instead of controlling the computer")
    args = ap.parse_args()

    kind = device_kind(sys.platform, args.dry_run)
    if sys.platform == "darwin":
        State.host = "mac"
    if kind == "windows":
        State.dev = WindowsInput()
    elif kind == "mac":
        events = MacEvents()
        if not events.allowed():
            events.ask_permission()
            print("[!] macOS is blocking PhoneMouse from controlling the mouse and keyboard.")
            print("    Open System Settings, then Privacy & Security, then Accessibility,")
            print("    and switch on the app you started PhoneMouse from (Terminal, iTerm, VS Code).")
            print("    Then stop PhoneMouse with Ctrl+C and start it again.")
            print()
        State.dev = MacInput(events)
    else:
        if not args.dry_run:
            print("[i] This system is not supported yet, so actions are printed instead.")
        State.dev = DryRunInput()

    ips = local_ips()
    ensure_cert(ips[0])
    State.pin = load_pin(args.new_pin)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT_FILE, KEY_FILE)

    try:
        httpd = Server(("0.0.0.0", args.port), Handler, ctx)
    except OSError as e:
        print("[!] Could not start on port %d (%s). Try: python phone_mouse.py --port 9443" % (args.port, e))
        input("Press Enter to exit...")
        return

    line = "=" * 62
    print(line)
    print("  PhoneMouse is running")
    print(line)
    print("  1. Connect your phone to the SAME Wi-Fi as this computer.")
    print("  2. Open Safari (iPhone) or Chrome (Android) on the phone and go to:")
    print()
    print("        https://%s:%d" % (ips[0], args.port))
    for other in ips[1:]:
        print("        (or https://%s:%d if that one doesn't load)" % (other, args.port))
    print()
    print("     On the 'not private' warning tap:")
    print("       Safari: Show Details, then visit this website, then Visit Website.")
    print("       Chrome: Advanced, then Proceed.")
    print("  3. Enter this PIN:   %s" % State.pin)
    print(line)
    print("  Press Ctrl+C here to stop.")
    print()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


# =============================================================================
#  The phone page
# =============================================================================

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0b0d12">
<title>PhoneMouse</title>
<style>
:root{--bg:#0b0d12;--panel:#151922;--panel2:#1c212c;--line:#272d3a;--text:#eef0f5;--dim:#8b93a6;
--laser:#ff3b3b;--laser2:#ff7a45;--ok:#37d67a;--warn:#ffb020}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent;-webkit-touch-callout:none;-webkit-user-select:none;user-select:none}
html,body{margin:0;height:100%;background:var(--bg);color:var(--text);font:15px/1.3 -apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif;overflow:hidden;overscroll-behavior:none}
body{display:flex;flex-direction:column;gap:10px;padding:calc(env(safe-area-inset-top) + 8px) 12px calc(env(safe-area-inset-bottom) + 12px);touch-action:none}
button{font:inherit;color:inherit;border:0;background:none;padding:0;cursor:pointer}
header{display:flex;align-items:center;gap:10px;height:34px}
.brand{font-weight:700;letter-spacing:.2px}
.brand b{color:var(--laser)}
#status{display:flex;align-items:center;gap:7px;margin-left:auto;color:var(--dim);font-size:13px;font-variant-numeric:tabular-nums}
#dot{width:9px;height:9px;border-radius:50%;background:#555}
#dot.ok{background:var(--ok);box-shadow:0 0 8px var(--ok)}
#dot.bad{background:var(--laser)}
#gear{width:34px;height:34px;border-radius:10px;background:var(--panel);display:grid;place-items:center}
#gear svg{width:18px;height:18px;stroke:var(--dim)}

.typebar{display:flex;gap:8px;touch-action:manipulation}
input{-webkit-user-select:text!important;user-select:text!important;touch-action:manipulation;pointer-events:auto}
#kbBtn{flex:none;width:52px;height:44px;border-radius:12px;background:var(--panel);border:1px solid var(--line);font-size:22px;touch-action:manipulation}
#kbBtn:active{border-color:var(--laser)}
#kb{flex:1;min-width:0;height:44px;border-radius:12px;border:1px solid var(--line);background:var(--panel);color:var(--text);
padding:0 14px;font:16px -apple-system,system-ui,sans-serif;outline:none;-webkit-user-select:text;user-select:text;caret-color:var(--laser)}
#kb:focus{border-color:var(--laser)}
#kb::placeholder{color:var(--dim)}
.keys{display:flex;gap:6px;overflow-x:auto;touch-action:pan-x;scrollbar-width:none;margin:0 -12px;padding:0 12px}
.keys::-webkit-scrollbar{display:none}
.keys button{flex:none;height:36px;min-width:44px;padding:0 12px;border-radius:10px;background:var(--panel);border:1px solid var(--line);font-size:14px;color:#cfd4df}
.keys button:active{background:var(--panel2);border-color:var(--laser)}

#aim{position:relative;flex:1;min-height:180px;border-radius:22px;overflow:hidden;
background:radial-gradient(circle at 50% 50%,#1a1f2b 0%,#12151d 60%,#0f1218 100%);border:1px solid var(--line);
display:grid;place-items:center;transition:border-color .15s}
#aim .ring{position:absolute;border-radius:50%;border:1px dashed #2a3140}
#aim .r1{width:46%;aspect-ratio:1}
#aim .r2{width:80%;aspect-ratio:1;border-style:solid;border-color:#1d2330}
#aim .cross{position:absolute;inset:0;background:
linear-gradient(#232937,#232937) center/1px 100% no-repeat,linear-gradient(#232937,#232937) center/100% 1px no-repeat;opacity:.7}
#beam{position:absolute;width:18px;height:18px;margin:-9px 0 0 -9px;left:50%;top:50%;border-radius:50%;
background:radial-gradient(circle,#fff 0 18%,var(--laser) 40%,rgba(255,59,59,0) 72%);opacity:.35;transition:opacity .15s;will-change:transform}
#aim .label{position:absolute;bottom:14px;left:0;right:0;text-align:center;color:var(--dim);font-size:13px;pointer-events:none}
#aim .big{position:absolute;top:14px;left:16px;font-size:12px;letter-spacing:1.5px;text-transform:uppercase;color:#5d6578;pointer-events:none}
#laserBar{position:absolute;top:10px;right:10px;display:none;gap:8px}
#laserBar button{height:38px;padding:0 14px;border-radius:12px;background:rgba(255,59,59,.14);border:1px solid rgba(255,59,59,.5);color:#fff;font-weight:600;font-size:14px}
#laserBar button:active{background:rgba(255,59,59,.35)}
#aim.laser #laserBar{display:flex}
#aim.laser #beam{opacity:1;width:24px;height:24px;margin:-12px 0 0 -12px;box-shadow:0 0 22px 6px rgba(255,59,59,.45)}
#aim.on{border-color:var(--laser);box-shadow:inset 0 0 60px rgba(255,59,59,.12)}
#aim.on #beam{opacity:1;width:26px;height:26px;margin:-13px 0 0 -13px;box-shadow:0 0 24px 6px rgba(255,59,59,.45)}

.clicks{display:grid;grid-template-columns:1fr 64px 1fr;gap:10px;height:112px}
.clicks .btn{border-radius:20px;background:var(--panel);border:1px solid var(--line);font-weight:600;font-size:15px;color:#d9dde6;display:grid;place-items:center}
.clicks .btn.down{background:linear-gradient(180deg,#3a1d22,#2a1519);border-color:var(--laser);color:#fff}
#scroll{border-radius:20px;background:repeating-linear-gradient(180deg,#151922 0 10px,#191e28 10px 20px);border:1px solid var(--line);
display:grid;place-items:center;color:var(--dim);font-size:11px;letter-spacing:1px;writing-mode:vertical-rl}
#scroll.down{border-color:var(--laser2);color:var(--text)}

.overlay{position:fixed;inset:0;background:rgba(6,7,10,.86);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
display:none;align-items:center;justify-content:center;padding:24px;z-index:10;touch-action:auto}
.overlay.show{display:flex}
.card{width:100%;max-width:380px;background:var(--panel);border:1px solid var(--line);border-radius:22px;padding:22px}
.card h1{margin:0 0 4px;font-size:22px}
.card h1 b{color:var(--laser)}
.card p{margin:0 0 16px;color:var(--dim);font-size:14px}
.card input[type=tel]{width:100%;height:52px;border-radius:14px;border:1px solid var(--line);background:#0f1218;color:var(--text);
font:600 24px ui-monospace,Menlo,monospace;letter-spacing:6px;text-align:center;outline:none;-webkit-user-select:text;user-select:text}
.card input[type=tel]:focus{border-color:var(--laser)}
.primary{width:100%;height:52px;margin-top:14px;border-radius:14px;background:linear-gradient(180deg,var(--laser),#d92b2b);color:#fff;font-weight:700;font-size:16px}
.err{color:var(--laser);font-size:13px;min-height:18px;margin-top:10px}
.small{color:var(--dim);font-size:12px;margin-top:6px}

.sheet .card{max-height:88vh;overflow:auto}
.row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 0;border-bottom:1px solid var(--line)}
.row:last-of-type{border-bottom:0}
.row label{font-size:14px}
.row .val{color:var(--dim);font-size:13px;font-variant-numeric:tabular-nums;min-width:34px;text-align:right}
.row input[type=range]{flex:1;accent-color:var(--laser)}
.seg{display:flex;background:#0f1218;border-radius:10px;padding:3px;gap:3px}
.seg button{padding:7px 10px;border-radius:8px;font-size:13px;color:var(--dim)}
.seg button.sel{background:var(--panel2);color:var(--text)}
.tog{width:46px;height:28px;border-radius:14px;background:#2a2f3b;position:relative;flex:none}
.tog::after{content:"";position:absolute;top:3px;left:3px;width:22px;height:22px;border-radius:50%;background:#fff;transition:left .15s}
.tog.on{background:var(--laser)}
.tog.on::after{left:21px}
#debug{font:12px ui-monospace,Menlo,monospace;color:var(--dim);margin-top:10px}
.ghost{width:100%;height:46px;margin-top:12px;border-radius:12px;background:var(--panel2);font-weight:600}
</style>
</head>
<body>

<header>
  <div class="brand">Phone<b>Mouse</b></div>
  <div id="status"><span id="dot"></span><span id="stxt">Not connected</span></div>
  <button id="gear" aria-label="Settings"><svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg></button>
</header>

<div class="typebar">
  <input id="kb" type="text" placeholder="Tap here to type on your computer..." autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" enterkeyhint="send">
  <button id="kbBtn" aria-label="Open keyboard">&#9000;</button>
</div>

<div class="keys" id="keys">
  <button data-k="enter">Enter</button>
  <button data-k="backspace">&#9003;</button>
  <button data-k="esc">Esc</button>
  <button data-k="tab">Tab</button>
  <button data-k="left">&larr;</button>
  <button data-k="up">&uarr;</button>
  <button data-k="down">&darr;</button>
  <button data-k="right">&rarr;</button>
  <button data-c="ctrl+c">Copy</button>
  <button data-c="ctrl+v">Paste</button>
  <button data-c="ctrl+z">Undo</button>
  <button data-c="ctrl+a">Select all</button>
  <button data-c="alt+tab">Alt+Tab</button>
  <button data-c="win+d">Desktop</button>
  <button data-k="win">Win</button>
  <button data-k="f5">F5</button>
  <button data-k="playpause">&#9199;</button>
  <button data-k="voldown">Vol&minus;</button>
  <button data-k="volup">Vol+</button>
  <button data-k="mute">Mute</button>
</div>

<div id="aim">
  <div class="cross"></div><div class="ring r2"></div><div class="ring r1"></div>
  <div id="beam"></div>
  <div id="laserBar"><button id="centerBtn">&#9678; Center</button><button id="edgeBtn">&harr; Set edge</button></div>
  <div class="big" id="modeTag">Air mouse</div>
  <div class="label" id="aimLabel">Hold &amp; point to move &middot; quick tap = click</div>
</div>

<div class="clicks">
  <button class="btn" id="lb">Left</button>
  <div id="scroll">SCROLL</div>
  <button class="btn" id="rb">Right</button>
</div>

<!-- connect screen -->
<div class="overlay show" id="connect">
  <div class="card">
    <h1>Phone<b>Mouse</b></h1>
    <p>Enter the PIN shown in the window on your computer.</p>
    <input id="pin" type="tel" inputmode="numeric" maxlength="8" placeholder="------">
    <button class="primary" id="start">Start</button>
    <div class="err" id="cerr"></div>
    <div class="small">Tap "Allow" when asked about Motion &amp; Orientation &mdash; that's what lets you point.</div>
  </div>
</div>

<!-- settings -->
<div class="overlay sheet" id="settings">
  <div class="card">
    <h1 style="font-size:19px;margin-bottom:8px">Settings</h1>
    <div class="row"><label>Mode</label>
      <div class="seg" id="modeSeg"><button data-m="laser">Laser</button><button data-m="hold">Hold</button><button data-m="toggle">Toggle</button><button data-m="pad">Touchpad</button></div></div>
    <div class="row"><label>Laser reach</label><input type="range" id="range" min="6" max="50" step="1"><span class="val" id="rangeV"></span></div>
    <div class="row"><label>Speed</label><input type="range" id="sens" min="10" max="250" step="1"><span class="val" id="sensV"></span></div>
    <div class="row"><label>Steadiness</label><input type="range" id="smooth" min="0" max="0.9" step="0.05"><span class="val" id="smoothV"></span></div>
    <div class="row"><label>Shake filter</label><input type="range" id="dz" min="0" max="6" step="0.25"><span class="val" id="dzV"></span></div>
    <div class="row"><label>Acceleration</label><input type="range" id="accel" min="0" max="2" step="0.1"><span class="val" id="accelV"></span></div>
    <div class="row"><label>Flip left/right</label><button class="tog" id="invX"></button></div>
    <div class="row"><label>Flip up/down</label><button class="tog" id="invY"></button></div>
    <div class="row"><label>Swap sensor axes</label><button class="tog" id="swapAxes"></button></div>
    <div id="debug">motion: waiting...</div>
    <button class="ghost" id="close">Done</button>
  </div>
</div>

<script>
(function(){
"use strict";
const $ = s => document.querySelector(s);

// ---------------- settings ----------------
const DEF = {pin:"", sens:75, smooth:0.3, dz:1.2, accel:0.3, invX:false, invY:false, swapAxes:false, mode:"hold", range:18};
let S = Object.assign({}, DEF);
try {
  const saved = JSON.parse(localStorage.getItem("phonemouse4") || "null");
  if (saved) Object.assign(S, saved);
  else S.pin = (JSON.parse(localStorage.getItem("phonemouse2") || localStorage.getItem("phonemouse") || "{}").pin) || "";
} catch(e) {}
function save(){ try { localStorage.setItem("phonemouse4", JSON.stringify(S)); } catch(e) {} }

// ---------------- connection ----------------
let pdx = 0, pdy = 0, queue = [], inflight = false, started = false, rtt = 0;
let pendAbs = null, lastAbs = [0, 0];   // laser position waiting to be sent

function setStatus(kind, text){
  $("#dot").className = kind;
  $("#stxt").textContent = text;
}

function send(ev){
  if (ev[0] === "wheel" && queue.length && queue[queue.length-1][0] === "wheel") {
    queue[queue.length-1][1] += ev[1];
  } else {
    queue.push(ev);
  }
  pump();
}

function pump(){
  if (!started || inflight) return;
  if (!queue.length && !pendAbs && Math.abs(pdx) < 0.01 && Math.abs(pdy) < 0.01) return;
  const ev = queue; queue = [];
  const msg = {pin:S.pin, dx:Math.round(pdx*100)/100, dy:Math.round(pdy*100)/100, ev:ev};
  if (pendAbs) msg.abs = [Math.round(pendAbs[0]*10000)/10000, Math.round(pendAbs[1]*10000)/10000];
  const body = JSON.stringify(msg);
  pdx = 0; pdy = 0; pendAbs = null;
  inflight = true;
  const t0 = performance.now();
  fetch("/e", {method:"POST", headers:{"Content-Type":"application/json"}, body:body, cache:"no-store"})
    .then(r => {
      if (r.status === 401) { started = false; setStatus("bad","Wrong PIN"); showConnect("That PIN didn't match. Check the window on your computer."); return; }
      if (r.status === 429) { setStatus("bad","Locked - wait 1 min"); return; }
      if (!r.ok) throw new Error("http " + r.status);
      const ms = performance.now() - t0;
      rtt = rtt ? rtt*0.8 + ms*0.2 : ms;
      setStatus("ok", "Connected · " + Math.round(rtt) + " ms");
    })
    .catch(() => {
      setStatus("bad", "Can't reach computer");
      // keep button/key events so nothing gets stuck; drop old ones if it's down for long
      queue = ev.concat(queue).slice(-100);
    })
    .finally(() => { inflight = false; if (queue.length || pendAbs || pdx || pdy) setTimeout(pump, queue.length ? 0 : 1); });
}

setInterval(() => { if (started && !inflight && !queue.length) send(["ping"]); }, 1500);

// ---------------- motion -> cursor ----------------
const IOS = typeof DeviceMotionEvent !== "undefined" && typeof DeviceMotionEvent.requestPermission === "function";
let aiming = false, aimStart = 0, lastT = 0, sx = 0, sy = 0, motionOK = false, gotMotion = false;
let tilt = 70 * Math.PI/180;   // how upright the phone is (0 = flat on a table, 90 = standing up)
const beam = $("#beam"); let bx = 0, by = 0;

function onOrientation(e){
  if (e.beta == null) return;
  // Only the forward/back tilt is used: it stays stable even when the phone is upright.
  tilt = tilt*0.8 + (e.beta * Math.PI/180)*0.2;
}

function onMotion(e){
  const r = e.rotationRate;
  if (!r || r.alpha == null) return;
  gotMotion = true;
  const now = performance.now();
  let dt = lastT ? (now - lastT)/1000 : 0.016;
  lastT = now;
  dt = Math.min(Math.max(dt, 0.001), 0.05);

  // Simple mapping:
  //   turn the phone left/right  -> cursor left/right
  //   tilt the top up/down       -> cursor up/down
  // "Turning" is measured around whichever phone axis currently points up, so it
  // works whether you hold the phone upright, flat, or anywhere in between.
  const up = Math.sin(tilt), flat = Math.cos(tilt);
  // Turn speed around the phone's own axes: x = across the screen, y = along it, z = out of it.
  // iPhones label these alpha/beta/gamma = x/y/z; other phones use alpha/beta/gamma = z/x/y.
  let wx, wy, wz;
  if (IOS !== !!S.swapAxes) { wx = r.alpha || 0; wy = r.beta || 0; wz = r.gamma || 0; }
  else { wx = r.beta || 0; wy = r.gamma || 0; wz = r.alpha || 0; }
  const yaw = wy*up + wz*flat;   // turning left/right
  const pitch = wx;              // tilting the top up/down

  let vx = -yaw, vy = -pitch;    // turn left = cursor left, tilt up = cursor up
  if (S.invX) vx = -vx;
  if (S.invY) vy = -vy;

  if (settingsOpen) $("#debug").textContent = "turn " + vx.toFixed(1) + "  tilt " + vy.toFixed(1) + " deg/s   held at " + Math.round(tilt*180/Math.PI) + "\u00b0";

  if (S.mode === "laser") { laserStep(vx, vy, dt); return; }

  if (!aiming || S.mode === "pad" || now - aimStart < 110) { sx = sy = 0; return; }

  // soft dead zone removes hand shake
  const m = Math.hypot(vx, vy);
  if (m <= S.dz) { vx = vy = 0; } else { const k = (m - S.dz)/m; vx *= k; vy *= k; }
  // smoothing
  const a = S.smooth;
  sx = sx*a + vx*(1-a); sy = sy*a + vy*(1-a);
  // acceleration: fast flicks travel further, slow moves stay precise
  const sp = Math.hypot(sx, sy);
  const gain = S.sens * (1 + S.accel * Math.min(sp/80, 3));
  const dx = sx*dt*gain, dy = sy*dt*gain;
  if (dx || dy) { pdx += dx; pdy += dy; moveBeam(dx, dy); pump(); }
}

// ---------------- laser mode ----------------
// The phone's angle away from where you pressed "Center" decides where the cursor sits:
// point 10 degrees right of centre and the cursor sits the same distance right of the middle.
let lax = 0, lay = 0, lsx = 0, lsy = 0, frozen = false;
function laserStep(vx, vy, dt){
  if (Math.abs(vx) > 0.3) lax += vx*dt;   // tiny readings are sensor noise; skipping them reduces drift
  if (Math.abs(vy) > 0.3) lay += vy*dt;
  const lim = S.range*1.6;
  lax = Math.max(-lim, Math.min(lim, lax));
  lay = Math.max(-lim, Math.min(lim, lay));
  // steady when your hand is still, quick when you move
  const d = Math.hypot(lax - lsx, lay - lsy);
  const k = Math.min(1, (1 - S.smooth)*0.35 + d*0.3);
  lsx += (lax - lsx)*k; lsy += (lay - lsy)*k;
  if (frozen) return;
  const t = Math.tan(S.range*Math.PI/180);
  const nx = Math.tan(lsx*Math.PI/180)/t, ny = Math.tan(lsy*Math.PI/180)/t;
  if (Math.abs(nx - lastAbs[0]) > 0.0004 || Math.abs(ny - lastAbs[1]) > 0.0004) {
    lastAbs = [nx, ny]; pendAbs = [nx, ny]; pump();
  }
}
function laserCenter(){
  lax = lay = lsx = lsy = 0; lastAbs = [0, 0]; pendAbs = [0, 0]; pump();
  flash("Centered \u2014 the cursor is now where you're pointing");
}
function laserEdge(){
  const a = Math.abs(lsx);
  if (a < 3) { flash("Point at the left or right edge of the screen, then tap Set edge"); return; }
  S.range = Math.round(Math.min(50, Math.max(6, a))); save();
  $("#range").value = S.range; $("#rangeV").textContent = S.range + "\u00b0";
  flash("Edge set \u2014 reach is " + S.range + "\u00b0");
}
let flashT = null;
function flash(text){
  $("#aimLabel").textContent = text;
  clearTimeout(flashT);
  flashT = setTimeout(refreshLabel, 2500);
}
["centerBtn","edgeBtn"].forEach(id => {
  const b = $("#"+id);
  b.addEventListener("pointerdown", e => e.stopPropagation());
  b.addEventListener("pointerup", e => e.stopPropagation());
  b.addEventListener("click", e => { e.stopPropagation(); id === "centerBtn" ? laserCenter() : laserEdge(); });
});

function moveBeam(dx, dy){
  const el = $("#aim"); const W = el.clientWidth/2 - 20, H = el.clientHeight/2 - 20;
  bx = Math.max(-W, Math.min(W, bx + dx*0.35));
  by = Math.max(-H, Math.min(H, by + dy*0.35));
}
(function beamLoop(){
  if (S.mode === "laser") {
    const el = $("#aim"); const W = el.clientWidth/2 - 16, H = el.clientHeight/2 - 16;
    bx = Math.max(-W, Math.min(W, lastAbs[0]*W));
    by = Math.max(-H, Math.min(H, lastAbs[1]*W));
  } else { bx *= 0.9; by *= 0.9; }
  beam.style.transform = "translate(" + bx.toFixed(1) + "px," + by.toFixed(1) + "px)";
  requestAnimationFrame(beamLoop);
})();

async function enableMotion(){
  try {
    const reqs = [];
    if (typeof DeviceMotionEvent !== "undefined" && DeviceMotionEvent.requestPermission) reqs.push(DeviceMotionEvent.requestPermission());
    if (typeof DeviceOrientationEvent !== "undefined" && DeviceOrientationEvent.requestPermission) reqs.push(DeviceOrientationEvent.requestPermission());
    const res = await Promise.all(reqs);
    if (res.length && res[0] !== "granted") throw new Error("denied");
    if (!motionOK) {
      window.addEventListener("devicemotion", onMotion);
      window.addEventListener("deviceorientation", onOrientation);
    }
    motionOK = true;
  } catch (e) {
    motionOK = false;
  }
}

// ---------------- aim pad ----------------
const aim = $("#aim");
let aimPid = null, padLast = null, padMoved = 0, tapT = 0;

function setAim(on){
  aiming = on;
  aim.classList.toggle("on", on);
  if (on) { aimStart = performance.now(); sx = sy = 0; }
}

const MODES = {laser:["Laser","Point at the screen \u00b7 tap = click"],
             hold:["Air mouse","Hold & point to move \u00b7 quick tap = click"],
             toggle:["Air mouse \u00b7 toggle","Tap to start / stop pointing"],
             pad:["Touchpad","Drag to move \u00b7 tap = click"]};
function refreshLabel(){ $("#aimLabel").textContent = MODES[S.mode][1]; }
function refreshMode(){
  aim.classList.toggle("laser", S.mode === "laser");
  frozen = false;
  const t = {hold:["Air mouse","Hold & point to move · quick tap = click"],
             toggle:["Air mouse · toggle","Tap to start / stop pointing"],
             pad:["Touchpad","Drag to move · tap = click"], laser:MODES.laser}[S.mode];
  $("#modeTag").textContent = t[0];
  $("#aimLabel").textContent = t[1];
  setAim(false);
  document.querySelectorAll("#modeSeg button").forEach(b => b.classList.toggle("sel", b.dataset.m === S.mode));
}

aim.addEventListener("pointerdown", e => {
  e.preventDefault();
  if (aimPid !== null) return;
  aimPid = e.pointerId;
  try { aim.setPointerCapture(e.pointerId); } catch(_) {}
  tapT = performance.now(); padLast = {x:e.clientX, y:e.clientY}; padMoved = 0;
  if (S.mode === "hold") setAim(true);
  else if (S.mode === "pad") aim.classList.add("on");
  else if (S.mode === "laser") { frozen = true; aim.classList.add("on"); }  // hold the cursor still while you tap
});
aim.addEventListener("pointermove", e => {
  if (e.pointerId !== aimPid || S.mode !== "pad") return;
  const dx = e.clientX - padLast.x, dy = e.clientY - padLast.y;
  padLast = {x:e.clientX, y:e.clientY};
  padMoved += Math.abs(dx) + Math.abs(dy);
  const sp = Math.hypot(dx, dy);
  const k = 1.6 * (S.sens/45) * (1 + S.accel * Math.min(sp/12, 2.5));
  pdx += dx*k; pdy += dy*k; moveBeam(dx*k, dy*k); pump();
});
function aimEnd(e, cancelled){
  if (e.pointerId !== aimPid) return;
  aimPid = null;
  const quick = performance.now() - tapT < 220;
  if (S.mode === "laser") { frozen = false; aim.classList.remove("on"); if (quick && !cancelled) send(["click","l"]); }
  else if (S.mode === "hold") { setAim(false); if (quick && !cancelled) send(["click","l"]); }
  else if (S.mode === "toggle") { if (!cancelled) setAim(!aiming); }
  else { aim.classList.remove("on"); if (quick && padMoved < 10 && !cancelled) send(["click","l"]); }
}
aim.addEventListener("pointerup", e => aimEnd(e, false));
aim.addEventListener("pointercancel", e => aimEnd(e, true));

// ---------------- click buttons (press & hold = drag) ----------------
function holdButton(el, down, upEv){
  let pid = null;
  el.addEventListener("pointerdown", e => {
    e.preventDefault(); if (pid !== null) return;
    pid = e.pointerId; try { el.setPointerCapture(pid); } catch(_) {}
    el.classList.add("down"); send([down]);
  });
  const release = e => { if (e.pointerId !== pid) return; pid = null; el.classList.remove("down"); send([upEv]); };
  el.addEventListener("pointerup", release);
  el.addEventListener("pointercancel", release);
  return () => { if (pid !== null) { pid = null; el.classList.remove("down"); send([upEv]); } };
}
const releaseL = holdButton($("#lb"), "ld", "lu");
const releaseR = holdButton($("#rb"), "rd", "ru");

// ---------------- scroll strip ----------------
const sc = $("#scroll"); let scPid = null, scY = 0, scAcc = 0;
sc.addEventListener("pointerdown", e => { e.preventDefault(); scPid = e.pointerId; scY = e.clientY; scAcc = 0; try { sc.setPointerCapture(scPid); } catch(_) {} sc.classList.add("down"); });
sc.addEventListener("pointermove", e => {
  if (e.pointerId !== scPid) return;
  scAcc += (e.clientY - scY) * 4;   // finger up = page scrolls down (like a phone)
  scY = e.clientY;
  const n = Math.trunc(scAcc);
  if (n) { scAcc -= n; send(["wheel", n]); }
});
const scEnd = e => { if (e.pointerId === scPid) { scPid = null; sc.classList.remove("down"); } };
sc.addEventListener("pointerup", scEnd); sc.addEventListener("pointercancel", scEnd);

// ---------------- keyboard ----------------
const kb = $("#kb");
const SEN = "​";   // invisible marker so Backspace works even when the box looks empty
let last = SEN; kb.value = SEN;
// iPhone only opens the keyboard when focus() happens directly inside a tap
function openKb(e){ if (e) e.stopPropagation(); if (document.activeElement !== kb) { kb.focus(); try { kb.setSelectionRange(kb.value.length, kb.value.length); } catch(_) {} } }
kb.addEventListener("touchend", openKb);
kb.addEventListener("click", openKb);
$("#kbBtn").addEventListener("touchend", e => { e.preventDefault(); if (document.activeElement === kb) kb.blur(); else openKb(e); });
$("#kbBtn").addEventListener("click", e => { if (document.activeElement !== kb) openKb(e); });
kb.addEventListener("focus", () => { if (!kb.value.startsWith(SEN)) { kb.value = SEN + kb.value; last = kb.value; } });
kb.addEventListener("input", () => {
  const a = Array.from(last), b = Array.from(kb.value);
  let i = 0; const n = Math.min(a.length, b.length);
  while (i < n && a[i] === b[i]) i++;
  const del = a.length - i;
  const add = b.slice(i).join("").split(SEN).join("");
  if (del > 0) send(["bs", del]);
  if (add) send(["text", add]);
  if (!kb.value.startsWith(SEN)) {
    kb.value = SEN + kb.value.split(SEN).join("");
    try { kb.setSelectionRange(kb.value.length, kb.value.length); } catch(_) {}
  }
  if (kb.value.length > 300) kb.value = SEN;
  last = kb.value;
});
kb.addEventListener("keydown", e => {
  if (e.key === "Enter") { e.preventDefault(); send(["key","enter"]); kb.value = SEN; last = SEN; }
});

// The same buttons do the matching Mac action, so they get Mac names.
const HOST = "__HOST__";
const MAC_LABELS = {"alt+tab": "Switch app", "win": "Spotlight", "f5": "Reload"};
document.querySelectorAll("#keys button").forEach(b => {
  const macLabel = MAC_LABELS[b.dataset.c || b.dataset.k];
  if (HOST === "mac" && macLabel) b.textContent = macLabel;
  b.addEventListener("click", () => {
    if (b.dataset.k) send(["key", b.dataset.k]);
    else send(["combo", b.dataset.c.split("+")]);
  });
});

// ---------------- connect screen ----------------
function showConnect(msg){
  $("#cerr").textContent = msg || "";
  $("#connect").classList.add("show");
}
$("#pin").value = S.pin || "";
$("#start").addEventListener("click", async () => {
  const p = $("#pin").value.trim();
  if (!/^\d{4,8}$/.test(p)) { $("#cerr").textContent = "Enter the PIN from the window on your computer."; return; }
  S.pin = p; save();
  if (!window.isSecureContext) { $("#cerr").textContent = "Open this page with https:// (not http://) so motion works."; }
  await enableMotion();
  started = true;
  $("#connect").classList.remove("show");
  setStatus("", "Connecting...");
  send(["ping"]);
  keepAwake();
  setTimeout(() => {
    if (!gotMotion && S.mode !== "pad") {
      S.mode = "pad"; save(); refreshMode();
      $("#aimLabel").textContent = "Motion not available - using touchpad (see Settings)";
    }
  }, 1500);
});

// ---------------- settings sheet ----------------
let settingsOpen = false;
const fmt = {range:v=>v+"\u00b0", sens:v=>v, smooth:v=>Math.round(v*100)+"%", dz:v=>v, accel:v=>v};
["range","sens","smooth","dz","accel"].forEach(id => {
  const el = $("#"+id), v = $("#"+id+"V");
  el.value = S[id]; v.textContent = fmt[id](S[id]);
  el.addEventListener("input", () => { S[id] = parseFloat(el.value); v.textContent = fmt[id](S[id]); save(); });
});
["invX","invY","swapAxes"].forEach(id => {
  const el = $("#"+id); el.classList.toggle("on", S[id]);
  el.addEventListener("click", () => { S[id] = !S[id]; el.classList.toggle("on", S[id]); save(); });
});
document.querySelectorAll("#modeSeg button").forEach(b => b.addEventListener("click", async () => {
  S.mode = b.dataset.m; save(); refreshMode();
  if (S.mode === "laser") laserCenter();
  if (S.mode !== "pad" && !motionOK) await enableMotion();
}));
$("#gear").addEventListener("click", () => { settingsOpen = true; $("#settings").classList.add("show"); });
$("#close").addEventListener("click", () => { settingsOpen = false; $("#settings").classList.remove("show"); });

// ---------------- safety + keep screen on ----------------
let wakeLock = null;
async function keepAwake(){
  try { if (navigator.wakeLock && document.visibilityState === "visible") wakeLock = await navigator.wakeLock.request("screen"); } catch(e) {}
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") { setAim(false); releaseL(); releaseR(); }
  else if (started) keepAwake();
});

refreshMode();
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
