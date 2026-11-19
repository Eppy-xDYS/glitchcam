#!/usr/bin/env python3
"""GlitchCam - a hand-bent camera for the M5Stack CardputerZero.

Live viewfinder on the 320x170 ST7789 (/dev/fb0), keyboard on the TCA8418
(evdev), IMX219 via picamera2. See README.md for keys.
"""

import os
import queue
import signal
import subprocess
import sys
import threading
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _early_splash():
    """Blit a pre-rendered splash before numpy/picamera2 take ~5 s to import."""
    try:
        with open(os.path.join(APP_DIR, "splash.rgb565"), "rb") as src, \
                open("/dev/fb0", "r+b", buffering=0) as fb:
            fb.write(src.read())
    except OSError:
        pass


if __name__ == "__main__" and "--render-splash" not in sys.argv:
    _early_splash()

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402
sys.path.insert(0, APP_DIR)

import glitch  # noqa: E402
from glitch import MODES, MODE_INFO  # noqa: E402

W, H = 320, 170
SAVE_DIR = os.environ.get("GLITCHCAM_DIR", os.path.expanduser("~/Pictures/GlitchCam"))
WORK_DIR = os.path.join(SAVE_DIR, ".work")  # on the SD card: /tmp is a small RAM disk
DEBUG = os.environ.get("GLITCHCAM_DEBUG") == "1"
CLIP_MAX = int(os.environ.get("GLITCHCAM_CLIP_MAX", "300"))  # seconds

# palette (dark navy, icy cyan, glyph magenta/blue)
BG = (6, 8, 16)
ACCENT = (168, 220, 240)
WHITE = (208, 238, 248)
MUTED = (92, 120, 160)
DIM = (24, 48, 80)
GLYPH1 = (224, 36, 90)
GLYPH2 = (48, 184, 224)

FONT_B = os.path.join(APP_DIR, "fonts", "SpaceMono-Bold.ttf")
FONT_R = os.path.join(APP_DIR, "fonts", "SpaceMono-Regular.ttf")


def font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", size)


F_TITLE = font(FONT_B, 22)
F_MODE = font(FONT_B, 12)
F_SMALL = font(FONT_R, 9)
F_SMALLB = font(FONT_B, 9)
F_HELP = font(FONT_R, 10)


# --------------------------------------------------------------------------
# Display
# --------------------------------------------------------------------------
class Framebuffer:
    def __init__(self, dev="/dev/fb0"):
        for i in range(4):
            try:
                with open(f"/sys/class/graphics/fb{i}/virtual_size") as f:
                    if f.read().strip() == f"{W},{H}":
                        dev = f"/dev/fb{i}"
                        break
            except OSError:
                pass
        self.f = open(dev, "r+b", buffering=0)

    def show(self, rgb):
        self.f.seek(0)
        self.f.write(to_rgb565(rgb))

    def clear(self):
        self.f.seek(0)
        self.f.write(b"\x00" * (W * H * 2))

    def close(self):
        try:
            self.clear()
        finally:
            self.f.close()


# --------------------------------------------------------------------------
# Keyboard
# --------------------------------------------------------------------------
KEY = {
    "LEFT": {105, 51},       # arrow / ','
    "RIGHT": {106, 53},      # arrow / '/'
    "UP": {103, 39},         # arrow / ';'
    "DOWN": {108, 52},       # arrow / '.'
    "SHOT": {57, 28},        # space / enter
    "CLIP": {47},            # v
    "SEED": {19},            # r
    "HELP": {35},            # h
    "HUD": {15},             # tab
    "ARM": {21},             # y
    "STACK": {31},           # s
    "UNSTACK": {46},         # c
    "QUIT": {1, 16},         # esc / q
}
NUM_KEYS = {2: 0, 3: 1, 4: 2, 5: 3, 6: 4, 7: 5, 8: 6, 9: 7}  # 1..8 -> mode index
WIRE_KEYS = {c: c - 2 for c in range(2, 11)}  # in BEND: 1..9 -> wire 0..8 (momentary)
REPEATABLE = {"UP", "DOWN", "LEFT", "RIGHT"}


def find_keyboard():
    dev = os.environ.get("GLITCHCAM_KBD")
    if dev:
        return [dev]
    found = []
    for i in range(16):
        try:
            with open(f"/sys/class/input/event{i}/device/name") as f:
                name = f.read().lower()
        except OSError:
            continue
        if "tca8418" in name or "glitchcam-test" in name:
            found.append(f"/dev/input/event{i}")
    return found or ["/dev/input/event3"]


class Keyboard(threading.Thread):
    def __init__(self, q):
        super().__init__(daemon=True)
        self.q = q
        self.devs = []

    def run(self):
        import evdev
        import select
        while True:
            self.devs = []
            try:
                for p in find_keyboard():
                    self.devs.append(evdev.InputDevice(p))
                for d in self.devs:
                    try:
                        d.grab()  # keep keys away from the launcher while we run
                    except OSError:
                        pass
                fds = {d.fd: d for d in self.devs}
                while True:
                    r, _, _ = select.select(list(fds), [], [])
                    for fd in r:
                        for ev in fds[fd].read():
                            if ev.type == 1:
                                self.q.put((ev.code, ev.value))
            except Exception:
                # device vanished or failed to open: close what we have, retry
                for d in self.devs:
                    try:
                        d.close()
                    except Exception:
                        pass
                time.sleep(0.5)


class FifoKeys(threading.Thread):
    """Test hook: GLITCHCAM_KEYFIFO=/path -> each line "<evdev code>" is a key press."""

    def __init__(self, q, path):
        super().__init__(daemon=True)
        self.q, self.path = q, path

    def run(self):
        if not os.path.exists(self.path):
            os.mkfifo(self.path)
        while True:
            with open(self.path) as f:
                for line in f:
                    line = line.strip()
                    if line.isdigit():
                        self.q.put((int(line), 1))
                    elif line.startswith("+") and line[1:].isdigit():
                        self.q.put((int(line[1:]), 1))  # press and hold
                    elif line.startswith("-") and line[1:].isdigit():
                        self.q.put((int(line[1:]), 0))  # release


def action_for(code, value):
    if value == 1 and code in NUM_KEYS:
        return ("MODE", NUM_KEYS[code])
    for name, codes in KEY.items():
        if code in codes:
            if value == 2 and name not in REPEATABLE:
                return None
            return (name, None)
    return None


# --------------------------------------------------------------------------
# Drawing helpers
# --------------------------------------------------------------------------
def spaced_text(draw, xy, text, fnt, fill, spacing=1):
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=fnt, fill=fill)
        x += draw.textlength(ch, font=fnt) + spacing
    return x


def glyph_text(draw, xy, text, fnt, fill=WHITE, spacing=1, split=1):
    """Chromatic-aberration title text: magenta left, blue right, white on top."""
    x, y = xy
    spaced_text(draw, (x - split, y), text, fnt, GLYPH1 + (255,), spacing)
    spaced_text(draw, (x + split, y), text, fnt, GLYPH2 + (255,), spacing)
    return spaced_text(draw, (x, y), text, fnt, fill + (255,) if len(fill) == 3 else fill, spacing)


def blend(frame, hud_rgb, hud_a):
    a = hud_a[..., None].astype(np.uint16)
    return ((frame.astype(np.uint16) * (255 - a) + hud_rgb.astype(np.uint16) * a) // 255).astype(np.uint8)


def load_icon(size):
    try:
        im = Image.open(os.path.join(APP_DIR, "icon.png")).convert("RGBA")
        return im.resize((size, size), Image.LANCZOS)
    except Exception:
        return None


def splash_image(status="starting sensor"):
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im, "RGBA")
    for y in range(0, H, 3):  # faint scanlines
        d.line([(0, y), (W, y)], fill=(255, 255, 255, 6))
    icon = load_icon(84)
    if icon:
        im.paste(icon, (14, 40), icon)
    glyph_text(d, (112, 48), "GLITCH", F_TITLE, WHITE, spacing=2, split=2)
    glyph_text(d, (112, 74), "CAM", F_TITLE, WHITE, spacing=2, split=2)
    spaced_text(d, (114, 108), "PHOTOS THAT MISBEHAVE", F_SMALL, MUTED + (255,), spacing=1)
    spaced_text(d, (114, 124), status.upper() + "_", F_SMALL, ACCENT + (255,), spacing=1)
    return np.asarray(im)


def splash(fb, status="starting sensor"):
    fb.show(splash_image(status))


def to_rgb565(rgb):
    r = rgb[..., 0].astype(np.uint16)
    g = rgb[..., 1].astype(np.uint16)
    b = rgb[..., 2].astype(np.uint16)
    return (((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)).astype("<u2").tobytes()


# --------------------------------------------------------------------------
# The app
# --------------------------------------------------------------------------
class App:
    def __init__(self):
        self.fb = Framebuffer()
        splash(self.fb)
        self.q = queue.Queue()
        self.kbd = Keyboard(self.q)
        self.kbd.start()
        if os.environ.get("GLITCHCAM_KEYFIFO"):
            FifoKeys(self.q, os.environ["GLITCHCAM_KEYFIFO"]).start()
        self.mode = 1
        self.levels = {m: 4 for m in MODES}
        self.levels["BEND"] = 2  # leaves wires 3-9 free to hold in by hand
        self.stack = []          # modes held on top of the current one
        self.seed = int.from_bytes(os.urandom(2), "little") % 10000
        self.show_hud = True
        self.show_help = False
        self.toast = None  # (text, until, colour)
        self.flash = 0
        self.running = True
        self.sensor = None
        self.sensor_armed = False
        self.mosher = glitch.Mosher()
        self.last_jpeg = None
        self.recording = None  # dict, see start_clip
        self.busy = []         # background job labels
        self.hud_key = None
        self.hud_cache = None
        self.last_raw = None
        self.last_mosh = None
        self.layers_key = None
        self.held_wires = set()
        self.jobs = []  # detached clip encoders (Popen) still running
        self.fps = 0.0
        os.makedirs(WORK_DIR, exist_ok=True)
        self.start_camera()

    # ---------------- state helpers
    @property
    def level(self):
        return self.levels[MODES[self.mode]]

    @level.setter
    def level(self, v):
        self.levels[MODES[self.mode]] = v

    def active_layers(self):
        """{mode: level} in pipeline order: what is actually applied now."""
        names = set(self.stack)
        if MODES[self.mode] != "CLEAN":
            names.add(MODES[self.mode])
        if not self.sensor_armed:
            names.discard("SENSOR")
        return {m: self.levels[m] for m in MODES if m in names}

    @staticmethod
    def layers_name(layers):
        if not layers:
            return "CLEAN"
        if len(layers) == 1:
            (m, lv), = layers.items()
            return f"{m}_L{lv}"
        return "-".join(f"{m}{lv}" for m, lv in layers.items())

    # ---------------- camera
    def start_camera(self):
        from libcamera import Transform
        from picamera2 import Picamera2
        self.cam = Picamera2()
        flip = os.environ.get("GLITCHCAM_FLIP", "1") == "1"
        cfg = self.cam.create_video_configuration(
            main={"size": (640, 480), "format": "YUV420"},
            raw={"size": (1640, 1232), "format": "SRGGB10_CSI2P"},
            buffer_count=4,
            transform=Transform(hflip=1, vflip=1) if flip else Transform(),
            controls={"FrameRate": 30},
        )
        self.cam.configure(cfg)
        rc = self.cam.camera_configuration()["raw"]
        self.raw_w, self.raw_h = rc["size"]
        self.raw_stride = rc["stride"]
        fmt = rc["format"]
        self.order = fmt[1:5] if fmt[1:5] in ("BGGR", "RGGB", "GRBG", "GBRG") else "RGGB"
        self.cam.start()

    def restart_camera(self):
        try:
            if self.recording:
                self.stop_clip()
            self.cam.stop()
        except Exception:
            pass
        if self.sensor:
            self.sensor.forget()  # driver rewrote every register on restart
        self.cam.start()

    def grab(self):
        job = self.cam.capture_request(wait=False)
        try:
            req = self.cam.wait(job, timeout=2.0)
        except Exception:
            self.sensor_armed = False
            self.set_toast("SENSOR STALLED - RESTARTED", GLYPH1, 3)
            self.restart_camera()
            return None
        try:
            arr = req.make_array("raw")
            md = req.get_metadata()
        finally:
            req.release()
        gains = md.get("ColourGains", (1.6, 1.6))
        return glitch.RawFrame(arr, self.raw_w, self.raw_h, self.raw_stride, gains, self.order)

    # ---------------- sensor bending
    def sensor_presets(self):
        import sensorbend
        return sensorbend.presets(self.seed)

    def sync_sensor(self):
        want = "SENSOR" in self.active_layers()
        if not want:
            if self.sensor and self.sensor.active_label:
                self.sensor.restore()
            return
        try:
            if self.sensor is None:
                import sensorbend
                self.sensor = sensorbend.SensorBend()
            label, pokes = self.sensor_presets()[self.levels["SENSOR"] - 1]
            if self.sensor.active_label != label:
                self.sensor.apply(label, pokes)
        except Exception as e:  # noqa: BLE001
            self.sensor_armed = False
            self.set_toast("I2C: " + str(e)[:30], GLYPH1, 4)

    # ---------------- input
    def set_toast(self, text, colour=ACCENT, secs=1.6):
        self.toast = (text, time.time() + secs, colour)

    def bend_held(self):
        return tuple(sorted(self.held_wires))

    def bend_label(self):
        if "BEND" not in self.active_layers():
            return ""
        return " ".join(lbl for lbl, _ in glitch.bend_ops(self.levels["BEND"], self.held_wires, self.seed))

    def handle(self, code, value):
        if MODES[self.mode] == "BEND" and code in WIRE_KEYS and not self.show_help:
            if value == 1:
                self.held_wires.add(WIRE_KEYS[code])
            elif value == 0:
                self.held_wires.discard(WIRE_KEYS[code])
            return
        if value == 0:
            if code in WIRE_KEYS:
                self.held_wires.discard(WIRE_KEYS[code])
            return
        act = action_for(code, value)
        if not act:
            return
        name, arg = act
        if name == "QUIT":
            self.running = False
            return
        if self.show_help:
            self.show_help = False
            return
        cur = MODES[self.mode]
        if name == "HELP":
            self.show_help = True
        elif name == "HUD":
            self.show_hud = not self.show_hud
        elif name in ("LEFT", "RIGHT", "MODE"):
            if name == "MODE":
                self.mode = arg
            else:
                self.mode = (self.mode + (1 if name == "RIGHT" else -1)) % len(MODES)
            if MODES[self.mode] != "SENSOR" and "SENSOR" not in self.stack:
                self.sensor_armed = False
        elif name in ("UP", "DOWN"):
            self.level = max(1, min(9, self.level + (1 if name == "UP" else -1)))
        elif name == "SEED":
            self.seed = int.from_bytes(os.urandom(2), "little") % 10000
            self.mosher.reset()
            self.set_toast("SEED #%04d" % self.seed)
        elif name == "ARM" and (cur == "SENSOR" or "SENSOR" in self.stack):
            self.sensor_armed = not self.sensor_armed
            self.set_toast("SENSOR ARMED" if self.sensor_armed else "SENSOR SAFE",
                           GLYPH1 if self.sensor_armed else ACCENT)
        elif name == "STACK":
            if cur == "CLEAN":
                self.set_toast("PICK A GLITCH TO STACK", GLYPH1)
            elif cur in self.stack:
                self.stack.remove(cur)
                self.set_toast("UNSTACKED " + cur)
            else:
                self.stack.append(cur)
                self.set_toast("STACKED " + "+".join(self.stack), GLYPH2)
        elif name == "UNSTACK":
            self.stack = []
            if cur != "SENSOR":
                self.sensor_armed = False
            self.set_toast("STACK CLEARED")
        elif name == "SHOT":
            self.shoot()
        elif name == "CLIP":
            if self.recording:
                self.stop_clip()
            else:
                self.start_clip()

    # ---------------- stills
    def stamp(self):
        return time.strftime("%Y%m%d-%H%M%S")

    def shoot(self):
        raw = self.last_raw
        if raw is None:
            return
        layers, seed, held = self.active_layers(), self.seed, self.bend_held()
        base = os.path.join(SAVE_DIR, f"GC_{self.stamp()}_{self.layers_name(layers)}_S{seed:04d}")
        mosh = self.last_mosh.copy() if ("MOSH" in layers and self.last_mosh is not None) else None
        self.flash = 2

        def work():
            self.busy.append("DEV")
            try:
                if mosh is not None:
                    # MOSH lives in the viewfinder's frame history: save that, 2x
                    img = Image.fromarray(mosh).resize((mosh.shape[1] * 2, mosh.shape[0] * 2), Image.NEAREST)
                    path = base + ".png"
                    img.save(path)
                else:
                    qx, qy = glitch.full_grid(raw)
                    raw_layers = {k: v for k, v in layers.items() if k in glitch.RAW_LAYERS}
                    if "BEND" in raw_layers:
                        raw_layers["BEND"] = (raw_layers["BEND"], held)
                    img = glitch.decode(raw, qx, qy, raw_layers, seed)
                    data = None
                    if "JPEG" in layers:
                        for k in range(8):
                            _, data = glitch.corrupt_jpeg(img, layers["JPEG"], seed + k, quality=88)
                            if data:
                                break
                    if data:
                        path = base + ".jpg"
                        with open(path, "wb") as f:
                            f.write(data)  # the damaged JPEG bytes themselves
                    else:
                        path = base + ".png"
                        Image.fromarray(img).save(path, compress_level=1)
                self.set_toast("SAVED " + os.path.basename(path)[3:22], ACCENT, 2)
            except Exception as e:  # noqa: BLE001
                self.set_toast("SAVE FAILED: " + str(e)[:24], GLYPH1, 3)
            finally:
                self.busy.remove("DEV")

        threading.Thread(target=work, daemon=True).start()

    # ---------------- clips
    def start_clip(self):
        layers, seed = self.active_layers(), self.seed
        name = f"GC_{self.stamp()}_{self.layers_name(layers)}_S{seed:04d}"
        rec = {"t0": time.time(), "layers": layers, "seed": seed, "frames": 0,
               "out": os.path.join(SAVE_DIR, name + ".mp4")}
        if set(layers) == {"MOSH"}:
            # real codec datamosh: hardware H.264 of the camera, keyframes cut later
            from picamera2.encoders import H264Encoder
            from picamera2.outputs import FileOutput
            rec["kind"] = "codec"
            rec["src"] = os.path.join(WORK_DIR, name + ".h264")
            rec["enc"] = H264Encoder(bitrate=4_000_000, repeat=True, iperiod=12)
            self.cam.start_encoder(rec["enc"], FileOutput(rec["src"]), name="main")
        else:
            # record exactly what the glitch pipeline renders (no HUD), raw to disk;
            # encoding happens after stop so the viewfinder never fights an encoder
            rec["kind"] = "render"
            rec["src"] = os.path.join(WORK_DIR, name + ".rgb")
            rec["fh"] = open(rec["src"], "wb", buffering=1 << 20)
        self.recording = rec

    def feed_clip(self, frame):
        rec = self.recording
        if rec and rec["kind"] == "render":
            try:
                rec["fh"].write(np.ascontiguousarray(frame).tobytes())
                rec["frames"] += 1
            except OSError as e:
                self.set_toast("REC FAILED: " + str(e)[:24], GLYPH1, 3)
                self.stop_clip()

    def check_clip_limits(self):
        rec = self.recording
        if not rec:
            return
        if time.time() - rec["t0"] > CLIP_MAX:
            self.stop_clip()
            return
        if int(time.time() * 2) != rec.get("dchk"):
            rec["dchk"] = int(time.time() * 2)
            st = os.statvfs(WORK_DIR)
            if st.f_bavail * st.f_frsize < 300 * 1024 * 1024:
                self.set_toast("DISK NEARLY FULL - STOPPED", GLYPH1, 3)
                self.stop_clip()

    def stop_clip(self):
        """Stop recording and hand the clip to a detached encoder process.

        The encoder runs in its own session, so quitting the app never waits
        for it and never loses the clip.
        """
        rec, self.recording = self.recording, None
        if rec is None:
            return
        secs = max(0.1, time.time() - rec["t0"])
        if rec["kind"] == "codec":
            try:
                self.cam.stop_encoder(rec["enc"])
            except Exception:
                pass
            cmd = [sys.executable, os.path.join(APP_DIR, "h264mosh.py"), rec["src"],
                   rec["out"], str(rec["layers"]["MOSH"]), str(rec["seed"])]
        else:
            rec["fh"].close()
            if rec["frames"] < 2:
                os.unlink(rec["src"])
                self.set_toast("CLIP TOO SHORT", GLYPH1)
                return
            fps = rec["frames"] / secs
            cmd = [sys.executable, os.path.join(APP_DIR, "clipenc.py"), rec["src"],
                   rec["out"], str(W), str(H), "%.3f" % fps]
        proc = subprocess.Popen(cmd, start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.jobs.append(proc)
        self.set_toast("CLIP %d:%02d - ENCODING" % (secs // 60, secs % 60), ACCENT, 2.5)

    def poll_jobs(self):
        for p in list(self.jobs):
            rc = p.poll()
            if rc is not None:
                self.jobs.remove(p)
                self.set_toast("CLIP SAVED" if rc == 0 else "CLIP ENCODE FAILED",
                               ACCENT if rc == 0 else GLYPH1, 2.5)

    # ---------------- HUD
    def hud(self):
        mode = MODES[self.mode]
        now = time.time()
        toast = self.toast if self.toast and self.toast[1] > now else None
        rec = None
        if self.recording:
            rec = now - self.recording["t0"]
        sensor_label = None
        if mode == "SENSOR":
            sensor_label = self.sensor_presets()[self.level - 1][0]
        key = (mode, self.level, self.seed, self.show_hud, self.show_help,
               toast[0] if toast else None, None if rec is None else int(rec * 4),
               self.sensor_armed, tuple(self.busy), len(self.jobs), sensor_label, tuple(self.stack),
               self.bend_label())
        if key == self.hud_key:
            return self.hud_cache
        im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(im, "RGBA")
        if self.show_hud:
            d.rectangle([0, 0, W, 17], fill=BG + (190,))
            d.line([(0, 18), (W, 18)], fill=DIM + (255,))
            x = glyph_text(d, (6, 1), mode, F_MODE, WHITE, spacing=2)
            desc = sensor_label if mode == "SENSOR" and self.sensor_armed else MODE_INFO[mode]
            if mode == "SENSOR" and not self.sensor_armed:
                desc = "SAFE - PRESS Y TO ARM"
            if mode == "BEND":
                desc = self.bend_label() or desc
            desc = desc.upper()
            others = [m for m in self.stack if m != mode]
            if others:
                x = spaced_text(d, (x + 4, 4), "+" + "+".join(others), F_SMALLB, GLYPH2 + (255,), 0)
            room = W - 40 - (x + 8)
            while desc and d.textlength(desc, font=F_SMALL) > room:
                desc = desc[:-1]
            spaced_text(d, (x + 8, 4), desc, F_SMALL, MUTED + (255,), 0)
            if rec is not None:
                if int(rec * 2) % 2 == 0:
                    d.ellipse([W - 62, 5, W - 54, 13], fill=GLYPH1 + (255,))
                spaced_text(d, (W - 50, 3), "REC %d:%02d" % (rec // 60, rec % 60), F_SMALLB, GLYPH1 + (255,), 1)
            else:
                spaced_text(d, (W - 30, 3), "%d/%d" % (self.mode + 1, len(MODES)), F_SMALLB, ACCENT + (255,), 1)
            # bottom bar: intensity meter, seed, hints
            d.rectangle([0, H - 16, W, H], fill=BG + (190,))
            d.line([(0, H - 17), (W, H - 17)], fill=DIM + (255,))
            spaced_text(d, (6, H - 14), "INT", F_SMALLB, MUTED + (255,), 1)
            for i in range(9):
                x0 = 30 + i * 9
                col = (GLYPH1 if i == self.level - 1 else ACCENT) if i < self.level else DIM
                d.rectangle([x0, H - 12, x0 + 6, H - 5], fill=col + (255,))
            spaced_text(d, (116, H - 14), "#%04d" % self.seed, F_SMALLB, ACCENT + (255,), 1)
            jobs = self.busy + (["ENC"] * min(1, len(self.jobs)))
            hint = "  ".join(jobs) + " ..." if jobs else "SPC SHOT  V CLIP  H HELP"
            spaced_text(d, (168, H - 14), hint, F_SMALL, MUTED + (255,), 0)
            if "SENSOR" in self.active_layers():
                d.rectangle([0, 19, 2, H - 18], fill=GLYPH1 + (255,))
                d.rectangle([W - 3, 19, W, H - 18], fill=GLYPH1 + (255,))
        if mode == "SENSOR" and not self.sensor_armed and not self.show_help:
            d.rectangle([30, 40, W - 30, 128], fill=BG + (225,), outline=GLYPH1 + (255,))
            glyph_text(d, (40, 44), "EXPERIMENTAL", F_MODE, WHITE, spacing=2)
            lines = ["Writes IMX219 registers over I2C.",
                     "Worst case the sensor stalls and",
                     "GlitchCam restarts it. Nothing is",
                     "written persistently.",
                     "[Y] ARM     [,/] OTHER MODE"]
            for i, t in enumerate(lines):
                d.text((40, 62 + i * 12), t, font=F_SMALL,
                       fill=(ACCENT if i == 4 else WHITE) + (255,))
        if toast:
            tw = d.textlength(toast[0], font=F_SMALLB) + 16
            x0 = (W - tw) // 2
            d.rectangle([x0, H - 38, x0 + tw, H - 22], fill=BG + (230,), outline=toast[2] + (255,))
            d.text((x0 + 8, H - 36), toast[0], font=F_SMALLB, fill=toast[2] + (255,))
        if self.show_help:
            d.rectangle([0, 0, W, H], fill=BG + (238,))
            glyph_text(d, (8, 3), "GLITCH CAM", F_MODE, WHITE, spacing=2)
            rows = [
                (",  /   or  < >", "glitch mode   (1-8 jump)"),
                (";  .   or  ^ v", "intensity 1-9 (per mode)"),
                ("SPACE / ENTER", "shoot still"),
                ("V", "start/stop video clip"),
                ("S  /  C", "stack mode on top / clear stack"),
                ("R", "new seed (same seed = same glitch)"),
                ("1-9 (in BEND)", "hold = connect that wire"),
                ("Y  TAB  ESC/Q", "arm SENSOR  hide HUD  quit"),
            ]
            for i, (k, v) in enumerate(rows):
                d.text((8, 22 + i * 15), k, font=F_SMALLB, fill=ACCENT + (255,))
                d.text((108, 22 + i * 15), v, font=F_SMALL, fill=WHITE + (255,))
            d.text((8, H - 14), "saves to " + SAVE_DIR.replace(os.path.expanduser("~"), "~"),
                   font=F_SMALL, fill=MUTED + (255,))
        arr = np.asarray(im)
        rgb, a = arr[..., :3].copy(), arr[..., 3].copy()
        self.hud_key, self.hud_cache = key, (rgb, a, bool(a.any()))
        return self.hud_cache

    # ---------------- frame
    def frame(self):
        raw = self.grab()
        if raw is None:
            return None
        self.last_raw = raw
        layers = self.active_layers()
        if "BEND" in layers:
            layers["BEND"] = (layers["BEND"], self.bend_held())
        key = tuple(k for k in layers)
        if key != self.layers_key:
            self.layers_key = key
            self.mosher.reset()
        if "MOSH" in layers:
            qx, qy = glitch.grid(raw, W, 240, aspect_crop=False)
        else:
            qx, qy = glitch.grid(raw, W, H)
        img, _ = glitch.render_stack(raw, qx, qy, layers, self.seed, fallback=self.last_jpeg)
        if "JPEG" in layers:
            self.last_jpeg = img
        if "MOSH" in layers:
            img = self.mosher.step(img, layers["MOSH"], self.seed)
            self.last_mosh = img
            y0 = (img.shape[0] - H) // 2
            img = img[y0:y0 + H]
            if img.shape[1] < W:
                img = np.pad(img, ((0, 0), (0, W - img.shape[1]), (0, 0)))
            img = np.ascontiguousarray(img[:, :W])
        return img

    def run(self):
        t_last = time.time()
        while self.running:
            while True:
                try:
                    self.handle(*self.q.get_nowait())
                except queue.Empty:
                    break
            if not self.running:
                break
            self.sync_sensor()
            self.check_clip_limits()
            self.poll_jobs()
            f = self.frame()
            if f is None:
                continue
            if self.recording:
                self.feed_clip(f)
            if self.flash:
                f = 255 - f
                self.flash -= 1
            rgb, a, any_a = self.hud()
            if any_a:
                f = blend(f, rgb, a)
            self.fb.show(f)
            now = time.time()
            self.fps = 0.9 * self.fps + 0.1 / max(1e-3, now - t_last)
            t_last = now
            if DEBUG and int(now) % 3 == 0 and int(now) != getattr(self, "_dbg", 0):
                self._dbg = int(now)
                print("fps %.1f layers %s" % (self.fps, self.active_layers()), flush=True)

    def close(self):
        try:
            if self.recording:
                self.stop_clip()
        except Exception:
            pass
        if self.sensor:
            try:
                self.sensor.close()
            except Exception:
                pass
        try:
            self.cam.stop()
            self.cam.close()
        except Exception:
            pass
        # let background saves / clip encodes finish before handing back the screen
        t0 = time.time()
        while self.busy and time.time() - t0 < 60:
            splash(self.fb, "saving %s %ds" % ("+".join(self.busy).lower(), time.time() - t0))
            time.sleep(0.5)
        self.fb.close()


def main():
    if DEBUG:
        import faulthandler
        faulthandler.register(signal.SIGUSR1, all_threads=True)
    if "--render-splash" in sys.argv:
        with open(sys.argv[sys.argv.index("--render-splash") + 1], "wb") as f:
            f.write(to_rgb565(splash_image("loading camera")))
        return
    signal.signal(signal.SIGTERM, lambda *a: (_ for _ in ()).throw(SystemExit(0)))
    signal.signal(signal.SIGHUP, lambda *a: (_ for _ in ()).throw(SystemExit(0)))
    app = None
    try:
        app = App()
        app.run()
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        try:
            fb = app.fb if app else Framebuffer()
            im = Image.new("RGB", (W, H), BG)
            d = ImageDraw.Draw(im)
            d.text((8, 8), "GLITCHCAM ERROR", font=F_MODE, fill=GLYPH1)
            msg = str(e)
            for i in range(0, min(len(msg), 250), 46):
                d.text((8, 30 + i // 46 * 12), msg[i:i + 46], font=F_SMALL, fill=WHITE)
            fb.show(np.asarray(im))
            time.sleep(5)
        except Exception:
            pass
    finally:
        if app:
            try:
                app.close()
            except BaseException:  # noqa: BLE001
                pass
        os._exit(0)


if __name__ == "__main__":
    main()
