#!/usr/bin/env python3
"""Render the OpenBox promotional video.

Generates ``build/promo/openbox-promo-1080p.mp4`` entirely from local assets:
the real library screenshots, the official logo, and the design tokens from
``static/app.css``. Frames are composited with Pillow and piped as raw RGB
into ffmpeg (libx264); the ambient score is synthesized with numpy.

Run with the project dev venv (needs Pillow + numpy):

    .venv-dev/bin/python scripts/make_promo_video.py
    .venv-dev/bin/python scripts/make_promo_video.py --probe   # sample stills
"""

from __future__ import annotations

import argparse
import math
import subprocess
import sys
import time
import wave
from pathlib import Path

try:
    import numpy as np
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont
except ImportError:
    sys.exit("Pillow + numpy are required: .venv-dev/bin/pip install numpy pillow")

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "build" / "promo"

# ---------------------------------------------------------------- tokens ---
# Mirrors of static/app.css :root - the shared visual contract.
BG = (15, 14, 12)          # --bg #0F0E0C
BG_DEEP = (11, 10, 9)      # --surface-deep #0B0A09
SHEET = (23, 21, 19)       # --surface-sheet base
FIELD = (38, 34, 29)       # --surface-field #26221D
CARD = (31, 28, 24)        # --surface-card #1F1C18
LINE = (58, 52, 44)        # --line #3A342C
BORDER_CARD = (94, 83, 69)  # --border-card #5E5345
TEXT = (246, 240, 231)     # --text #F6F0E7
MUTED = (180, 168, 151)    # --muted #B4A897
SOFT = (207, 194, 177)     # --text-soft #CFC2B1
FAINT = (164, 152, 139)    # --section-muted #A89B8B
BRAND = (255, 122, 26)     # --brand #FF7A1A
CYAN = (95, 211, 196)      # --cyan #5FD3C4
GOLD = (242, 193, 78)      # --gold #F2C14E
BLACK = (0, 0, 0)

W, H = 1920, 1080
FPS = 30
DUR = 55.0
CROSS = 0.72               # scene crossfade, seconds
FADE_IN = 0.9
FADE_OUT = 1.4

FONTS = Path("/usr/share/fonts/opentype/inter")
ASSETS = ROOT / "assets"

AMB = None       # set in main(): AmbientGlows
EMBERS = None    # set in main(): Embers
SCRIM = None     # set in main(): bottom scrim for captioned scenes
WHITE = None     # set in main(): full-frame white reuse for the card sweep


# ---------------------------------------------------------------- helpers ---


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def ease_in_out(t: float) -> float:
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    return t * t * (3.0 - 2.0 * t)


def ease_out(t: float) -> float:
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    return 1.0 - (1.0 - t) ** 3


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def win(t: float, a: float, b: float) -> float:
    if b <= a:
        return 0.0 if t < a else 1.0
    return clamp((t - a) / (b - a), 0.0, 1.0)


def f_display(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / "InterDisplay-Black.otf"), size)


def f_semi(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / "InterDisplay-SemiBold.otf"), size)


def f_medium(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / "InterDisplay-Medium.otf"), size)


def tracked_width(font: ImageFont.FreeTypeFont, text: str, track: int) -> int:
    w = sum(font.getlength(ch) for ch in text)
    return int(round(w + track * max(0, len(text) - 1)))


def draw_tracked(draw: ImageDraw.ImageDraw, x: float, y: float, text: str,
                 font: ImageFont.FreeTypeFont, track: int, fill) -> None:
    cx = float(x)
    for ch in text:
        draw.text((cx, y), ch, font=font, fill=fill)
        cx += font.getlength(ch) + track


def fade_paste(dst: Image.Image, layer: Image.Image, pos: tuple[int, int], alpha: float) -> None:
    if alpha <= 0.003:
        return
    x, y = pos
    if alpha >= 0.997:
        dst.paste(layer, (x, y), layer)
        return
    mask = layer.getchannel("A").point(lambda v, a=alpha: int(v * a))
    dst.paste(layer, (x, y), mask)


def radial_glow(size: int, color, alpha: float = 255) -> Image.Image:
    """Soft circular glow sprite (RGBA)."""
    base = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(base)
    steps = 26
    for i in range(steps, 0, -1):
        r = size * 0.5 * i / steps
        a = int(alpha * (1.0 - i / (steps + 1)) ** 2.2)
        d.ellipse([size * 0.5 - r, size * 0.5 - r, size * 0.5 + r, size * 0.5 + r],
                  fill=a)
    spr = Image.new("RGBA", (size, size), color + (0,))
    spr.putalpha(base)
    return spr


def hashfrac(i: float, salt: float = 0.0) -> float:
    """Deterministic [0,1) scatter for particle fields."""
    v = math.sin((i + salt) * 12.9898) * 43758.5453
    return v - math.floor(v)


def make_bg() -> Image.Image:
    """Warm vignette gradient background built from the real tokens."""
    vert = Image.new("L", (1, H), 0)
    for y in range(H):
        t = y / (H - 1)
        vert.putpixel((0, y), int(BG[0] + (BG_DEEP[0] - BG[0]) * t))
    col = Image.new("RGB", (W, H), BG)
    deep = Image.new("RGB", (W, H), BG_DEEP)
    frame = Image.composite(deep, col, vert.resize((W, H)))

    # subtle warm wash from the bottom center
    glow = radial_glow(1400, BRAND, alpha=255)
    g = glow.getchannel("A").point(lambda v: int(v * 0.05))
    wash = Image.new("L", (W, H), 0)
    wash.paste(g, ((W - 1400) // 2, H - 980))
    warm = Image.new("RGB", (W, H), BRAND)
    frame = Image.composite(warm, frame, wash)

    # vignette: darken edges
    vig = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(vig)
    d.ellipse([-W * 0.25, -H * 0.30, W * 1.25, H * 1.30], fill=255)
    vig = vig.filter(ImageFilter.GaussianBlur(240))
    dark = Image.new("RGB", (W, H), BG_DEEP)
    dark_a = ImageChops.invert(vig).point(lambda v: int(v * 0.30))
    frame = Image.composite(dark, frame, dark_a)
    return frame


def build_scrim() -> Image.Image:
    """Bottom gradient scrim composited over screenshot scenes."""
    sc = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(sc)
    top = 620
    for y in range(top, H):
        k = (y - top) / (H - top)
        a = int(205 * clamp((k - 0.10) / 0.90, 0.0, 1.0) ** 1.3)
        d.line([0, y, W, y], fill=(10, 9, 8, a))
    # gentle left bias for the caption
    for x in range(0, 620):
        a = int(45 * (1.0 - x / 620) ** 1.6)
        d.line([x, top, x, H], fill=(8, 7, 6, a))
    return sc


def build_grain(count: int = 8) -> list[Image.Image]:
    """Pre-seeded noise stacks for an animated film-grain overlay."""
    rng = np.random.default_rng(5)
    tiles = []
    for _ in range(count):
        noise = rng.integers(-9, 10, (H, W, 1), dtype=np.int16)
        base = rng.integers(-18, 19, (H, W, 3), dtype=np.int16)
        tiles.append(base + noise)
    return tiles


# ------------------------------------------------------------- screenshot ---

def build_screenshot_card(path: Path, disp_w: int, disp_h: int, hi_scale: float,
                          radius: int = 18) -> tuple[Image.Image, Image.Image]:
    """Return (display_card_rgba, hires_canvas_rgba) ready for Ken Burns."""
    src = Image.open(path).convert("RGB")
    hi_w, hi_h = int(disp_w * hi_scale), int(disp_h * hi_scale)

    ar_d = disp_w / disp_h
    ar_s = src.width / src.height
    if ar_s > ar_d:
        crop_h = src.height
        crop_w = int(crop_h * ar_d)
        x0 = (src.width - crop_w) // 2
        box = (x0, 0, x0 + crop_w, crop_h)
    else:
        crop_w = src.width
        crop_h = int(crop_w / ar_d)
        y0 = int((src.height - crop_h) * 0.45)
        box = (0, y0, crop_w, y0 + crop_h)
    hi = src.crop(box).resize((hi_w, hi_h), Image.LANCZOS).convert("RGBA")

    disp = Image.new("RGBA", (disp_w, disp_h))
    disp.paste(hi.resize((disp_w, disp_h), Image.LANCZOS), (0, 0))

    mask = Image.new("L", (disp_w, disp_h), 0)
    dm = ImageDraw.Draw(mask)
    dm.rounded_rectangle([0, 0, disp_w - 1, disp_h - 1], radius=radius, fill=255)
    disp.putalpha(mask)
    hi_mask = mask.resize((hi_w, hi_h), Image.BICUBIC)
    hi.putalpha(hi_mask)

    for canvas, rad in ((disp, radius), (hi, int(radius * hi_scale))):
        db = ImageDraw.Draw(canvas)
        db.rounded_rectangle([0, 0, canvas.width - 1, canvas.height - 1], radius=rad,
                             outline=BORDER_CARD + (255,), width=1)
    return disp, hi


def card_sweep_band(h_disp: int) -> Image.Image:
    """Diagonal highlight band used for a one-pass glass sweep."""
    sw = Image.new("L", (620, h_disp), 0)
    d = ImageDraw.Draw(sw)
    for i in range(620):
        a = int(30 * max(0.0, 1.0 - abs(i - 310) / 300.0) ** 2)
        d.line([i, 0, i, h_disp], fill=a)
    sw = sw.rotate(13, expand=True, resample=Image.BILINEAR)
    band = Image.new("RGBA", sw.size, (255, 249, 240, 0))
    band.putalpha(sw)
    return band


# --------------------------------------------------------------- caption ---

class Caption:
    """Panel-based caption block pasted over screenshot cards."""

    def __init__(self, kicker: str, headline: str, sub: str, chips: list[str],
                 max_w: int = 1160):
        pad = 30
        kick_font = f_semi(29)
        sub_font = f_medium(33)
        chip_font = f_semi(25)

        kick_text = kicker.upper()
        kick_w = tracked_width(kick_font, kick_text, 5)
        sub_w = int(sub_font.getlength(sub))
        chips_w = self._chips_total_w(chips, chip_font)

        # pick the largest headline size that fits max_w
        head = None
        for size in (88, 80, 72, 64):
            hf = f_display(size)
            if int(hf.getlength(headline)) + pad * 2 <= max_w:
                head = (size, hf, int(hf.getlength(headline)))
                break
        if head is None:  # extreme fallback: shrink text itself
            hf = f_display(64)
            head = (64, hf, int(hf.getlength(headline)) + pad * 2)
        head_size, head_font, head_w = head

        content_w = max(head_w, sub_w, kick_w + 40, chips_w)
        self.width = min(max_w, content_w + pad * 2 + 14)
        kick_h = 40
        head_h = int(head_size * 1.16)
        sub_h = 52
        gap1, gap2, gap3 = 16, 12, 24
        chip_h = 46
        rows_chips = 1
        self.height = pad + kick_h + gap1 + head_h + gap2 + sub_h + gap3 + \
            rows_chips * chip_h + pad

        # ---- baked static panel: fill, borders, accent, kicker ----
        self.panel = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        dl = ImageDraw.Draw(self.panel)
        dl.rounded_rectangle([0, 0, self.width - 1, self.height - 1], radius=16,
                             fill=SHEET + (222,), outline=LINE + (150,), width=1)
        dl.rounded_rectangle([0, 0, 5, self.height - 1], radius=2, fill=BRAND + (235,))
        y = pad
        underline_w = int(kick_w + 12)
        draw_tracked(dl, pad + 12, y, kick_text, kick_font, 5, GOLD)
        dl.line([pad + 12, y + kick_h, pad + 12 + underline_w, y + kick_h],
                fill=LINE + (160,), width=1)
        self.head_pos = (pad + 12, y + kick_h + gap1)
        self.sub_pos = (pad + 12, self.head_pos[1] + head_h + gap2)
        self.chip_base = (pad + 12, self.sub_pos[1] + sub_h + gap3)

        # ---- separate layers that animate in ----
        self.head_layer = Image.new("RGBA", (self.width, head_h + 4), (0, 0, 0, 0))
        ImageDraw.Draw(self.head_layer).text((0, -4), headline, font=head_font, fill=TEXT)
        self.sub_layer = Image.new("RGBA", (sub_w + 6, sub_h), (0, 0, 0, 0))
        ImageDraw.Draw(self.sub_layer).text((0, 6), sub, font=sub_font, fill=SOFT)
        self.chip_layers = []
        cx = self.chip_base[0]
        for label in chips:
            tw = int(chip_font.getlength(label))
            img = Image.new("RGBA", (tw + 40, chip_h), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rounded_rectangle([0, 0, tw + 39, chip_h - 1], radius=chip_h // 2,
                                fill=FIELD + (178,), outline=BORDER_CARD + (200,), width=1)
            d.text((20, 9), label, font=chip_font, fill=(214, 204, 190))
            self.chip_layers.append((img, (cx, self.chip_base[1])))
            cx += img.width + 14

    @staticmethod
    def _chips_total_w(chips: list[str], font: ImageFont.FreeTypeFont) -> int:
        if not chips:
            return 0
        return sum(int(font.getlength(c)) + 40 for c in chips) + 14 * (len(chips) - 1)

    def paint(self, frame: Image.Image, x: int, y: int, t: float) -> None:
        base = ease_out(win(t, 0.08, 0.60))
        if base > 0:
            off = int((1.0 - base) * 20)
            fade_paste(frame, self.panel, (x, y + off), base)
        head_a = ease_out(win(t, 0.32, 0.70))
        if head_a > 0:
            off = int((1.0 - head_a) * 12)
            fade_paste(frame, self.head_layer, (x + self.head_pos[0],
                                                y + self.head_pos[1] + off), head_a)
        sub_a = ease_out(win(t, 0.50, 0.88))
        fade_paste(frame, self.sub_layer, (x + self.sub_pos[0], y + self.sub_pos[1]), sub_a)
        for i, (img, (cpx, cpy)) in enumerate(self.chip_layers):
            a = ease_out(win(t, 0.62 + i * 0.09, 0.94 + i * 0.09))
            fade_paste(frame, img, (x + cpx, y + cpy), a)


# ------------------------------------------------------------- atmosphere ---

class Embers:
    """Warm rising ember particles for intro/hook/outro scenes."""

    def __init__(self, count: int = 80):
        self.parts = []
        for i in range(count):
            f1, f2, f3 = hashfrac(i), hashfrac(i, 1), hashfrac(i, 2)
            self.parts.append({
                "x": f1, "y": f2,
                "s": (10, 16, 22, 32)[int(f3 * 4) % 4],
                "v": 14 + (f2 - f3) * 26 + 8,
                "w": 0.8 + f1 * 1.6,
                "ph": f3 * math.tau,
            })
        self.sprites = {}
        self.sizes = (10, 16, 22, 32)
        for size in self.sizes:
            for tier in range(6):
                self.sprites[(size, tier)] = radial_glow(size, (255, 150, 60),
                                                         alpha=int(30 + tier * 32))

    def paint(self, frame: Image.Image, t: float, spread: float = 1.0) -> None:
        span = H * 1.22
        for p in self.parts:
            y = p["y"] * span + span - (p["v"] * t) % span
            if y > H + 40:
                continue
            x = (p["x"] * 1.22 - 0.11) * W * spread + math.sin(t * p["w"] + p["ph"]) * 24
            a = 0.35 + 0.6 * (0.5 + 0.5 * math.sin(t * 1.55 + p["ph"] * 2.0))
            tier = min(5, int(a * 6))
            spr = self.sprites[(p["s"], tier)]
            frame.paste(spr, (int(x) - spr.width // 2, int(y) - spr.height // 2), spr)


class AmbientGlows:
    """Huge slow-drifting brand glows for empty-background scenes."""

    def __init__(self):
        self.orange = radial_glow(900, BRAND, alpha=255)
        self.teal = radial_glow(720, CYAN, alpha=255)

    def paint(self, frame: Image.Image, t: float) -> None:
        ox = 180 + math.sin(t * 0.21) * 90
        oy = -320 + math.cos(t * 0.16) * 46
        tx = 1560 + math.cos(t * 0.18) * 80
        ty = 820 + math.sin(t * 0.13) * 56

        o = self.orange.getchannel("A").point(
            lambda v: int(v * (0.044 + 0.010 * math.sin(t * 0.5))))
        o_img = self.orange.copy()
        o_img.putalpha(o)
        frame.paste(o_img, (int(ox) - 450, int(oy) - 450), o_img)

        t_img = self.teal.copy()
        ta = self.teal.getchannel("A").point(
            lambda v: int(v * (0.05 + 0.012 * math.sin(t * 0.42 + 2.0))))
        t_img.putalpha(ta)
        frame.paste(t_img, (int(tx) - 360, int(ty) - 360), t_img)


# ----------------------------------------------------------------- scenes ---

def scene_local(t: float, dur: float) -> float:
    return clamp(t, 0.0, max(dur, 0.001))


class Scene:
    start: float = 0.0
    end: float = 0.0

    def local(self, t: float) -> float:
        return scene_local(t, self.end - self.start)

    def render(self, t: float) -> Image.Image:
        raise NotImplementedError


class IntroScene(Scene):
    def __init__(self):
        self.mark = Image.open(ASSETS / "openbox-logo.png").convert("RGBA") \
            .resize((340, 340), Image.LANCZOS)
        glow = Image.open(ASSETS / "OpenBoxGL.png").convert("RGBA")
        self.glow = glow.resize((720, 720), Image.LANCZOS) \
            .filter(ImageFilter.GaussianBlur(40))
        title = Image.open(ASSETS / "openboxgl-title-dark.png").convert("RGBA")
        self.title = title.resize((int(title.width * 1.10),
                                   int(title.height * 1.10)), Image.LANCZOS)

    def render(self, t: float) -> Image.Image:
        frame = BASE_BG.copy()
        AMB.paint(frame, t)
        EMBERS.paint(frame, clamp(t - 0.4, 0, 99), spread=0.65)

        a0 = ease_out(win(t, 0.5, 1.45))
        if a0 > 0:
            cy = H // 2 - 90
            glow = self.glow.copy()
            pulse = 0.9 + 0.1 * math.sin(t * 1.35)
            glow.putalpha(self.glow.getchannel("A")
                          .point(lambda v, p=pulse, a=a0: int(v * 0.58 * p * a)))
            frame.paste(glow, (W // 2 - 360, cy - 360 + 26), glow)

            img = self.mark.copy()
            scale = lerp(0.93, 1.0, a0)
            if scale < 0.999:
                img = img.resize((int(340 * scale),) * 2, Image.BILINEAR)
            img.putalpha(img.getchannel("A").point(lambda v, a=a0: int(v * a)))
            frame.paste(img, (W // 2 - img.width // 2, cy - img.height // 2), img)

            if t < 2.7:  # expanding scan rings
                pr = ease_out(win(t, 0.95, 2.25))
                d = ImageDraw.Draw(frame, "RGBA")
                for k in range(2):
                    rr = 205 + k * 85 + pr * 320
                    alpha = int(max(0.0, 1.0 - pr) * 140 * (1.0 - k * 0.4))
                    if alpha > 4:
                        d.ellipse([W // 2 - rr, cy - rr, W // 2 + rr, cy + rr],
                                  outline=(255, 150, 60, alpha), width=2)

            ta = ease_out(win(t, 1.7, 2.5))
            ty = cy + 186
            fade_paste(frame, self.title, (W // 2 - self.title.width // 2, ty), ta)
            if ta > 0.15:
                d = ImageDraw.Draw(frame, "RGBA")
                d.line([W // 2 - 260, ty + self.title.height + 34,
                        W // 2 + 260, ty + self.title.height + 34],
                       fill=BORDER_CARD + (int(120 * ta),), width=1)

            ka = ease_out(win(t, 2.2, 2.95))
            if ka > 0:
                kick = "OPEN GAME LAUNCHER"
                font = f_semi(26)
                kb = Image.new("RGBA", (tracked_width(font, kick, 7) + 4, 38), (0, 0, 0, 0))
                draw_tracked(ImageDraw.Draw(kb), 0, 3, kick, font, 7, GOLD)
                fade_paste(frame, kb, (W // 2 - kb.width // 2, ty + self.title.height + 56), ka)
        return frame


class HookScene(Scene):
    def __init__(self):
        self.head = "Every game. One box."
        self.head_font = f_display(122)
        self.kick = "LOCAL-FIRST GAME LAUNCHER"
        self.kick_font = f_semi(28)
        self.sub_font = f_medium(36)
        self.sub_lines = [
            "Steam, ROMs, emulators, arcade —",
            "cataloged, launched and tracked in one beautiful place.",
        ]
        self.chips = ["Steam", "GOG", "Epic", "Lutris", "ROM folders", "MAME", "and more"]

    def render(self, t: float) -> Image.Image:
        frame = BASE_BG.copy()
        AMB.paint(frame, t)
        EMBERS.paint(frame, t + 1.6, spread=1.08)

        rise = ease_out(win(t, 0.22, 1.0))
        if rise <= 0:
            return frame

        ka = ease_out(win(t, 0.5, 1.05))
        if ka > 0:
            kb = Image.new("RGBA", (tracked_width(self.kick_font, self.kick, 7) + 4, 40),
                           (0, 0, 0, 0))
            draw_tracked(ImageDraw.Draw(kb), 0, 4, self.kick, self.kick_font, 7, GOLD)
            fade_paste(frame, kb, (W // 2 - kb.width // 2, 236), ka)

        head_w = int(self.head_font.getlength(self.head))
        hb = Image.new("RGBA", (head_w + 8, 176), (0, 0, 0, 0))
        ImageDraw.Draw(hb).text((0, -6), self.head, font=self.head_font, fill=TEXT)
        off = int((1.0 - rise) * 30)
        fade_paste(frame, hb, (W // 2 - hb.width // 2, 316 + off), rise)

        pulse = ease_out(win(t, 0.8, 1.75))
        if pulse > 0:
            gl = radial_glow(1250, BRAND, alpha=255)
            gl = gl.copy()
            gl.putalpha(radial_glow(1250, BRAND, alpha=255).getchannel("A")
                        .point(lambda v, p=pulse: int(v * 0.16 * p)))
            fade_paste(frame, gl, (W // 2 - 625, 205), pulse)

        sa = ease_out(win(t, 0.72, 1.4))
        if sa > 0:
            sw = max(int(self.sub_font.getlength(s)) for s in self.sub_lines)
            sb = Image.new("RGBA", (sw + 8, 118), (0, 0, 0, 0))
            sd = ImageDraw.Draw(sb)
            for i, s in enumerate(self.sub_lines):
                sd.text((2, 6 + i * 52), s, font=self.sub_font, fill=MUTED)
            fade_paste(frame, sb, (W // 2 - sb.width // 2, 536 + int(off * 0.6)), sa)

        chip_font = f_semi(26)
        widths = [int(chip_font.getlength(c)) + 42 for c in self.chips]
        total = sum(widths) + 16 * (len(self.chips) - 1)
        cx = W // 2 - total // 2
        for i, (label, cw) in enumerate(zip(self.chips, widths)):
            a = ease_out(win(t, 0.95 + i * 0.09, 1.36 + i * 0.09))
            if a <= 0:
                continue
            chip = Image.new("RGBA", (cw, 52), (0, 0, 0, 0))
            cd = ImageDraw.Draw(chip)
            cd.rounded_rectangle([0, 0, cw - 1, 51], radius=26, fill=FIELD + (175,),
                                 outline=BORDER_CARD + (255,), width=1)
            cd.text((21, 10), label, font=chip_font, fill=(214, 204, 190))
            fade_paste(frame, chip, (cx, 712 + int((1.0 - a) * 14)), a)
            cx += cw + 16
        return frame


class ShotScene(Scene):
    """Real-screenshot card with Ken Burns motion and a caption panel."""

    def __init__(self, image: str, kicker: str, headline: str, sub: str,
                 chips: list[str], z: tuple[float, float],
                 px: tuple[float, float], py: tuple[float, float],
                 center: tuple[float, float]):
        self.z = z
        self.px = px
        self.py = py
        self.center = center  # tilt of pan vs zoom bias (kept simple)
        self.w_disp, self.h_disp = 1740, 962
        self.x0, self.y0 = 90, 58
        self.card, self.hi = build_screenshot_card(ASSETS / image, self.w_disp,
                                                   self.h_disp, hi_scale=1.30)
        self.sweep = card_sweep_band(self.h_disp)
        self.caption = Caption(kicker, headline, sub, chips)

    def render(self, t: float) -> Image.Image:
        t = self.local(t)
        frame = BASE_BG.copy()

        # card underglow + drop shadow
        pul = 0.78 + 0.22 * math.sin(t * 0.9)
        ug = radial_glow(1300, BRAND, alpha=255)
        ug = ug.copy()
        ug.putalpha(ug.getchannel("A").point(lambda v, p=pul: int(v * 0.13 * p)))
        frame.paste(ug, (W // 2 - 650, self.y0 + self.h_disp - 520), ug)

        sh = Image.new("RGBA", (self.w_disp + 280, self.h_disp + 320), (0, 0, 0, 0))
        ds = ImageDraw.Draw(sh)
        ds.rounded_rectangle([140, 100, self.w_disp + 140, self.h_disp + 100],
                             radius=26, fill=(0, 0, 0, 110))
        sh = sh.filter(ImageFilter.GaussianBlur(36))
        frame.paste(sh, (self.x0 - 140, self.y0 - 50), sh)

        # Ken Burns window over the hi-res canvas
        dur = max(self.end - self.start, 0.001)
        e = ease_in_out(t / dur)
        zt = lerp(self.z[0], self.z[1], e)
        dw, dh = int(self.w_disp / zt), int(self.h_disp / zt)
        room_x = (self.hi.width - dw) // 2
        room_y = (self.hi.height - dh) // 2
        off_x = int(lerp(self.px[0], self.px[1], e) * room_x * 0.92)
        off_y = int(lerp(self.py[0], self.py[1], e) * room_y * 0.92)
        cx = clamp((self.hi.width - dw) // 2 + off_x, 0, self.hi.width - dw)
        cy = clamp((self.hi.height - dh) // 2 + off_y, 0, self.hi.height - dh)
        frame.paste(self.hi.crop((cx, cy, cx + dw, cy + dh))
                    .resize((self.w_disp, self.h_disp), Image.BILINEAR),
                    (self.x0, self.y0))

        # one-pass glass sweep
        sw_p = win(t, 0.35, 1.7)
        if 0.0 < sw_p < 1.0:
            a = math.sin(math.pi * sw_p)
            sx = int(lerp(-self.sweep.width, self.w_disp, ease_in_out(sw_p)))
            band = self.sweep.getchannel("A").point(lambda v, fa=a: int(v * fa))
            m = Image.new("L", (self.w_disp, self.h_disp), 0)
            m.paste(band, (sx, 0))
            m = ImageChops.multiply(m, self.card.getchannel("A"))
            frame.paste(WHITE, (self.x0, self.y0), m)

        # passthrough blinders: nothing should cover the caption but the scrim
        frame.paste(SCRIM, (0, 0), SCRIM)
        self.caption.paint(frame, self.x0 + 40, H - 40 - self.caption.height, t)
        return frame


class BentoScene(Scene):
    def __init__(self):
        self.kick = "AND EVERYTHING ELSE"
        self.head = "No account. No cloud. No telemetry."
        self.tiles = [
            ("Saves & backups", "Versioned, automatic, restorable"),
            ("RetroAchievements", "Hardcore, mastery, badge art"),
            ("Time Machine", "Every library change, diffable"),
            ("Smart collections", "Saved queries that stay alive"),
            ("Plugins 2.0", "Sandboxed, one-click install"),
            ("REST API", "Token-authed, fully local"),
        ]
        self.footer = "LaunchBox migration · Game Night decks · Wrapped · 5 languages · and more"

    def render(self, t: float) -> Image.Image:
        t = self.local(t)
        frame = BASE_BG.copy()
        AMB.paint(frame, t + 1.2)

        ka = ease_out(win(t, 0.06, 0.5))
        if ka > 0:
            kb = Image.new("RGBA", (tracked_width(f_semi(28), self.kick, 7) + 4, 38),
                           (0, 0, 0, 0))
            draw_tracked(ImageDraw.Draw(kb), 0, 3, self.kick, f_semi(28), 7, GOLD)
            fade_paste(frame, kb, (W // 2 - kb.width // 2, 150), ka)

        ha = ease_out(win(t, 0.18, 0.72))
        if ha > 0:
            hf = f_display(72)
            hb = Image.new("RGBA", (int(hf.getlength(self.head)) + 10, 106), (0, 0, 0, 0))
            ImageDraw.Draw(hb).text((4, -6), self.head, font=hf, fill=TEXT)
            fade_paste(frame, hb, (W // 2 - hb.width // 2, 214), ha)

        glow_a = ease_out(win(t, 0.3, 1.1))
        if glow_a > 0:
            gl = radial_glow(1300, BRAND, alpha=255)
            gl = gl.copy()
            gl.putalpha(gl.getchannel("A").point(lambda v, a=glow_a: int(v * 0.12 * a)))
            frame.paste(gl, (W // 2 - 650, 560), gl)

        tw, th, gap = 548, 208, 30
        cols, rows = 3, 2
        gx = (W - cols * tw - (cols - 1) * gap) // 2
        gy = 402
        title_font = f_semi(34)
        desc_font = f_medium(24)
        for i, (title, desc) in enumerate(self.tiles):
            r, c = divmod(i, cols)
            a = ease_out(win(t, 0.44 + i * 0.13, 0.9 + i * 0.13))
            if a <= 0:
                continue
            xi = gx + c * (tw + gap)
            yi = gy + r * (th + gap) + int((1.0 - a) * 16)
            tile = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
            d = ImageDraw.Draw(tile)
            d.rounded_rectangle([0, 0, tw - 1, th - 1], radius=14, fill=CARD + (236,),
                                outline=LINE + (210,), width=1)
            d.rounded_rectangle([0, 0, 5, th - 1], radius=2,
                                fill=(BRAND if i % 2 == 0 else GOLD) + (200,))
            d.text((36, 38), title, font=title_font, fill=TEXT)
            d.text((36, 100), desc, font=desc_font, fill=MUTED)
            for k, col in enumerate((BRAND, GOLD, CYAN)):
                d.ellipse([36 + k * 24, th - 42, 42 + k * 24, th - 36], fill=col + (150,))
            fade_paste(frame, tile, (xi, yi), a)

        fa = ease_out(win(t, 1.3, 1.85))
        if fa > 0:
            fb = Image.new("RGBA", (int(f_medium(26).getlength(self.footer)) + 8, 40),
                           (0, 0, 0, 0))
            ImageDraw.Draw(fb).text((0, 2), self.footer, font=f_medium(26), fill=FAINT)
            fade_paste(frame, fb, (W // 2 - fb.width // 2,
                                   gy + 2 * th + gap + 42), fa)
        return frame


class OutroScene(Scene):
    def __init__(self):
        self.mark = Image.open(ASSETS / "openbox-logo.png").convert("RGBA") \
            .resize((300, 300), Image.LANCZOS)
        self.title = Image.open(ASSETS / "openboxgl-title-dark.png").convert("RGBA")
        self.chips = ["Linux", "Windows", "AGPL-3.0", "No account", "No telemetry"]
        self.url = "github.com/vindeckyy/OpenBoxGL"
        self.install = "AppImage · Flatpak · System install · Windows zip"
        self.tag = "YOUR LIBRARY. YOUR MACHINE."

    def render(self, t: float) -> Image.Image:
        t = self.local(t)
        frame = BASE_BG.copy()
        AMB.paint(frame, t + 2.0)
        EMBERS.paint(frame, t * 0.55 + 2.0, spread=0.55)

        glow = radial_glow(980, BRAND, alpha=255)
        pul = 0.75 + 0.25 * math.sin(t * 0.9 + 1.0)
        g = glow.copy()
        g.putalpha(glow.getchannel("A").point(lambda v, p=pul: int(v * 0.20 * p)))
        frame.paste(g, (W // 2 - 490, -130), g)

        a0 = ease_out(win(t, 0.08, 0.66))
        if a0 > 0:
            img = self.mark.copy()
            scale = lerp(0.95, 1.0, a0)
            if scale < 0.999:
                img = img.resize((int(300 * scale),) * 2, Image.BILINEAR)
            img.putalpha(img.getchannel("A").point(lambda v, a=a0: int(v * a)))
            frame.paste(img, (W // 2 - img.width // 2, 64), img)
        fade_paste(frame, self.title, (W // 2 - self.title.width // 2, 404),
                   ease_out(win(t, 0.42, 1.05)))

        d = ImageDraw.Draw(frame, "RGBA")
        line_a = ease_out(win(t, 0.72, 1.2))
        if line_a > 0.02:
            d.line([W // 2 - 160 - 200, 548, W // 2 - 160, 548],
                   fill=BORDER_CARD + (int(140 * line_a),), width=1)
            d.line([W // 2 + 160, 548, W // 2 + 160 + 200, 548],
                   fill=BORDER_CARD + (int(140 * line_a),), width=1)

        chip_font = f_semi(26)
        widths = [int(chip_font.getlength(c)) + 38 for c in self.chips]
        total = sum(widths) + 14 * (len(self.chips) - 1)
        cx = W // 2 - total // 2
        for i, (label, cw) in enumerate(zip(self.chips, widths)):
            a = ease_out(win(t, 0.88 + i * 0.08, 1.3 + i * 0.08))
            if a <= 0:
                continue
            chip = Image.new("RGBA", (cw, 48), (0, 0, 0, 0))
            cd = ImageDraw.Draw(chip)
            cd.rounded_rectangle([0, 0, cw - 1, 47], radius=24, fill=FIELD + (150,),
                                 outline=LINE + (255,), width=1)
            cd.text((19, 10), label, font=chip_font, fill=(196, 184, 168))
            fade_paste(frame, chip, (cx, 588), a)
            cx += cw + 14

        uf = f_semi(45)
        ua = ease_out(win(t, 1.35, 1.95))
        if ua > 0:
            ub = Image.new("RGBA", (int(uf.getlength(self.url)) + 8, 64), (0, 0, 0, 0))
            ImageDraw.Draw(ub).text((0, -4), self.url, font=uf, fill=(214, 204, 190))
            fade_paste(frame, ub, (W // 2 - ub.width // 2, 694), ua)
            ug = radial_glow(240, BRAND, alpha=255)
            ug = ug.copy()
            ug.putalpha(ug.getchannel("A").point(lambda v, a=ua: int(v * 0.30 * a)))
            frame.paste(ug, (W // 2 - 120, 668), ug)

        ia = ease_out(win(t, 1.75, 2.35))
        if ia > 0:
            ib = Image.new("RGBA", (int(f_medium(25).getlength(self.install)) + 6, 36),
                           (0, 0, 0, 0))
            ImageDraw.Draw(ib).text((0, 2), self.install, font=f_medium(25),
                                    fill=(150, 140, 127))
            fade_paste(frame, ib, (W // 2 - ib.width // 2, 800), ia)

        bga = ease_out(win(t, 2.1, 2.8))
        if bga > 0:
            bb = Image.new("RGBA", (tracked_width(f_semi(24), self.tag, 5) + 4, 34),
                           (0, 0, 0, 0))
            draw_tracked(ImageDraw.Draw(bb), 0, 3, self.tag, f_semi(24), 5, (150, 140, 127))
            fade_paste(frame, bb, (W // 2 - bb.width // 2, 880), bga)
        return frame


# --------------------------------------------------------------- timeline ---

def build_timeline() -> list[Scene]:
    scenes = [
        IntroScene(),
        HookScene(),
        ShotScene("openbox-screenshot.png", "One library", "Everything on one shelf",
                  "Your whole PC and retro catalogue, side by side.",
                  ["Steam", "Heroic", "Lutris", "ROM folders", "Arcade"],
                  z=(1.05, 1.18), px=(0.9, -0.4), py=(-0.5, 0.35), center=(0.6, 0.4)),
        ShotScene("openbox-game-detail.png", "Every game", "Presented like it matters",
                  "Box art, screenshots, play history and achievements — all local.",
                  ["Metadata", "Moments", "Story"],
                  z=(1.18, 1.06), px=(-0.8, 0.5), py=(0.4, -0.3), center=(0.4, 0.35)),
        ShotScene("openbox-bigbox.png", "Big Box", "Built for the couch",
                  "Fullscreen layouts, video snaps and gamepad-first navigation.",
                  ["Stage layout", "Game Night", "Attract mode"],
                  z=(1.04, 1.2), px=(0.25, -0.2), py=(0.8, -0.5), center=(0.55, 0.62)),
        ShotScene("openbox-constellation.png", "Library Constellation", "See what connects",
                  "Series, developers and genres drawn as one constellation.",
                  ["Series", "Developers", "Genres"],
                  z=(1.2, 1.07), px=(-0.75, 0.55), py=(-0.45, 0.4), center=(0.46, 0.5)),
        ShotScene("openbox-wrapped.png", "Play insights", "Your year in games",
                  "Streaks, momentum and mastery — computed on your machine.",
                  ["Wrapped", "Heatmap", "Mastery Map"],
                  z=(1.06, 1.17), px=(0.8, -0.45), py=(-0.4, 0.4), center=(0.5, 0.4)),
        BentoScene(),
        OutroScene(),
    ]
    starts = [0.0, 5.5, 10.5, 17.0, 23.0, 29.5, 35.0, 41.0, 47.5]
    ends = starts[1:] + [DUR]
    for sc, st, en in zip(scenes, starts, ends):
        sc.start, sc.end = st, en
    return scenes


# ------------------------------------------------------------------ audio ---

SR = 44100


def _band_noise(rng: np.random.Generator, lo: float, hi: float, n: int) -> np.ndarray:
    wh = rng.standard_normal(n)
    sp = np.fft.rfft(wh)
    fr = np.fft.rfftfreq(n, 1.0 / SR)
    g = np.exp(-0.5 * ((fr - (lo + hi) / 2.0) / max((hi - lo) / 2.0, 1.0)) ** 2)
    s = np.fft.irfft(sp * g, n)
    s /= max(1e-9, np.abs(s).max())
    return s


def _env_add(dst_l, dst_r, sig: np.ndarray, t0: float, atk: float, dec: float,
             pan: float = 0.0, gain: float = 1.0) -> None:
    n = len(dst_l)
    i0 = int(max(0, t0 * SR))
    i1 = min(n, int((t0 + max(len(sig), 1) / SR) * SR))
    if i1 <= i0:
        return
    seg_t = np.arange(i1 - i0) / SR
    env = np.where(seg_t < atk, seg_t / max(atk, 1e-6), np.exp(-(seg_t - atk) / dec))
    env = np.minimum(env, 1.0)
    s = sig[: i1 - i0] * env * gain
    dst_l[i0:i1] += s * (1.0 - max(0.0, pan))
    dst_r[i0:i1] += s * (1.0 + min(0.0, pan))


def synth_mix() -> np.ndarray:
    """Cinematic ambient bed: warm pad, sub-thumps, air whooshes, glints."""
    T = DUR + 0.5
    n = int(T * SR)
    t = np.arange(n) / SR
    rng = np.random.default_rng(23)
    left = np.zeros(n)
    right = np.zeros(n)

    # warm pad: three detuned lows with slow breathing + low rumble bed
    wob = 0.5 + 0.5 * np.sin(math.tau * 0.045 * t)
    pad = (0.16 * wob * np.sin(math.tau * 55.0 * t)
           + 0.10 * (1.0 - 0.3 * wob) * np.sin(math.tau * 82.41 * t)
           + 0.05 * np.sin(math.tau * 123.47 * t))
    pad += _band_noise(rng, 28, 210, n) * 0.05
    left += pad * 0.85
    right += pad * 0.85

    thump = np.arange(int(1.25 * SR)) / SR
    f = 92.0 * np.exp(-thump * 6.0) + 32.0
    thump_sig = np.sin(2 * np.pi * np.cumsum(f) / SR) * 0.5

    whoosh_times = [s.start + 0.05 for s in _SCENES[1:]]
    for k, t0 in enumerate(whoosh_times):
        s = _band_noise(rng, 480, 3100, int(1.15 * SR)) * 0.055
        _env_add(left, right, s, t0 - 0.26, 0.24, 0.42,
                 pan=0.45 if k % 2 else -0.45, gain=0.09)

    for t0 in _CAPTION_BEATS:
        _env_add(left, right, thump_sig, t0, 0.006, 0.30, gain=0.85)

    glints = zip(_CAPTION_BEATS, (1567.98, 1244.51, 1396.91, 1174.66, 1046.50,
                                  1318.51, 1567.98, 2093.00))
    for t0, fq in glints:
        seg = np.arange(int(0.3 * SR)) / SR
        _env_add(left, right, np.sin(2 * np.pi * fq * seg) * 0.03, t0, 0.005, 0.085)

    # gentle riser into the finale + warm landed chord
    rlen, r0 = 4.2, 44.2
    rt = np.arange(int(rlen * SR)) / SR
    ris = np.sin(2 * np.pi * (170 + 55 * (rt / rlen) ** 2.4) * rt) * 0.026
    ris += rng.standard_normal(len(rt)) * np.linspace(0.0, 0.02, len(rt)) ** 2
    _env_add(left, right, ris, r0, rlen * 0.9, rlen, gain=1.0)
    chord_t = np.arange(int(11.0 * SR)) / SR
    for fq, amp in ((110.0, 0.038), (164.81, 0.026), (220.0, 0.017), (277.18, 0.010)):
        _env_add(left, right, np.sin(2 * np.pi * fq * chord_t) * amp, 47.6, 0.7, 3.4)

    mix_l, mix_r = left, right
    mix_l = np.tanh(1.1 * mix_l) * 0.9
    mix_r = np.tanh(1.1 * mix_r) * 0.9
    fi, fo = int(FADE_IN * SR), int(2.6 * SR)
    for ch in (mix_l, mix_r):
        ch[:fi] *= np.linspace(0.0, 1.0, fi)
        ch[-fo:] *= np.linspace(1.0, 0.0, fo)
    peak = max(1e-9, max(np.abs(mix_l).max(), np.abs(mix_r).max()))
    k = 0.76 / peak
    return np.stack([mix_l * k, mix_r * k], axis=1)


def write_wav(path: Path, stereo: np.ndarray) -> None:
    pcm = (np.clip(stereo, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(pcm.tobytes())


# --------------------------------------------------------------- pipeline ---

GRAY_TARGET = (11, 10, 9)


def composite(t: float) -> Image.Image:
    """Assemble the full frame at absolute time t."""
    scenes = _SCENES

    # boundary crossfade? find a scene start straddled by the cross window
    bound_k = None  # index of the scene starting at this boundary
    for k in range(1, len(scenes)):
        if scenes[k].start - CROSS / 2 <= t < scenes[k].start + CROSS / 2:
            bound_k = k
            break

    if bound_k is not None:
        prev_sc, next_sc = scenes[bound_k - 1], scenes[bound_k]
        f = ease_in_out(win(t, scenes[bound_k].start - CROSS / 2,
                            scenes[bound_k].start + CROSS / 2))
        frame = Image.blend(prev_sc.render(t - prev_sc.start),
                            next_sc.render(t - next_sc.start), f)
    else:
        active = scenes[0]
        for sc in scenes:
            if sc.start <= t < sc.end:
                active = sc
                break
        if t >= scenes[-1].end:
            active = scenes[-1]
        frame = active.render(t - active.start)

    a_in = ease_out(win(t, 0.0, FADE_IN))
    a_out = 1.0 - win(t, DUR - FADE_OUT, DUR)
    a = min(a_in, a_out)
    if a < 1.0:
        frame = Image.blend(Image.new("RGB", (W, H), BLACK), frame, a)

    grain = _GRAIN[int(t * FPS) % len(_GRAIN)]
    arr = np.asarray(frame, dtype=np.int16)
    arr = np.clip(arr + grain, 0, 255).astype(np.uint8)
    return Image.fromarray(arr)


def probe(times: list[float], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for tt in times:
        p = out_dir / f"probe_t{tt:05.2f}.png"
        composite(tt).resize((960, 540), Image.LANCZOS).save(p)
        print(f"  probe {tt:6.2f}s -> {p.name}", flush=True)


def encode_frames(out_dir: Path, frames: int) -> subprocess.Popen:
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
        "-i", "-",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-g", "60",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
        str(out_dir / "openbox-promo-1080p.mp4"),
    ]
    cap = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
    t0 = time.time()
    for i in range(frames):
        cap.stdin.write(composite(i / FPS).tobytes())
        if i % 150 == 0:
            el = time.time() - t0
            eta = el / max(1, i) * (frames - i)
            print(f"  frame {i:5d}/{frames}   t={i / FPS:6.2f}s   {el:6.1f}s, ~{eta:4.0f}s left",
                  flush=True)
        if cap.poll() is not None:
            raise RuntimeError(f"ffmpeg exited early with code {cap.returncode}")
    cap.stdin.close()
    cap.wait()
    if cap.returncode != 0:
        raise RuntimeError(f"ffmpeg failed with code {cap.returncode}")
    return cap


def main() -> int:
    global BASE_BG, AMB, EMBERS, SCRIM, WHITE, _SCENES, _GRAIN, _CAPTION_BEATS

    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="render sample stills only")
    ap.add_argument("--probe-times", type=str,
                    default="0.9,2.6,4.4,7.4,9.6,12.6,15.0,19.5,22.0,26.2,28.2,32.4,34.2,37.5,39.8,42.6,45.4,47.0,50.5,53.8")
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    ap.add_argument("--video-only", action="store_true")
    args = ap.parse_args()

    print("Preparing assets...", flush=True)
    BASE_BG = make_bg()
    SCRIM = build_scrim()
    WHITE = Image.new("RGB", (W, H), (255, 249, 240))
    AMB = AmbientGlows()
    EMBERS = Embers()
    _SCENES = build_timeline()
    _GRAIN = build_grain()
    _CAPTION_BEATS = [s.start + 0.55 for s in _SCENES[1:]] + [48.2]

    args.out.mkdir(parents=True, exist_ok=True)
    times = [float(x) for x in args.probe_times.split(",") if x]
    if args.probe:
        probe(times, args.out)
        return 0

    video = args.out / "openbox-promo-1080p.mp4"
    frames = int(DUR * FPS)

    audio_path = args.out / "score.wav"
    if not args.video_only:
        print("Synthesizing score...", flush=True)
        write_wav(audio_path, synth_mix())

    print(f"Encoding {frames} frames -> {video}", flush=True)
    encode_frames(args.out, frames)

    if not args.video_only:
        mux = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video), "-i", str(audio_path),
            "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
            "-shortest", str(args.out / "openbox-promo-1080p-scored.mp4"),
        ]
        subprocess.run(mux, check=True)
        audio_path.unlink(missing_ok=True)
        final = args.out / "openbox-promo-1080p-scored.mp4"
        video.replace(video.with_name(video.name.replace(".mp4", "-silent.mp4")))
    else:
        final = video

    composite(26.4).save(args.out / "poster.jpg", quality=92)
    print(f"Done: {final}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
