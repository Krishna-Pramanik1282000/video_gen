"""
Free animated stick-figure explainer video maker.
Topic -> script (Gemini free tier) -> voice (edge-tts, free)
      -> animated scenes drawn frame by frame (Pillow) -> video (ffmpeg).
Runs on GitHub Actions. Output: output/final.mp4 + output/info.txt
"""
import asyncio
import json
import math
import os
import random
import re
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

TOPIC = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TOPIC", "")).strip()
TOPIC = TOPIC or "Why is the sky blue?"
SCENES = int(os.environ.get("SCENES") or 12)
VOICE = os.environ.get("VOICE", "en-US-GuyNeural")
FPS = int(os.environ.get("FPS") or 20)
CAPTIONS = (os.environ.get("CAPTIONS") or "1") != "0"

W, H = 1280, 720
FLOOR_Y = 575      # top of the floor band
GROUND = 590       # where characters' feet stand
INK = (30, 30, 30)
OUT = Path("output")
TMP = OUT / "tmp"

THEME_LIST = ["sky", "sunset", "night", "space", "city", "forest", "ocean", "lab", "stage", "tech"]
WHO = ["hero", "friend", "teacher", "robot", "alien"]
ACTIONS = ["idle", "wave", "cheer", "jump", "dance", "walk", "run", "think", "point",
           "shock", "shrug", "sad"]
PROPS = ["none", "sun", "cloud", "question", "lightbulb", "star", "arrow", "clock", "coin",
         "heart", "warning", "gear", "book", "rocket", "earth", "chart", "phone", "lock",
         "check", "cross", "atom", "rainbow", "magnifier", "brain"]
ACCENTS = [(255, 84, 84), (255, 160, 0), (40, 170, 110), (70, 130, 255), (170, 80, 250), (240, 70, 160)]

PROMPT = """You write scripts for a lively animated stick-figure explainer YouTube video.
Topic: {topic}
Write exactly {n} scenes. Scene 1 is a curious hook. The last scene is a short
conclusion that also asks viewers to subscribe. Keep the tone friendly and simple.

Return ONLY valid JSON, no other text, in this shape:
{{"title": "catchy YouTube title",
  "description": "2-3 sentence YouTube description",
  "scenes": [
    {{"narration": "2-3 spoken sentences, no emojis, no stage directions",
      "label": "2-5 word caption shown on screen",
      "theme": one of {themes},
      "chars": [{{"who": one of {who}, "action": one of {actions}}}],
      "prop": one of {props}}}
  ]}}
Rules:
- "chars" has 1 or 2 characters. Use "hero" in most scenes. Use a second character
  (friend, teacher, robot or alien) in about half of the scenes.
- Match the action to the words: think for questions, shock for surprising facts,
  cheer or jump for good news and the ending, walk or run for travel and movement,
  point or wave when explaining, shrug or sad for problems.
- Pick a theme that fits the topic (sunset for sunsets, space for planets, tech for
  computers and AI, ocean for water, lab for science, stage for celebrations).
- Vary theme, actions and prop from scene to scene."""


# ---------------------------------------------------------------- script ---
def ask_llm(prompt):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise SystemExit("GEMINI_API_KEY is missing. Add it in the repo: "
                         "Settings > Secrets and variables > Actions.")
    models = [m for m in (os.environ.get("GEMINI_MODEL"), "gemini-2.5-flash",
                          "gemini-2.5-flash-lite", "gemini-3-flash-preview") if m]
    last = None
    for m in models:
        for attempt in range(2):
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
                    headers={"x-goog-api-key": key},
                    json={"contents": [{"parts": [{"text": prompt}]}],
                          "generationConfig": {"responseMimeType": "application/json",
                                               "temperature": 0.8}},
                    timeout=180,
                )
                r.raise_for_status()
                parts = r.json()["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts)
                if text.strip():
                    return text
            except Exception as e:  # retry, then try the next model
                last = e
                print(f"[llm] {m} attempt {attempt + 1} failed: {e}")
                time.sleep(5)
    raise SystemExit(f"Could not get a script from Gemini: {last}")


def clean_scene(s, i):
    chars = []
    for c in (s.get("chars") or []):
        who, act = (c.get("who"), c.get("action")) if isinstance(c, dict) else (str(c), "idle")
        chars.append((who if who in WHO else "hero", act if act in ACTIONS else "idle"))
    chars = chars[:2] or [("hero", "idle")]
    if len(chars) == 2 and chars[0][0] == chars[1][0]:
        chars[1] = ("friend" if chars[0][0] != "friend" else "teacher", chars[1][1])
    theme = s.get("theme")
    return {
        "narration": str(s.get("narration", "")).strip(),
        "label": str(s.get("label", "")).strip(),
        "theme": theme if theme in THEME_LIST else THEME_LIST[i % len(THEME_LIST)],
        "chars": chars,
        "prop": s.get("prop") if s.get("prop") in PROPS else "none",
    }


def get_script():
    prompt = PROMPT.format(topic=TOPIC, n=SCENES, themes=THEME_LIST, who=WHO,
                           actions=ACTIONS, props=PROPS)
    raw = ask_llm(prompt)
    raw = re.sub(r"```(?:json)?", "", raw)
    data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    scenes = [clean_scene(s, i) for i, s in enumerate(data["scenes"])]
    scenes = [s for s in scenes if s["narration"]]
    if not scenes:
        raise SystemExit("The AI returned no scenes.")
    data["scenes"] = scenes
    return data


# ----------------------------------------------------------------- voice ---
async def _edge(text, path):
    import edge_tts
    await edge_tts.Communicate(text, VOICE).save(path)


def make_voice(text, base):
    mp3 = f"{base}.mp3"
    for attempt in range(3):
        try:
            asyncio.run(_edge(text, mp3))
            if os.path.getsize(mp3) > 1000:
                return mp3
        except Exception as e:
            print(f"[voice] edge-tts attempt {attempt + 1} failed: {e}")
        time.sleep(2)
    wav = f"{base}.wav"  # robotic but always works
    print("[voice] falling back to espeak-ng")
    subprocess.run(["espeak-ng", "-s", "150", "-w", wav, text], check=True)
    return wav


def duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True).stdout
    return float(out.strip())


# ------------------------------------------------------- drawing helpers ---
@lru_cache(maxsize=64)
def font(size):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


def darker(c, f=0.8):
    return tuple(int(v * f) for v in c)


def ease_out(x):
    x = min(max(x, 0.0), 1.0)
    return 1 - (1 - x) ** 3


def ease_out_back(x):
    x = min(max(x, 0.0), 1.0)
    c1 = 1.70158
    c3 = c1 + 1
    return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2


class SD:
    """ImageDraw wrapper that scales every coordinate (used for smooth 2x sprites)."""

    def __init__(self, im, k=1):
        self.d = ImageDraw.Draw(im)
        self.k = k

    def _p(self, pts):
        return [(x * self.k, y * self.k) for x, y in pts]

    def _w(self, w):
        return max(1, round(w * self.k))

    def line(self, pts, fill, width=1):
        self.d.line(self._p(pts), fill=fill, width=self._w(width), joint="curve")

    def circle(self, c, r, fill=None, outline=None, width=1):
        k = self.k
        self.d.ellipse([(c[0] - r) * k, (c[1] - r) * k, (c[0] + r) * k, (c[1] + r) * k],
                       fill=fill, outline=outline, width=self._w(width))

    def ellipse(self, box, fill=None, outline=None, width=1):
        k = self.k
        self.d.ellipse([v * k for v in box], fill=fill, outline=outline, width=self._w(width))

    def rect(self, box, fill=None, outline=None, width=1, radius=0):
        k = self.k
        b = [v * k for v in box]
        if radius:
            self.d.rounded_rectangle(b, radius * k, fill=fill, outline=outline, width=self._w(width))
        else:
            self.d.rectangle(b, fill=fill, outline=outline, width=self._w(width))

    def poly(self, pts, fill=None, outline=None, width=1):
        self.d.polygon(self._p(pts), fill=fill, outline=outline, width=self._w(width))

    def arc(self, box, a0, a1, fill, width=1):
        self.d.arc([v * self.k for v in box], a0, a1, fill=fill, width=self._w(width))

    def pie(self, box, a0, a1, fill=None, outline=None, width=1):
        self.d.pieslice([v * self.k for v in box], a0, a1, fill=fill, outline=outline,
                        width=self._w(width))

    def text(self, xy, s, size, fill, anchor="mm", stroke=0, stroke_fill=None):
        self.d.text((xy[0] * self.k, xy[1] * self.k), s, font=font(round(size * self.k)),
                    fill=fill, anchor=anchor, stroke_width=round(stroke * self.k),
                    stroke_fill=stroke_fill)


def text_width(s, size):
    return font(size).getlength(s)


def limb(sd, pts, w, c):
    """Thick line with round ends and a dark outline."""
    sd.line(pts, INK, w + 7)
    for q in pts:
        sd.circle(q, (w + 7) / 2, fill=INK)
    sd.line(pts, c, w)
    for q in pts:
        sd.circle(q, w / 2, fill=c)


# ----------------------------------------------------------- characters ---
SS = 2                    # supersampling for smooth lines
CW, CH, FY = 440, 560, 520   # character sprite size and the y of its feet

CHARS = {
    "hero": dict(color=(45, 110, 255)),
    "friend": dict(color=(240, 70, 150)),
    "teacher": dict(color=(35, 170, 95)),
    "robot": dict(color=(70, 195, 205)),
    "alien": dict(color=(140, 215, 60)),
}


def pose_for(action, t):
    s = math.sin
    p = dict(dx=0.0, bob=3 * s(t * 3.2), lean=0.0, tilt=0.0, aL=14.0, bL=8.0, aR=14.0,
             bR=8.0, ph=None, step=0.0, lift=0.0, jump=0.0)
    if action == "wave":
        p.update(aR=128, bR=30 * s(t * 11), tilt=4 * s(t * 3))
    elif action == "cheer":
        u = abs(s(t * 4.5))
        p.update(aL=135 + 8 * s(t * 9), aR=135 - 8 * s(t * 9), bL=22, bR=22, jump=u * 55,
                 bob=14 * max(0, 0.15 - u) / 0.15 * 0.6)
    elif action == "jump":
        u = abs(s(t * 4.5))
        p.update(aL=30 + 105 * u, aR=30 + 105 * u, bL=20, bR=20, jump=u * 85,
                 bob=16 * max(0, 0.18 - u) / 0.18)
    elif action == "dance":
        p.update(lean=9 * s(t * 5), tilt=8 * s(t * 5 + 1), aL=100 + 45 * s(t * 5),
                 aR=100 - 45 * s(t * 5), bL=25 * s(t * 10), bR=-25 * s(t * 10),
                 ph=t * 5, step=14, lift=10, bob=-abs(s(t * 5)) * 9)
    elif action == "walk":
        ph = t * 8
        p.update(ph=ph, step=34, lift=18, aL=14 + 38 * s(ph), aR=14 - 38 * s(ph),
                 bob=-5 * abs(s(ph)))
    elif action == "run":
        ph = t * 13
        p.update(ph=ph, step=48, lift=34, lean=8, aL=70 + 50 * s(ph), aR=70 - 50 * s(ph),
                 bL=70, bR=70, bob=-7 * abs(s(ph)))
    elif action == "think":
        p.update(aR=35, bR=145, tilt=10 + 3 * s(t * 2), lean=2)
    elif action == "point":
        p.update(aR=92 + 3 * s(t * 4), bR=0, tilt=-4)
    elif action == "shock":
        p.update(aL=95 + 8 * s(t * 30), aR=95 - 8 * s(t * 30), bL=20, bR=20, lean=-10,
                 dx=3 * s(t * 35), bob=-8)
    elif action == "shrug":
        p.update(aL=48, aR=48, bL=105 - 15 * s(t * 3), bR=105 - 15 * s(t * 3), tilt=10 * s(t * 2))
    elif action == "sad":
        p.update(aL=8, aR=8, tilt=-16, lean=6, bob=4)
    else:  # idle
        p.update(aL=14 + 5 * s(t * 2), aR=14 + 5 * s(t * 2 + 1.5), tilt=3 * s(t * 1.5))
    return p


def draw_char(who, p, t, facing, talk, mood, seed=0.0):
    col = CHARS[who]["color"]
    im = Image.new("RGBA", (CW * SS, CH * SS), (0, 0, 0, 0))
    sd = SD(im, SS)
    cx = CW / 2 + p["dx"]
    lean = math.radians(p["lean"] * facing)
    tilt = math.radians(p["tilt"] * facing)
    hip = (cx, FY - 108 + p["bob"])
    neck = (hip[0] + math.sin(lean) * 112, hip[1] - math.cos(lean) * 112)
    sh = (hip[0] + math.sin(lean) * 90, hip[1] - math.cos(lean) * 90)
    hr = 50
    hc = (neck[0] + math.sin(lean + tilt) * hr, neck[1] - math.cos(lean + tilt) * hr)

    limbs = []
    for side in (-1, 1):                                   # legs
        ph = p["ph"]
        if ph is None:
            fx, fy = cx + side * 32, FY
        else:
            a = ph + (0 if side < 0 else math.pi)
            fx = cx + side * 26 + math.sin(a) * p["step"]
            fy = FY - max(0.0, math.cos(a)) * p["lift"]
        mx, my = (hip[0] + fx) / 2, (hip[1] + fy) / 2
        comp = max(0.0, 108 - math.hypot(fx - hip[0], fy - hip[1]))
        knee = (mx + side * (5 + 0.6 * comp), my)
        limbs.append(([hip, knee, (fx, fy), (fx + side * 16, fy)], 10, col))
    limbs.append(([hip, sh, neck], 17, col))               # body
    hands = []
    for side, ang, bend in ((-1, p["aL"], p["bL"]), (1, p["aR"], p["bR"])):
        a = math.radians(ang)
        g = a + math.radians(bend)
        el = (sh[0] + side * math.sin(a) * 50, sh[1] + math.cos(a) * 50)
        hd = (el[0] + side * math.sin(g) * 48, el[1] + math.cos(g) * 48)
        limbs.append(([sh, el, hd], 9, col))
        hands.append(hd)
    for pts, w, c in limbs:
        sd.line(pts, INK, w + 7)
        for q in pts:
            sd.circle(q, (w + 7) / 2, fill=INK)
    for pts, w, c in limbs:
        sd.line(pts, c, w)
        for q in pts:
            sd.circle(q, w / 2, fill=c)
    for hd in hands:
        sd.circle(hd, 8, fill=(255, 235, 215), outline=INK, width=3)

    blink = ((t * 1000 + seed * 977) % 3200) < 140
    ex, ey = 18, -3
    # ---- head
    if who == "robot":
        sd.rect((hc[0] - 50, hc[1] - 45, hc[0] + 50, hc[1] + 45), fill=(210, 220, 230),
                outline=INK, width=6, radius=14)
        for sx in (-1, 1):
            if blink:
                sd.line([(hc[0] + sx * 20 - 10, hc[1] - 6), (hc[0] + sx * 20 + 10, hc[1] - 6)], INK, 4)
            else:
                sd.rect((hc[0] + sx * 20 - 10, hc[1] - 15, hc[0] + sx * 20 + 10, hc[1] + 5),
                        fill=(60, 230, 255), outline=INK, width=3, radius=3)
        if talk:
            for k in range(-2, 3):
                h = 4 + 10 * abs(math.sin(t * 13 + k))
                sd.line([(hc[0] + k * 10, hc[1] + 25 - h / 2), (hc[0] + k * 10, hc[1] + 25 + h / 2)], INK, 4)
        else:
            sd.line([(hc[0] - 25, hc[1] + 25), (hc[0] + 25, hc[1] + 25)], INK, 4)
        sd.line([(hc[0], hc[1] - 45), (hc[0], hc[1] - 74)], INK, 5)
        on = math.sin(t * 6) > 0
        sd.circle((hc[0], hc[1] - 78), 9, fill=(255, 60, 60) if on else (150, 40, 40), outline=INK, width=3)
    else:
        if who == "alien":
            sd.ellipse((hc[0] - 46, hc[1] - 52, hc[0] + 46, hc[1] + 50), fill=(160, 230, 80),
                       outline=INK, width=6)
            for sx in (-1, 1):
                sd.line([(hc[0] + sx * 20, hc[1] - 48), (hc[0] + sx * 38, hc[1] - 80)], INK, 4)
                sd.circle((hc[0] + sx * 40, hc[1] - 84), 7, fill=(255, 120, 200), outline=INK, width=3)
        else:
            sd.circle(hc, hr, fill=(255, 255, 255), outline=INK, width=6)
        big = who == "alien"
        for sx in (-1, 1):
            x, y = hc[0] + sx * ex, hc[1] + ey
            if big:
                if blink:
                    sd.line([(x - 9, y), (x + 9, y)], INK, 4)
                else:
                    sd.ellipse((x - 11, y - 14, x + 11, y + 14), fill=INK)
                    sd.circle((x - 3, y - 4), 3, fill=(255, 255, 255))
            elif mood == "shock":
                sd.circle((x, y), 9, fill=(255, 255, 255), outline=INK, width=3)
                sd.circle((x + facing * 2, y), 3, fill=INK)
            elif blink:
                sd.line([(x - 6, y), (x + 6, y)], INK, 4)
            else:
                sd.circle((x + facing * 2, y), 6, fill=INK)
        my = hc[1] + 23
        if talk:
            h = 4 + 11 * abs(math.sin(t * 13))
            sd.ellipse((hc[0] - 11, my - h / 2, hc[0] + 11, my + h / 2), fill=INK)
        elif mood == "shock":
            sd.circle((hc[0], my + 2), 9, outline=INK, width=4)
        elif mood == "sad":
            sd.arc((hc[0] - 15, my - 2, hc[0] + 15, my + 22), 200, 340, INK, 4)
        elif mood == "think":
            sd.line([(hc[0] - 11, my + 4), (hc[0] + 11, my + 2)], INK, 4)
        else:
            sd.arc((hc[0] - 18, my - 14, hc[0] + 18, my + 12), 15, 165, INK, 4)
        if who == "hero":                                   # red cap
            capc = (235, 60, 60)
            sd.pie((hc[0] - 53, hc[1] - 72, hc[0] + 53, hc[1] + 34), 180, 360, fill=capc,
                   outline=INK, width=5)
            sd.poly([(hc[0], hc[1] - 23), (hc[0] + facing * 72, hc[1] - 21),
                     (hc[0] + facing * 72, hc[1] - 11), (hc[0], hc[1] - 14)], fill=capc,
                    outline=INK, width=4)
        elif who == "friend":                               # hair bun
            hair = (110, 60, 40)
            sd.pie((hc[0] - 53, hc[1] - 64, hc[0] + 53, hc[1] + 42), 185, 355, fill=hair,
                   outline=INK, width=5)
            sd.circle((hc[0], hc[1] - 64), 19, fill=hair, outline=INK, width=5)
        elif who == "teacher":                              # glasses and bow tie
            for sx in (-1, 1):
                sd.circle((hc[0] + sx * ex, hc[1] + ey), 16, outline=INK, width=3)
            sd.line([(hc[0] - 3, hc[1] + ey), (hc[0] + 3, hc[1] + ey)], INK, 3)
            sd.poly([(neck[0], neck[1] + 10), (neck[0] - 20, neck[1] - 2), (neck[0] - 20, neck[1] + 22)],
                    fill=(230, 60, 60), outline=INK, width=3)
            sd.poly([(neck[0], neck[1] + 10), (neck[0] + 20, neck[1] - 2), (neck[0] + 20, neck[1] + 22)],
                    fill=(230, 60, 60), outline=INK, width=3)
    return im.reduce(SS)


# ------------------------------------------------------------- props ---
def draw_prop(sd, name, cx, cy, t=0.0):
    Y = (250, 215, 60)
    R = (235, 80, 100)
    if name == "sun":
        for k in range(12):
            a = k * math.pi / 6
            sd.line([(cx + 85 * math.cos(a), cy + 85 * math.sin(a)),
                     (cx + 125 * math.cos(a), cy + 125 * math.sin(a))], INK, 8)
        sd.circle((cx, cy), 70, fill=Y, outline=INK, width=6)
    elif name == "cloud":
        parts = [(-60, 15, 50), (0, -10, 65), (65, 15, 50), (0, 25, 45)]
        for dx, dy, r in parts:
            sd.circle((cx + dx, cy + dy), r + 6, fill=INK)
        for dx, dy, r in parts:
            sd.circle((cx + dx, cy + dy), r, fill=(255, 255, 255))
    elif name == "question":
        sd.text((cx, cy), "?", 320, (255, 200, 40), stroke=8, stroke_fill=INK)
    elif name == "lightbulb":
        for k in range(-2, 3):
            a = -math.pi / 2 + k * 0.5
            sd.line([(cx + 95 * math.cos(a), cy - 20 + 95 * math.sin(a)),
                     (cx + 130 * math.cos(a), cy - 20 + 130 * math.sin(a))], INK, 7)
        sd.circle((cx, cy - 20), 65, fill=Y, outline=INK, width=6)
        sd.rect((cx - 30, cy + 40, cx + 30, cy + 90), fill=(190, 190, 195), outline=INK, width=6)
    elif name == "star":
        pts = []
        for k in range(10):
            r = 105 if k % 2 == 0 else 45
            a = -math.pi / 2 + k * math.pi / 5
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        sd.poly(pts, fill=Y, outline=INK, width=7)
    elif name == "arrow":
        pts = [(cx - 105, cy - 32), (cx + 20, cy - 32), (cx + 20, cy - 80), (cx + 115, cy),
               (cx + 20, cy + 80), (cx + 20, cy + 32), (cx - 105, cy + 32)]
        sd.poly(pts, fill=(235, 90, 80), outline=INK, width=7)
    elif name == "clock":
        sd.circle((cx, cy), 90, fill=(255, 255, 255), outline=INK, width=7)
        for k in range(12):
            a = k * math.pi / 6
            sd.line([(cx + 74 * math.cos(a), cy + 74 * math.sin(a)),
                     (cx + 84 * math.cos(a), cy + 84 * math.sin(a))], INK, 4)
        a = t * 2.0 - math.pi / 2
        sd.line([(cx, cy), (cx + 70 * math.cos(a), cy + 70 * math.sin(a))], (220, 50, 50), 5)
        a2 = t * 0.17 - math.pi / 2
        sd.line([(cx, cy), (cx + 48 * math.cos(a2), cy + 48 * math.sin(a2))], INK, 8)
        sd.circle((cx, cy), 8, fill=INK)
    elif name == "coin":
        sd.circle((cx, cy), 85, fill=Y, outline=INK, width=7)
        sd.circle((cx, cy), 62, outline=(200, 160, 20), width=4)
        sd.text((cx, cy + 3), "$", 100, INK)
    elif name == "heart":
        sd.circle((cx - 45, cy - 35), 50, fill=R)
        sd.circle((cx + 45, cy - 35), 50, fill=R)
        sd.poly([(cx - 92, cy - 15), (cx + 92, cy - 15), (cx, cy + 100)], fill=R)
        sd.line([(cx - 95, cy - 15), (cx, cy + 105), (cx + 95, cy - 15)], INK, 7)
        sd.arc((cx - 95, cy - 85, cx - 0, cy + 15), 150, 360, INK, 7)
        sd.arc((cx + 0, cy - 85, cx + 95, cy + 15), 180, 30, INK, 7)
    elif name == "warning":
        sd.poly([(cx, cy - 100), (cx + 110, cy + 85), (cx - 110, cy + 85)], fill=Y, outline=INK, width=8)
        sd.text((cx, cy + 32), "!", 120, INK)
    elif name == "gear":
        for k in range(8):
            a = k * math.pi / 4
            sd.line([(cx + 55 * math.cos(a), cy + 55 * math.sin(a)),
                     (cx + 98 * math.cos(a), cy + 98 * math.sin(a))], INK, 32)
            sd.line([(cx + 55 * math.cos(a), cy + 55 * math.sin(a)),
                     (cx + 94 * math.cos(a), cy + 94 * math.sin(a))], (160, 165, 180), 24)
        sd.circle((cx, cy), 72, fill=(160, 165, 180), outline=INK, width=6)
        sd.circle((cx, cy), 28, fill=(255, 255, 255), outline=INK, width=6)
    elif name == "book":
        sd.rect((cx - 105, cy - 72, cx, cy + 72), fill=(255, 255, 255), outline=INK, width=7)
        sd.rect((cx, cy - 72, cx + 105, cy + 72), fill=(255, 255, 255), outline=INK, width=7)
        for k in range(4):
            sd.line([(cx - 82, cy - 40 + k * 26), (cx - 22, cy - 40 + k * 26)], (90, 130, 230), 5)
            sd.line([(cx + 22, cy - 40 + k * 26), (cx + 82, cy - 40 + k * 26)], (90, 130, 230), 5)
    elif name == "rocket":
        sd.poly([(cx - 18, cy + 50), (cx, cy + 120), (cx + 18, cy + 50)], fill=(255, 170, 40), outline=INK, width=5)
        sd.poly([(cx - 30, cy + 10), (cx - 70, cy + 70), (cx - 30, cy + 55)], fill=(235, 60, 60), outline=INK, width=6)
        sd.poly([(cx + 30, cy + 10), (cx + 70, cy + 70), (cx + 30, cy + 55)], fill=(235, 60, 60), outline=INK, width=6)
        sd.rect((cx - 34, cy - 65, cx + 34, cy + 55), fill=(255, 255, 255), outline=INK, width=7)
        sd.poly([(cx, cy - 125), (cx - 34, cy - 62), (cx + 34, cy - 62)], fill=(235, 60, 60), outline=INK, width=7)
        sd.circle((cx, cy - 20), 16, fill=(90, 190, 255), outline=INK, width=5)
    elif name == "earth":
        sd.circle((cx, cy), 90, fill=(70, 150, 235), outline=INK, width=7)
        for dx, dy, r in ((-30, -28, 30), (34, 22, 26), (-12, 48, 16), (40, -45, 14)):
            sd.circle((cx + dx, cy + dy), r, fill=(90, 200, 100))
    elif name == "chart":
        sd.line([(cx - 100, cy - 90), (cx - 100, cy + 80), (cx + 100, cy + 80)], INK, 7)
        for i, (h, c) in enumerate(((55, (255, 170, 60)), (100, (90, 170, 255)), (150, (90, 210, 130)))):
            x0 = cx - 75 + i * 58
            sd.rect((x0, cy + 74 - h, x0 + 44, cy + 74), fill=c, outline=INK, width=5)
    elif name == "phone":
        sd.rect((cx - 50, cy - 100, cx + 50, cy + 100), fill=(40, 40, 55), outline=INK, width=7, radius=16)
        sd.rect((cx - 40, cy - 80, cx + 40, cy + 72), fill=(120, 200, 255))
        for i in range(2):
            for j in range(2):
                sd.rect((cx - 30 + j * 34, cy - 66 + i * 34, cx - 6 + j * 34, cy - 42 + i * 34),
                        fill=[(255, 110, 110), (255, 200, 60), (110, 220, 140), (180, 130, 255)][i * 2 + j],
                        radius=6)
        sd.circle((cx, cy + 86), 6, fill=(255, 255, 255))
    elif name == "lock":
        sd.arc((cx - 42, cy - 95, cx + 42, cy + 5), 180, 360, INK, 22)
        sd.arc((cx - 42, cy - 95, cx + 42, cy + 5), 180, 360, (170, 175, 190), 12)
        sd.rect((cx - 62, cy - 12, cx + 62, cy + 90), fill=Y, outline=INK, width=7, radius=12)
        sd.circle((cx, cy + 34), 12, fill=INK)
        sd.rect((cx - 5, cy + 36, cx + 5, cy + 66), fill=INK)
    elif name == "check":
        sd.circle((cx, cy), 92, fill=(60, 195, 105), outline=INK, width=7)
        sd.line([(cx - 48, cy + 2), (cx - 12, cy + 38), (cx + 50, cy - 36)], (255, 255, 255), 20)
    elif name == "cross":
        sd.circle((cx, cy), 92, fill=(235, 65, 65), outline=INK, width=7)
        sd.line([(cx - 40, cy - 40), (cx + 40, cy + 40)], (255, 255, 255), 20)
        sd.line([(cx - 40, cy + 40), (cx + 40, cy - 40)], (255, 255, 255), 20)
    elif name == "atom":
        for ang in (0, 60, 120):
            f = math.radians(ang)
            pts = []
            for k in range(0, 63):
                th = k * 0.1
                x, y = 100 * math.cos(th), 36 * math.sin(th)
                pts.append((cx + x * math.cos(f) - y * math.sin(f), cy + x * math.sin(f) + y * math.cos(f)))
            sd.line(pts, INK, 6)
            sd.circle(pts[10], 9, fill=(90, 170, 255), outline=INK, width=3)
        sd.circle((cx, cy), 20, fill=(235, 70, 70), outline=INK, width=5)
    elif name == "rainbow":
        cols = [(235, 60, 60), (255, 150, 40), (250, 215, 60), (90, 200, 110), (80, 150, 255), (160, 90, 230)]
        for i, c in enumerate(cols):
            r = 115 - i * 14
            sd.arc((cx - r, cy - r + 40, cx + r, cy + r + 40), 180, 360, c, 15)
        for sx in (-1, 1):
            sd.circle((cx + sx * 108, cy + 44), 24, fill=(255, 255, 255), outline=INK, width=4)
    elif name == "magnifier":
        sd.line([(cx + 40, cy + 40), (cx + 100, cy + 100)], INK, 30)
        sd.line([(cx + 40, cy + 40), (cx + 100, cy + 100)], (150, 90, 50), 20)
        sd.circle((cx - 15, cy - 15), 62, fill=(210, 240, 255), outline=INK, width=9)
        sd.arc((cx - 50, cy - 50, cx + 20, cy + 20), 200, 260, (255, 255, 255), 6)
    elif name == "brain":
        pink = (255, 170, 195)
        sd.circle((cx - 34, cy), 56, fill=pink, outline=INK, width=6)
        sd.circle((cx + 34, cy), 56, fill=pink, outline=INK, width=6)
        sd.circle((cx - 34, cy), 50, fill=pink)
        sd.circle((cx + 34, cy), 50, fill=pink)
        sd.line([(cx, cy - 52), (cx, cy + 50)], INK, 4)
        sd.arc((cx - 70, cy - 30, cx - 20, cy + 20), 200, 340, INK, 4)
        sd.arc((cx + 20, cy - 30, cx + 70, cy + 20), 200, 340, INK, 4)
        sd.arc((cx - 60, cy + 5, cx - 15, cy + 45), 20, 160, INK, 4)
        sd.arc((cx + 15, cy + 5, cx + 60, cy + 45), 20, 160, INK, 4)


_SPRITES = {}


def prop_sprite(name, t):
    if name == "clock":  # the clock hands move, so redraw
        im = Image.new("RGBA", (800, 800), (0, 0, 0, 0))
        draw_prop(SD(im, 2), name, 200, 200, t)
        return im.reduce(2)
    if name not in _SPRITES:
        im = Image.new("RGBA", (800, 800), (0, 0, 0, 0))
        draw_prop(SD(im, 2), name, 200, 200)
        _SPRITES[name] = im.reduce(2)
    return _SPRITES[name]


SPIN = {"sun": 25, "gear": 40, "atom": 30, "star": 20}


def paste_prop(img, name, x, y, t, k=1.2):
    if name == "none":
        return
    sp = prop_sprite(name, t)
    s = ease_out_back((t - 0.25) / 0.55) * k * (1 + 0.04 * math.sin(t * 4))
    if s < 0.02:
        return
    ang = t * SPIN[name] if name in SPIN else 5 * math.sin(t * 2.5)
    sp = sp.rotate(ang, resample=Image.BICUBIC)
    size = max(2, int(400 * s))
    sp = sp.resize((size, size), Image.BILINEAR)
    yy = y + 10 * math.sin(t * 3)
    img.paste(sp, (int(x - size / 2), int(yy - size / 2)), sp)


# ------------------------------------------------------------ backgrounds ---
THEMES = {
    "sky":    dict(g=[((60, 150, 255), (200, 240, 255)), ((30, 110, 235), (255, 240, 190))], floor=(95, 200, 110)),
    "sunset": dict(g=[((255, 110, 60), (255, 215, 120)), ((170, 60, 150), (255, 170, 90))], floor=(95, 45, 100)),
    "night":  dict(g=[((12, 18, 60), (55, 55, 140)), ((30, 12, 80), (75, 45, 150))], floor=(35, 45, 100)),
    "space":  dict(g=[((8, 4, 28), (70, 12, 100)), ((4, 10, 48), (100, 20, 120))], floor=(80, 78, 108)),
    "city":   dict(g=[((255, 150, 80), (255, 225, 160)), ((80, 80, 200), (255, 170, 170))], floor=(85, 85, 100)),
    "forest": dict(g=[((120, 215, 150), (235, 252, 205)), ((70, 175, 120), (250, 240, 170))], floor=(75, 160, 85)),
    "ocean":  dict(g=[((40, 170, 235), (210, 247, 255)), ((25, 120, 215), (255, 232, 190))], floor=(238, 212, 150)),
    "lab":    dict(g=[((232, 238, 255), (205, 222, 250)), ((250, 232, 255), (222, 212, 250))], floor=(165, 175, 210)),
    "stage":  dict(g=[((60, 15, 100), (155, 40, 165)), ((105, 20, 125), (235, 70, 140))], floor=(155, 95, 65)),
    "tech":   dict(g=[((8, 8, 35), (90, 15, 140)), ((15, 5, 60), (140, 20, 150))], floor=(22, 10, 55)),
}
_GRADS, _STATIC = {}, {}


def gradient(c0, c1):
    im = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(im)
    for y in range(H):
        f = y / (H - 1)
        d.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * f) for a, b in zip(c0, c1)))
    return im


def grads(theme):
    if theme not in _GRADS:
        a, b = THEMES[theme]["g"]
        _GRADS[theme] = (gradient(*a), gradient(*b))
    return _GRADS[theme]


def static_layer(theme):
    if theme in _STATIC:
        return _STATIC[theme]
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = SD(im)
    fl = THEMES[theme]["floor"]
    rnd = random.Random(theme)
    if theme in ("sky", "sunset", "night"):
        c1 = {"sky": (75, 185, 100), "sunset": (130, 55, 115), "night": (45, 55, 120)}[theme]
        c2 = darker(c1, 0.85)
        for box in ((-250, 440, 550, 900), (350, 470, 1150, 900), (850, 420, 1650, 900)):
            sd.ellipse(box, fill=c1)
        for box in ((-150, 520, 450, 900), (550, 540, 1350, 900)):
            sd.ellipse(box, fill=c2)
    elif theme == "city":
        for row, (col, hmin, hmax) in enumerate((((150, 120, 170), 200, 400), ((105, 95, 140), 130, 300))):
            x = -20
            while x < W:
                w = rnd.randint(70, 130)
                h = rnd.randint(hmin, hmax)
                sd.rect((x, FLOOR_Y - h, x + w, FLOOR_Y), fill=col)
                for wy in range(FLOOR_Y - h + 20, FLOOR_Y - 20, 34):
                    for wx in range(x + 12, x + w - 18, 26):
                        if rnd.random() < 0.55:
                            sd.rect((wx, wy, wx + 12, wy + 18), fill=(255, 235, 140))
                x += w + rnd.randint(4, 20)
    elif theme == "forest":
        for layer, (col, size) in enumerate((((60, 150, 95), 190), ((40, 120, 75), 240))):
            x = -50 + layer * 60
            while x < W + 50:
                h = size + rnd.randint(-30, 40)
                sd.rect((x - 10, FLOOR_Y - 60, x + 10, FLOOR_Y), fill=(110, 75, 50))
                for k in range(3):
                    sd.poly([(x, FLOOR_Y - h + k * 55), (x - 70 + k * 8, FLOOR_Y - 60 - (2 - k) * 8 - 55 * (2 - k) + 0),
                             (x + 70 - k * 8, FLOOR_Y - 60 - (2 - k) * 8 - 55 * (2 - k) + 0)], fill=col)
                x += 150 + rnd.randint(-20, 30)
    elif theme == "space":
        for _ in range(7):
            x, r = rnd.randint(0, W), rnd.randint(18, 50)
            sd.ellipse((x - r, FLOOR_Y + 20, x + r, FLOOR_Y + 20 + r // 2), fill=darker(fl, 0.8))
    elif theme == "lab":
        sd.rect((0, 120, W, 135), fill=(190, 170, 140))
        sd.rect((0, 320, W, 335), fill=(190, 170, 140))
        for row_y in (120, 320):
            x = 60
            while x < W - 40:
                c = rnd.choice([(255, 110, 110), (110, 200, 255), (130, 230, 140), (255, 210, 90)])
                h = rnd.randint(50, 95)
                sd.rect((x, row_y - h, x + 36, row_y), fill=c, outline=INK, width=3, radius=6)
                sd.rect((x + 10, row_y - h - 14, x + 26, row_y - h), fill=(230, 230, 235), outline=INK, width=3)
                x += rnd.randint(70, 130)
    elif theme == "stage":
        for sx, x0 in ((-1, 0), (1, W)):
            for k in range(7):
                xx = x0 + sx * (k * 34)
                sd.rect((min(xx, xx + sx * 34), 0, max(xx, xx + sx * 34), FLOOR_Y + 5),
                        fill=(190, 30, 50) if k % 2 == 0 else (150, 20, 40))
        for k in range(1, 8):
            sd.line([(0, FLOOR_Y + k * 22), (W, FLOOR_Y + k * 22)], darker(fl, 0.8), 2)
    elif theme == "tech":
        for pts in (((0, 400), (140, 300), (260, 380), (380, 260), (520, 400)),
                    ((760, 400), (900, 290), (1020, 370), (1150, 250), (1280, 400))):
            sd.poly(list(pts) + [(pts[-1][0], 402), (pts[0][0], 402)], fill=(40, 15, 90))
    top = 400 if theme == "tech" else FLOOR_Y
    sd.rect((0, top, W, H), fill=fl)
    if theme not in ("tech", "ocean"):
        sd.line([(0, FLOOR_Y), (W, FLOOR_Y)], INK, 5)
    _STATIC[theme] = im
    return im


_STARS = [(random.Random(i).uniform(0, W), random.Random(i + 99).uniform(0, 400),
           random.Random(i + 7).uniform(1.5, 3.6), random.Random(i + 3).uniform(0, 6.28)) for i in range(80)]


def draw_stars(sd, t, scroll=0.0):
    for x, y, r, ph in _STARS:
        b = 0.55 + 0.45 * math.sin(t * 3 + ph)
        c = int(140 + 115 * b)
        sd.circle(((x - scroll) % W, y), r * (0.8 + 0.4 * b), fill=(c, c, min(255, c + 20)))


def draw_sun(sd, cx, cy, r, t, col=(255, 222, 70), ray=(255, 238, 130)):
    for k in range(14):
        a = t * 0.6 + k * 2 * math.pi / 14
        r1, r2 = r + 14, r + 40 + 8 * math.sin(t * 3 + k)
        sd.line([(cx + r1 * math.cos(a), cy + r1 * math.sin(a)),
                 (cx + r2 * math.cos(a), cy + r2 * math.sin(a))], ray, 9)
    sd.circle((cx, cy), r, fill=col)


def draw_cloud(sd, x, y, s, col=(255, 255, 255)):
    for dx, dy, r in ((-45, 8, 32), (0, -8, 44), (48, 8, 34), (5, 18, 30)):
        sd.circle((x + dx * s, y + dy * s), r * s, fill=col)


def confetti(sd, t, n=45):
    cols = [(255, 80, 80), (255, 200, 40), (90, 200, 120), (90, 150, 255), (200, 100, 255), (255, 120, 190)]
    for i in range(n):
        r = random.Random(i)
        x = (r.uniform(0, W) + 30 * math.sin(t * 2 + i)) % W
        y = (r.uniform(0, H) + t * (110 + r.uniform(0, 90))) % (H + 40) - 20
        a = t * (3 + r.uniform(0, 3)) + i
        w, h = 12, 6
        pts = [(x + px * math.cos(a) - py * math.sin(a), y + px * math.sin(a) + py * math.cos(a))
               for px, py in ((-w, -h), (w, -h), (w, h), (-w, h))]
        sd.poly(pts, fill=cols[i % len(cols)])


def dyn_back(theme, sd, t):
    if theme == "sky":
        draw_sun(sd, 1080, 140, 68, t)
        for i, (x0, y0, s, sp) in enumerate(((100, 130, 1.0, 22), (520, 90, 0.8, 15), (860, 230, 1.3, 30), (300, 260, 0.7, 12))):
            draw_cloud(sd, (x0 + t * sp) % (W + 400) - 200, y0, s)
    elif theme == "sunset":
        draw_sun(sd, 640, 470 + 6 * math.sin(t), 120, t, (255, 235, 130), (255, 200, 110))
        for x0, y0, s, sp in ((150, 120, 1.0, 18), (760, 180, 1.2, 25)):
            draw_cloud(sd, (x0 + t * sp) % (W + 400) - 200, y0, s, (255, 190, 170))
        for i in range(4):  # birds
            bx = (200 + i * 90 + t * 60) % (W + 200) - 100
            by = 200 + i * 28 + 10 * math.sin(t * 2 + i)
            fl = 10 * math.sin(t * 9 + i)
            sd.line([(bx - 18, by - fl), (bx, by), (bx + 18, by - fl)], (70, 30, 70), 4)
    elif theme == "night":
        draw_stars(sd, t)
        sd.circle((1060, 140), 82, fill=(255, 246, 200))
        sd.circle((1060, 140), 66, fill=(255, 250, 220))
        for dx, dy, r in ((-18, -12, 12), (22, 18, 9), (-4, 28, 7)):
            sd.circle((1060 + dx, 140 + dy), r, fill=(235, 225, 175))
        p = (t % 4.0) / 0.7
        if p < 1:
            x0, y0 = 300 + p * 380, 60 + p * 190
            sd.line([(x0 - 90, y0 - 45), (x0, y0)], (255, 255, 255), 4)
    elif theme == "space":
        draw_stars(sd, t, scroll=t * 25)
        cx, cy = 1010, 215 + 8 * math.sin(t)
        sd.arc((cx - 160, cy - 34, cx + 160, cy + 34), 180, 360, (255, 220, 160), 9)
        sd.circle((cx, cy), 88, fill=(255, 150, 60))
        for k, c in enumerate(((240, 120, 50), (255, 175, 90), (235, 110, 45))):
            sd.arc((cx - 80 + k * 6, cy - 80 + k * 10, cx + 80 - k * 6, cy + 80 - k * 10), 20 + k * 30, 160 - k * 10, c, 9)
        sd.arc((cx - 160, cy - 34, cx + 160, cy + 34), 0, 180, (255, 220, 160), 9)
        sd.circle((200 + 30 * math.sin(t * 0.5), 150), 30, fill=(90, 170, 255))
    elif theme == "city":
        draw_sun(sd, 220, 150, 55, t, (255, 240, 170), (255, 220, 140))
        for x0, y0, s, sp in ((500, 110, 1.0, 20), (1000, 200, 0.9, 14)):
            draw_cloud(sd, (x0 + t * sp) % (W + 400) - 200, y0, s, (255, 255, 255))
    elif theme == "forest":
        draw_sun(sd, 1100, 120, 55, t, (255, 245, 150), (255, 250, 200))
    elif theme == "ocean":
        draw_sun(sd, 1050, 130, 62, t)
        for x0, y0, s, sp in ((200, 110, 1.0, 16), (640, 190, 0.8, 22)):
            draw_cloud(sd, (x0 + t * sp) % (W + 400) - 200, y0, s)
        for k, (y0, col) in enumerate(((420, (90, 190, 240)), (470, (55, 150, 225)), (520, (30, 120, 205)))):
            pts = [(x, y0 + 12 * math.sin(x / 90 + t * 1.6 + k * 1.7)) for x in range(-20, W + 40, 24)]
            sd.poly(pts + [(W + 20, H), (-20, H)], fill=col)
        bx = (t * 45) % (W + 300) - 150
        sd.poly([(bx, 330), (bx, 415), (bx + 70, 415)], fill=(255, 255, 255), outline=INK, width=4)
        sd.poly([(bx - 40, 420), (bx + 90, 420), (bx + 65, 445), (bx - 15, 445)], fill=(200, 80, 60), outline=INK, width=4)
    elif theme == "stage":
        for i, x0 in enumerate((260, 640, 1020)):
            tx = x0 + 240 * math.sin(t * 1.2 + i * 2)
            sd.poly([(x0 - 22, 0), (x0 + 22, 0), (tx + 130, FLOOR_Y), (tx - 130, FLOOR_Y)], fill=(215, 130, 215))
    elif theme == "tech":
        draw_stars(sd, t * 0.5, 0)
        sd.circle((640, 372), 150, fill=(255, 110, 160))
        sd.circle((640, 372), 150, outline=(255, 190, 90), width=10)
        for k in range(7):
            y0 = 350 + k * 16
            sd.rect((470, y0, 810, y0 + 3 + k * 1.6), fill=(30, 10, 70))


def dyn_front(theme, sd, t):
    if theme == "city":
        for i in range(-1, 9):
            x = (i * 180 - t * 200) % (W + 180) - 90
            sd.rect((x, 648, x + 90, 658), fill=(240, 240, 240))
    elif theme == "forest":
        for i in range(12):
            r = random.Random(i)
            x = (r.uniform(0, W) + 50 * math.sin(t * 1.3 + i)) % W
            y = (r.uniform(0, H) + t * (50 + 25 * r.random())) % (H + 30) - 15
            sd.ellipse((x - 8, y - 4, x + 8, y + 4), fill=[(255, 170, 40), (230, 90, 40), (250, 210, 60)][i % 3])
    elif theme == "lab":
        for col_x in (150, 1130):
            for i in range(9):
                r = random.Random(i + int(col_x))
                y = 560 - ((t * (40 + 25 * r.random()) + r.uniform(0, 400)) % 420)
                x = col_x + 26 * math.sin(t * 2 + i) + r.uniform(-20, 20)
                rad = 6 + 10 * r.random()
                sd.circle((x, y), rad, outline=(120, 170, 235), width=3)
    elif theme == "stage":
        confetti(sd, t)
    elif theme == "tech":
        for i in range(12):
            p = ((i + t * 0.7) % 12) / 12
            y = 400 + (H - 400) * p ** 2
            sd.line([(0, y), (W, y)], (255, 60, 200), 1 + 4 * p)
        for j in range(-12, 13):
            sd.line([(640 + j * 12, 400), (640 + j * 170, H)], (255, 60, 200), 2)
    elif theme == "night":
        pass


# --------------------------------------------------------------- frame ---
def chunk_words(text, n=6):
    w = text.split()
    return [" ".join(w[i:i + n]) for i in range(0, len(w), n)] or [""]


class SceneState:
    def __init__(self, sc, dur, idx):
        self.sc, self.dur, self.idx = sc, dur, idx
        self.accent = ACCENTS[idx % len(ACCENTS)]
        self.chunks = chunk_words(sc["narration"])
        lens = [max(1, len(c)) for c in self.chunks]
        tot = float(sum(lens))
        acc, self.bounds = 0.0, []
        for L in lens:
            self.bounds.append((acc / tot, (acc + L) / tot))
            acc += L
        n = len(sc["chars"])
        self.bases = [360.0] if n == 1 else [260.0, 1020.0]
        self.prop_xy = (920, 350) if n == 1 else (640, 330)
        self.has_party = any(a in ("cheer", "dance") for _, a in sc["chars"])


T_IN = 0.9


def char_state(st, i, who, action, t):
    n = len(st.sc["chars"])
    base = st.bases[i]
    home_face = 1 if i == 0 else -1
    delay = 0.15 * i
    start = -180.0 if i == 0 else W + 180.0
    u = (t - delay) / T_IN
    if u < 1.0:
        x = start + (base - start) * ease_out(u)
        face = 1 if base > start else -1
        return x, face, pose_for("walk", t), "idle"
    x, face = base, home_face
    if action in ("walk", "run"):
        amp = (200 if n == 1 else 130) * (1.3 if action == "run" else 1.0)
        w = 0.9 if action == "walk" else 1.5
        tt = t - delay - T_IN
        x = base + amp * math.sin(w * tt)
        face = 1 if math.cos(w * tt) >= 0 else -1
    return x, face, pose_for(action, t), action


def render_frame(st, t):
    sc = st.sc
    th = sc["theme"]
    ga, gb = grads(th)
    img = Image.blend(ga, gb, 0.5 + 0.5 * math.sin(t * 0.7 + st.idx))
    sd = SD(img)
    dyn_back(th, sd, t)
    layer = static_layer(th)
    img.paste(layer, (0, 0), layer)
    dyn_front(th, sd, t)
    if st.has_party and th != "stage":
        confetti(sd, t, 32)

    paste_prop(img, sc["prop"], st.prop_xy[0], st.prop_xy[1], t)

    speaking = 0.25 < t < st.dur - 0.45
    speaker = 0 if (len(sc["chars"]) == 1 or t < st.dur * 0.5) else 1
    fl = THEMES[th]["floor"]
    for i, (who, action) in enumerate(sc["chars"]):
        x, face, pose, mood = char_state(st, i, who, action, t)
        jump = pose["jump"]
        sh_w = 52 * (1 - 0.3 * min(1.0, jump / 80))
        sd.ellipse((x - sh_w, GROUND - 2, x + sh_w, GROUND + 14), fill=darker(fl, 0.65))
        spr = draw_char(who, pose, t, face, speaking and i == speaker, mood, seed=i + st.idx)
        img.paste(spr, (int(x - CW / 2), int(GROUND - FY - jump)), spr)

    # label chip that drops in
    label = sc["label"]
    if label:
        size = 62
        while size > 30 and text_width(label, size) > W - 220:
            size -= 4
        tw = text_width(label, size)
        u = (t - 0.1) / 0.5
        cy = 80 - (1 - ease_out_back(u)) * 150 + 3 * math.sin(t * 2.2)
        bw, bh = tw / 2 + 44, size / 2 + 26
        sd.rect((W / 2 - bw + 6, cy - bh + 8, W / 2 + bw + 6, cy + bh + 8), fill=(20, 20, 30), radius=24)
        sd.rect((W / 2 - bw, cy - bh, W / 2 + bw, cy + bh), fill=st.accent, outline=INK, width=6, radius=24)
        sd.text((W / 2, cy + 2), label, size, (255, 255, 255), stroke=5, stroke_fill=INK)

    # captions
    if CAPTIONS and t > 0.25:
        f = min(1.0, (t - 0.25) / max(0.1, st.dur - 0.7))
        ci = len(st.bounds) - 1
        for k, (a, b) in enumerate(st.bounds):
            if f < b:
                ci = k
                break
        text = st.chunks[ci]
        size = 42
        while size > 24 and text_width(text, size) > W - 120:
            size -= 2
        sd.text((W / 2, 660), text, size, (255, 255, 255), stroke=7, stroke_fill=INK)

    # camera: quick punch-in at the start, then a slow push
    z = 1.0 + 0.06 * (t / st.dur) + 0.10 * (1 - min(1.0, t / 0.4)) ** 2
    if z > 1.001:
        bw, bh = W / z, H / z
        x0, y0 = (W - bw) / 2, (H - bh) / 2
        img = img.resize((W, H), Image.BILINEAR, box=(x0, y0, x0 + bw, y0 + bh))
    return img


# ----------------------------------------------------------------- video ---
def render_scene(i, sc, audio):
    dur = duration(audio) + 0.4
    n = int(math.ceil(dur * FPS))
    clip = TMP / f"s{i:02d}.mp4"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-i", str(audio), "-af", "apad",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-ar", "44100", "-ac", "2", "-t", f"{dur:.2f}", str(clip)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    st = SceneState(sc, dur, i - 1)
    for f in range(n):
        proc.stdin.write(render_frame(st, f / FPS).tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise SystemExit("ffmpeg failed while building a scene")
    return clip


def main():
    OUT.mkdir(exist_ok=True)
    TMP.mkdir(parents=True, exist_ok=True)
    print(f"Topic: {TOPIC}", flush=True)
    data = get_script()
    scenes = data["scenes"]
    print(f"Got {len(scenes)} scenes: {data.get('title')}", flush=True)
    clips = []
    for i, sc in enumerate(scenes, 1):
        t0 = time.time()
        print(f"Scene {i}/{len(scenes)}: {sc['label']} [{sc['theme']}]", flush=True)
        audio = make_voice(sc["narration"], str(TMP / f"s{i:02d}"))
        clips.append(render_scene(i, sc, Path(audio)))
        print(f"  done in {time.time() - t0:.0f}s", flush=True)
    lst = TMP / "all.txt"
    lst.write_text("\n".join(f"file '{c.resolve()}'" for c in clips))
    final = OUT / "final.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", str(lst), "-c", "copy", str(final)], check=True)
    info = [f"TITLE: {data.get('title', TOPIC)}", "",
            f"DESCRIPTION: {data.get('description', '')}", "", "SCRIPT:"]
    info += [f"{i}. {s['narration']}" for i, s in enumerate(scenes, 1)]
    (OUT / "info.txt").write_text("\n".join(info))
    print(f"Done: {final} ({duration(final):.0f} seconds)", flush=True)


if __name__ == "__main__":
    main()
