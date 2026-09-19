# -*- coding: utf-8 -*-
"""
Skill-check macro (RedM) - circle minigame W/A/S/D
F9  = start / pause
F10 = quit

Works only while the circle is on screen:
  - read letter in center (W/A/S/D)
  - track rotating needle + hatched zone
  - press the letter when needle is inside zone
"""
import os
import time
import threading
import traceback
from collections import deque
import numpy as np
import cv2
import mss
import keyboard
import pydirectinput
from PIL import Image, ImageDraw, ImageFont

# ---------- config (1920x1080) ----------
CX, CY       = 960, 562     # circle center on screen
HALF         = 130          # capture half-size around center
R_IN, R_OUT  = 66, 98       # ring sample radii (black ring between disk and outer circle)
NEEDLE_MIN   = 130          # needle: min brightness along radius
ZONE_MEAN    = 45           # zone: mean brightness along radius above this
ZONE_STRIPE  = 150          # zone: hatch stripes are near-white; background seen through the ring is not
ZONE_MIN_DEG = 25           # min zone width (deg) - filters mouse cursor / noise
ZONE_MAX_DEG = 110          # max zone width (deg) - wider = fade-in garbage
NEEDLE_MAX_W = 14           # needle wider than this (deg) = garbage
START_DELAY  = 0.10         # ignore first moments after circle appears (fade-in)
STABLE_FRAMES= 3            # zone must be identical this many frames before trusting it
MIN_SPEED    = 20           # deg/sec - needle must really be moving
BLOCK_KEYS   = True         # while minigame is up: ignore physical W/A/S/D (macro presses still pass)
SAVE_PRESS   = True         # save frame at each press to debug/ (max 60)
EDGE_MARGIN  = 3            # stay this many deg inside zone edges
SAFE_MARGIN  = 8            # late press (already past center) only if landing point is this far inside zone
LEAD_SEC     = 0.03         # input latency compensation
AIM          = 0.5          # where in zone to press: 0.5 = center
CENTER_TOL   = 4            # deg tolerance around aim point
KEY_HOLD     = 0.05         # key hold time
COOLDOWN     = 0.35         # wait after press (next stage loads)
LETTER_CONF  = 0.45         # min match score for letter
DEBUG        = False        # True = print angles every frame
HUD          = True         # overlay on screen (game must be borderless/windowed)
HUD_X, HUD_Y = 8, 8         # HUD position (drag to move)
KEY_HUD      = 'f11'        # show/hide HUD
# ----------------------------------------

BASE = os.path.dirname(os.path.abspath(__file__))
TPL_DIR = os.path.join(BASE, "templates")
SEEN_DIR = os.path.join(BASE, "seen")
DBG_DIR = os.path.join(BASE, "debug")

pydirectinput.PAUSE = 0
pydirectinput.FAILSAFE = False

running = False
alive = True
ST = {'circle': False, 'letter': '-', 'needle': None, 'zone': None, 'last': '-', 'count': 0, 'fps': 0}

# ---- precomputed polar sampling ----
_ang = np.deg2rad(np.arange(360))
_rs = np.arange(R_IN, R_OUT, 2)
_PX = np.rint(HALF + np.outer(np.cos(_ang), _rs)).astype(int)   # [360, nr]
_PY = np.rint(HALF + np.outer(np.sin(_ang), _rs)).astype(int)
_DX = np.rint(HALF + 48 * np.cos(_ang)).astype(int)             # disk edge ring (white)
_DY = np.rint(HALF + 48 * np.sin(_ang)).astype(int)
_yy, _xx = np.mgrid[-44:44, -44:44]
_DISK = (_xx ** 2 + _yy ** 2) < 40 ** 2


# ---- letter templates ----
def _norm(a):
    a = a.astype(np.float32)
    a -= a.mean()
    n = np.sqrt((a * a).sum())
    return a / n if n > 0 else a


def _synth(ch, font):
    im = Image.new('L', (200, 200), 0)
    ImageDraw.Draw(im).text((40, 20), ch, fill=255, font=ImageFont.truetype(font, 120))
    a = np.array(im)
    ys, xs = np.where(a > 128)
    return cv2.resize(a[ys.min():ys.max() + 1, xs.min():xs.max() + 1], (32, 32), interpolation=cv2.INTER_AREA)


def load_templates():
    tpl = {c: [] for c in "WASD"}
    for f in ['timesbd.ttf', 'georgiab.ttf', 'arialbd.ttf', 'courbd.ttf']:
        p = os.path.join('C:/Windows/Fonts', f)
        if os.path.exists(p):
            for c in "WASD":
                tpl[c].append(_norm(_synth(c, p)))
    if os.path.isdir(TPL_DIR):
        for fn in os.listdir(TPL_DIR):
            c = fn[0].upper()
            if c in tpl and fn.lower().endswith('.png'):
                img = cv2.imdecode(np.fromfile(os.path.join(TPL_DIR, fn), np.uint8), cv2.IMREAD_GRAYSCALE)
                if img is not None:
                    tpl[c].append(_norm(cv2.resize(img, (32, 32))))
    return tpl


TPL = load_templates()


def read_letter(g):
    """g = gray capture. return (letter, score, glyph32) or (None, 0, None)"""
    sub = g[HALF - 44:HALF + 44, HALF - 44:HALF + 44]
    ink = (sub < 110) & _DISK
    ys, xs = np.where(ink)
    if len(ys) < 40:
        return None, 0.0, None
    crop = ink[ys.min():ys.max() + 1, xs.min():xs.max() + 1].astype(np.uint8) * 255
    gl = cv2.resize(crop, (32, 32), interpolation=cv2.INTER_AREA)
    n = _norm(gl)
    best, bs = None, -1.0
    for c, lst in TPL.items():
        for t in lst:
            s = float((n * t).sum())
            if s > bs:
                best, bs = c, s
    return best, bs, gl


def analyze(g):
    """return dict(present, needle, zone=(start,width)); angles in deg, 0=right, 90=down"""
    ring = g[_PY, _PX].astype(np.float32)          # [360, nr]
    mean = ring.mean(axis=1)
    mn = ring.min(axis=1)
    disk_edge = g[_DY, _DX].mean()
    present = disk_edge > 140 and np.median(mean) < 60
    if not present:
        return {'present': False}

    # needle = solid bright line along radius
    needle = None
    nm = mn > NEEDLE_MIN
    if nm.any() and nm.sum() <= NEEDLE_MAX_W:
        a = np.deg2rad(np.where(nm)[0])
        needle = float(np.rad2deg(np.arctan2(np.sin(a).mean(), np.cos(a).mean())) % 360)

    # zone = hatched sector (bright mean); close small gaps, circular
    # hatch = several near-white samples along the radius. The ring is semi-transparent, so bright
    # background shows through as medium gray - mean alone mistakes that for a zone.
    bright3 = np.sort(ring, axis=1)[:, -3]          # 3rd brightest sample per angle
    zm = (mean > ZONE_MEAN) & (bright3 > ZONE_STRIPE)
    k = 5
    ext = np.concatenate([zm[-k:], zm, zm[:k]]).astype(np.uint8)
    ext = cv2.morphologyEx(ext.reshape(1, -1), cv2.MORPH_CLOSE, np.ones((1, k), np.uint8)).ravel()
    zm = ext[k:-k].astype(bool)
    zone = None
    if zm.any() and not zm.all():
        start0 = int(np.where(~zm)[0][0])          # rotate so index 0 is outside zone
        rot = np.roll(zm, -start0)
        d = np.diff(np.concatenate([[0], rot.astype(int), [0]]))
        ss, ee = np.where(d == 1)[0], np.where(d == -1)[0]
        w = ee - ss
        i = int(np.argmax(w))
        if ZONE_MIN_DEG <= w[i] <= ZONE_MAX_DEG:                   # narrow runs = needle / mouse cursor
            zone = ((int(ss[i]) + start0) % 360, int(w[i]))
    return {'present': True, 'needle': needle, 'zone': zone}


def in_zone(angle, zone, margin):
    s, w = zone
    if w <= 2 * margin:
        margin = 0
    return ((angle - s - margin) % 360) <= (w - 2 * margin)


def prev_f_speed_ok(speed):
    """need a real speed estimate before aiming"""
    return abs(speed) > MIN_SPEED


def tap(key):
    pydirectinput.keyDown(key)
    time.sleep(KEY_HOLD)
    pydirectinput.keyUp(key)


def save_dbg(g, tag):
    try:
        os.makedirs(DBG_DIR, exist_ok=True)
        if len(os.listdir(DBG_DIR)) < 60:
            name = "%d_%s.png" % (int(time.time() * 1000), tag)
            cv2.imencode('.png', g)[1].tofile(os.path.join(DBG_DIR, name))
    except Exception:
        pass


def save_seen(gl, tag):
    try:
        os.makedirs(SEEN_DIR, exist_ok=True)
        if len(os.listdir(SEEN_DIR)) < 50:
            name = "%s_%d.png" % (tag, int(time.time() * 1000))
            cv2.imencode('.png', gl)[1].tofile(os.path.join(SEEN_DIR, name))
    except Exception:
        pass


def worker():
    region = {'left': CX - HALF, 'top': CY - HALF, 'width': 2 * HALF, 'height': 2 * HALF}
    was_present = False
    prev_a, prev_t, speed = None, None, 0.0
    prev_f = time.perf_counter()
    last_t = [prev_f]
    appear_t = 0.0
    zref, zcount = None, 0
    hist = deque(maxlen=5)      # recent instantaneous speeds
    blocked = None              # zone we already pressed on (needle freezes there after a press)
    press_t = 0.0
    with mss.mss() as sct:
        while alive:
            if not running:
                was_present = False
                prev_a = None
                time.sleep(0.1)
                continue
            try:
                t = time.perf_counter()
                if True:
                    ST['fps'] = 0.9 * ST['fps'] + 0.1 / max(t - prev_f, 1e-4)
                frame_dt = min(max(t - prev_f, 0.001), 0.03)   # real time between frames, capped
                prev_f = t
                g = cv2.cvtColor(np.array(sct.grab(region)), cv2.COLOR_BGRA2GRAY)
                r = analyze(g)
                if not r['present']:
                    if was_present:
                        print("วงกลมหายแล้ว รอรอบใหม่...")
                    was_present = False
                    prev_a = None
                    speed = 0.0
                    ST.update(circle=False, letter='-', needle=None, zone=None)
                    blocked = None
                    hist.clear()
                    time.sleep(0.05)
                    continue
                if not was_present:
                    print("เจอวงกลม เริ่มทำงาน")
                    was_present = True
                    appear_t = t
                    zref, zcount = None, 0

                a, zone = r['needle'], r['zone']
                ST.update(circle=True, needle=a, zone=zone)
                if a is None or zone is None:
                    zcount = 0
                    continue
                # signed angular speed (deg/sec)
                if prev_a is not None:
                    dt = t - prev_t
                    if dt > 0:
                        da = (a - prev_a + 180) % 360 - 180
                        if abs(da) > 45 or dt > 0.1:
                            hist.clear()                  # needle jumped (new stage) - restart estimate
                        else:
                            hist.append(da / dt)
                prev_a, prev_t = a, t
                speed = float(np.median(hist)) if len(hist) >= 3 else 0.0

                if t - appear_t < START_DELAY:
                    continue
                # zone must hold still for several frames (kills fade-in / stage-change garbage)
                if zref is not None and abs((zone[0] - zref[0] + 180) % 360 - 180) <= 5 and abs(zone[1] - zref[1]) <= 6:
                    zcount += 1
                else:
                    zref, zcount = zone, 1
                if zcount < STABLE_FRAMES:
                    continue

                pred = (a + speed * LEAD_SEC) % 360
                if DEBUG:
                    print("needle=%.0f pred=%.0f zone=%s speed=%.0f" % (a, pred, zone, speed))

                # already pressed on this zone: needle freezes here while game shows result - never press twice
                if blocked is not None:
                    same = abs((zone[0] - blocked[0] + 180) % 360 - 180) <= 8 and abs(zone[1] - blocked[1]) <= 8
                    if same and not (t - press_t > 1.5 and abs(speed) > 150):
                        continue
                    blocked = None

                # aim at middle of zone, not the edge
                if prev_f_speed_ok(speed) is False:
                    continue
                target = (zone[0] + zone[1] * AIM) % 360
                off = (pred - target + 180) % 360 - 180          # signed distance to aim point
                # half a frame of travel. (old code used time since last *aim attempt*, which was huge
                #  after a wait -> tolerance blew up -> pressed at zone edge)
                frame_step = abs(speed) * frame_dt * 0.6
                inside = in_zone(a, zone, 0) or in_zone(pred, zone, 0)
                toward = off * speed < 0
                fire = False
                if inside:
                    if abs(off) <= min(max(CENTER_TOL, frame_step), 10):
                        fire = True                                # at aim point
                    elif not toward and in_zone(pred, zone, SAFE_MARGIN):
                        fire = True                                # got ready late, already past center: press NOW while still safely inside
                    # past center and landing point too close to the edge -> do not press, wait next pass
                if fire:
                    letter, score, gl = read_letter(g)
                    if letter is None or score < LETTER_CONF:
                        print("อ่านตัวอักษรไม่ชัด (%s %.2f) ข้าม" % (letter, score))
                        if gl is not None:
                            save_seen(gl, "unknown")
                        continue
                    tap(letter.lower())
                    if SAVE_PRESS:
                        save_dbg(g, "%s_n%03d_z%03d-%03d_s%+04d" % (letter, a, zone[0], zone[1], speed))
                    ST['count'] += 1
                    ST['letter'] = letter
                    ST['last'] = '%s  %s' % (letter, time.strftime('%H:%M:%S'))
                    print("กด %s  (เข็ม=%.0f โซน=%d+%d ความมั่นใจ=%.2f)" % (letter, a, zone[0], zone[1], score))
                    prev_a = None
                    speed = 0.0
                    zref, zcount = None, 0
                    hist.clear()
                    blocked = zone
                    press_t = time.perf_counter()
                    time.sleep(COOLDOWN)
            except Exception:
                print("เกิด error ข้ามเฟรมนี้")
                traceback.print_exc()
                time.sleep(0.5)


def toggle():
    global running
    running = not running
    print("เริ่มทำงาน (รอวงกลมขึ้น)" if running else "หยุดชั่วคราว")


def quit_():
    global alive, running
    running = False
    alive = False
    print("ออกโปรแกรม")


HOOK_OK = [False]
_MUTEX_NAME = "Global" + chr(92) + "SkillCheckMacro_SingleInstance"
_mutex_handle = None


def acquire_single_instance():
    """True if this is the first instance, False if another one is already running"""
    global _mutex_handle
    import ctypes
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    _mutex_handle = k32.CreateMutexW(None, False, _MUTEX_NAME)
    return ctypes.get_last_error() != 183      # ERROR_ALREADY_EXISTS


def warn_already_running():
    import ctypes
    msg = "มาโครเปิดอยู่แล้ว (ดู HUD มุมจอ หรือกด F11)" + chr(10) + "ถ้าจะปิดใช้ stop_goldpan.bat"
    ctypes.windll.user32.MessageBoxW(None, msg,
                                     "Skill-check macro", 0x30 | 0x40000)


def start_key_blocker():
    """low-level keyboard hook: swallow physical W/A/S/D key-downs while the circle is on screen.
    Injected keys (from this macro) pass. Key-ups always pass so a held key never gets stuck."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    LRESULT = ctypes.c_ssize_t
    ULONG_PTR = ctypes.c_size_t

    class KBD(ctypes.Structure):
        _fields_ = [('vkCode', wintypes.DWORD), ('scanCode', wintypes.DWORD), ('flags', wintypes.DWORD),
                    ('time', wintypes.DWORD), ('dwExtraInfo', ULONG_PTR)]

    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    user32.CallNextHookEx.restype = LRESULT
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    VKS = (0x57, 0x41, 0x53, 0x44)      # W A S D
    KEYDOWN = (0x0100, 0x0104)
    INJECTED = 0x10

    def proc(nCode, wParam, lParam):
        try:
            if nCode == 0 and running and ST['circle'] and wParam in KEYDOWN:
                kb = ctypes.cast(lParam, ctypes.POINTER(KBD)).contents
                if kb.vkCode in VKS and not (kb.flags & INJECTED):
                    ST['blocked'] = ST.get('blocked', 0) + 1
                    return 1
        except Exception:
            pass
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    cb = HOOKPROC(proc)

    def loop():
        hook = user32.SetWindowsHookExW(13, cb, kernel32.GetModuleHandleW(None), 0)
        HOOK_OK[0] = bool(hook)
        if not hook:
            print("ติดตั้งตัวบล็อกปุ่มไม่ได้ error", ctypes.get_last_error())
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass

    start_key_blocker._keep = cb        # keep callback alive
    threading.Thread(target=loop, daemon=True).start()


def run_hud():
    """Herring-style HUD: one line, top-left, drag to move, F11 hide/show, double right-click = quit"""
    import tkinter as tk
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.85)
    root.configure(bg="#111111")
    root.geometry("+%d+%d" % (HUD_X, HUD_Y))

    label = tk.Label(root, text="", fg="#cccccc", bg="#111111",
                     font=("Segoe UI", 10, "bold"), padx=10, pady=4)
    label.pack()

    drag = {"x": 0, "y": 0}

    def press(e):
        drag["x"], drag["y"] = e.x, e.y

    def move(e):
        root.geometry("+%d+%d" % (e.x_root - drag["x"], e.y_root - drag["y"]))

    label.bind("<Button-1>", press)
    label.bind("<B1-Motion>", move)
    label.bind("<Double-Button-3>", lambda _e: quit_())

    def refresh():
        if not alive:
            root.destroy()
            return
        if not running:
            txt, col = "พร้อม — กด F9 เริ่ม (F11 ซ่อน HUD)", "#cccccc"
        elif ST['circle']:
            lock = " | 🔒 ล็อก WASD" if (BLOCK_KEYS and HOOK_OK[0]) else ""
            txt, col = "🎯 QTE กำลังเล่น | กดแล้ว %d | ล่าสุด %s%s" % (ST['count'], ST['last'], lock), "#2ecc71"
        else:
            txt, col = "⏳ รอวงกลมขึ้น | กดแล้ว %d | ล่าสุด %s" % (ST['count'], ST['last']), "#f39c12"
        label.config(text=txt, fg=col)
        root.after(200, refresh)

    def keep_top():
        if alive:
            root.attributes("-topmost", True)
            root.lift()
            root.after(2000, keep_top)

    vis = [True]

    def toggle_hud():
        def run():
            if vis[0]:
                root.withdraw()
            else:
                root.deiconify()
            vis[0] = not vis[0]
        root.after(0, run)

    keyboard.add_hotkey(KEY_HUD, toggle_hud)
    refresh()
    keep_top()
    root.mainloop()


if __name__ == "__main__":
    if not acquire_single_instance():
        print("มาโครเปิดอยู่แล้ว - ปิดตัวนี้ทิ้ง")
        warn_already_running()
        raise SystemExit(0)
    keyboard.add_hotkey('f9', toggle)
    keyboard.add_hotkey('f10', quit_)
    print("F9 = เริ่ม/หยุด | F10 = ออก | F11 = ซ่อน/แสดง HUD")
    print("จุดกลางวงกลม = (%d,%d)  ทำงานเฉพาะตอนวงกลมขึ้น" % (CX, CY))
    threading.Thread(target=worker, daemon=True).start()
    if BLOCK_KEYS:
        start_key_blocker()
    if HUD:
        run_hud()
    else:
        while alive:
            time.sleep(0.2)
