"""
Free stick-figure explainer video maker.
Topic -> script (Gemini free tier) -> voice (edge-tts, free)
      -> stick-figure pictures (Pillow) -> video (ffmpeg).
Runs on GitHub Actions. Output: output/final.mp4 + output/info.txt
"""
import asyncio
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

TOPIC = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TOPIC", "")).strip()
TOPIC = TOPIC or "Why is the sky blue?"
SCENES = int(os.environ.get("SCENES") or 12)
VOICE = os.environ.get("VOICE", "en-US-GuyNeural")

W, H = 1280, 720
GROUND = 600
INK = (30, 30, 30)
OUT = Path("output")
TMP = OUT / "tmp"

BGS = {
    "peach": (250, 190, 145),
    "blue": (170, 215, 235),
    "white": (255, 255, 255),
    "gray": (205, 205, 210),
    "green": (190, 225, 190),
}
POSES = ["stand", "think", "happy", "point", "surprised", "sad", "walk"]
PROPS = ["none", "sun", "cloud", "question", "lightbulb", "star", "arrow",
         "clock", "coin", "heart", "warning", "gear", "book"]

PROMPT = """You write scripts for a stick-figure animated explainer YouTube video.
Topic: {topic}
Write exactly {n} scenes. Scene 1 is a curious hook. The last scene is a short
conclusion that also asks viewers to subscribe. Keep the tone friendly and simple.

Return ONLY valid JSON, no other text, in this shape:
{{"title": "catchy YouTube title",
  "description": "2-3 sentence YouTube description",
  "scenes": [
    {{"narration": "2-3 spoken sentences, no emojis, no stage directions",
      "label": "2-5 word caption shown on screen",
      "bg": one of {bgs},
      "pose": one of {poses},
      "prop": one of {props}}}
  ]}}
Vary bg, pose and prop from scene to scene and choose ones that fit the words."""


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


def get_script():
    prompt = PROMPT.format(topic=TOPIC, n=SCENES, bgs=list(BGS), poses=POSES, props=PROPS)
    raw = ask_llm(prompt)
    raw = re.sub(r"```(?:json)?", "", raw)
    data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    scenes = []
    for s in data["scenes"]:
        scenes.append({
            "narration": str(s.get("narration", "")).strip(),
            "label": str(s.get("label", "")).strip(),
            "bg": s.get("bg") if s.get("bg") in BGS else "peach",
            "pose": s.get("pose") if s.get("pose") in POSES else "stand",
            "prop": s.get("prop") if s.get("prop") in PROPS else "none",
        })
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


# --------------------------------------------------------------- drawing ---
def font(size):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def darker(c, f=0.85):
    return tuple(int(v * f) for v in c)


def centered_text(d, text, cx, cy, size, fill=INK):
    f = font(size)
    while size > 24 and d.textlength(text, font=f) > W - 160:
        size -= 4
        f = font(size)
    d.text((cx, cy), text, font=f, fill=fill, anchor="mm")


ARMS = {  # (left elbow, left hand, right elbow, right hand) relative to shoulder
    "stand": ((-25, 40), (-40, 85), (25, 40), (40, 85)),
    "think": ((-25, 40), (-40, 85), (45, 25), (18, -22)),
    "happy": ((-45, -15), (-60, -70), (45, -15), (60, -70)),
    "point": ((-25, 40), (-40, 85), (55, 5), (115, -10)),
    "surprised": ((-45, 25), (-90, 5), (45, 25), (90, 5)),
    "sad": ((-15, 45), (-20, 95), (15, 45), (20, 95)),
    "walk": ((-30, 30), (-55, 70), (30, 30), (55, 65)),
}
LEGS = {  # foot offsets relative to hip
    "walk": ((-45, 105), (50, 105)),
}


def draw_figure(d, cx, pose, dy):
    lw = 7
    head_r = 40
    hip_y = GROUND - 115 + dy
    neck_y = hip_y - 125
    head_y = neck_y - head_r
    sh = neck_y + 22
    d.line([(cx, neck_y), (cx, hip_y)], fill=INK, width=lw)
    lf, rf = LEGS.get(pose, ((-38, 115), (38, 115)))
    for fx, fy in (lf, rf):
        d.line([(cx, hip_y), (cx + fx, hip_y + fy - dy)], fill=INK, width=lw, joint="curve")
    le, lh, re_, rh = ARMS[pose]
    for e, h in ((le, lh), (re_, rh)):
        d.line([(cx, sh), (cx + e[0], sh + e[1]), (cx + h[0], sh + h[1])],
               fill=INK, width=lw, joint="curve")
    d.ellipse([cx - head_r, head_y - head_r, cx + head_r, head_y + head_r],
              fill="white", outline=INK, width=lw)
    ex = 15
    ey = head_y - 6
    if pose == "surprised":
        for s in (-1, 1):
            d.ellipse([cx + s * ex - 6, ey - 6, cx + s * ex + 6, ey + 6], outline=INK, width=3)
        d.ellipse([cx - 8, head_y + 12, cx + 8, head_y + 30], outline=INK, width=4)
    else:
        for s in (-1, 1):
            d.ellipse([cx + s * ex - 4, ey - 4, cx + s * ex + 4, ey + 4], fill=INK)
        if pose in ("happy", "walk", "stand", "point"):
            d.arc([cx - 18, head_y + 2, cx + 18, head_y + 28], 15, 165, fill=INK, width=4)
        elif pose == "sad":
            d.arc([cx - 16, head_y + 16, cx + 16, head_y + 40], 195, 345, fill=INK, width=4)
        else:  # think
            d.line([(cx - 12, head_y + 20), (cx + 12, head_y + 20)], fill=INK, width=4)
    if pose == "think":  # thought bubble
        for r, (bx, by) in ((6, (cx + 55, head_y - 45)), (10, (cx + 75, head_y - 75))):
            d.ellipse([bx - r, by - r, bx + r, by + r], fill="white", outline=INK, width=3)
        d.ellipse([cx + 70, head_y - 175, cx + 200, head_y - 90], fill="white", outline=INK, width=4)
        centered_text(d, "?", cx + 135, head_y - 133, 60)


def outline_circle(d, cx, cy, r, fill, w=6):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill, outline=INK, width=w)


def draw_prop(d, name, cx, cy):
    if name == "none":
        return
    Y = (250, 215, 60)
    if name == "sun":
        for k in range(12):
            a = k * math.pi / 6
            d.line([(cx + 85 * math.cos(a), cy + 85 * math.sin(a)),
                    (cx + 125 * math.cos(a), cy + 125 * math.sin(a))], fill=INK, width=7)
        outline_circle(d, cx, cy, 70, Y)
    elif name == "cloud":
        parts = [(-60, 15, 50), (0, -10, 65), (65, 15, 50), (0, 25, 45)]
        for dx, dy, r in parts:
            d.ellipse([cx + dx - r - 6, cy + dy - r - 6, cx + dx + r + 6, cy + dy + r + 6], fill=INK)
        for dx, dy, r in parts:
            d.ellipse([cx + dx - r, cy + dy - r, cx + dx + r, cy + dy + r], fill="white")
    elif name == "question":
        centered_text(d, "?", cx, cy, 300)
    elif name == "lightbulb":
        for k in range(-2, 3):
            a = -math.pi / 2 + k * 0.5
            d.line([(cx + 95 * math.cos(a), cy - 20 + 95 * math.sin(a)),
                    (cx + 130 * math.cos(a), cy - 20 + 130 * math.sin(a))], fill=INK, width=6)
        outline_circle(d, cx, cy - 20, 65, Y)
        d.rectangle([cx - 30, cy + 40, cx + 30, cy + 90], fill=(190, 190, 195), outline=INK, width=6)
    elif name == "star":
        pts = []
        for k in range(10):
            r = 100 if k % 2 == 0 else 42
            a = -math.pi / 2 + k * math.pi / 5
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        d.polygon(pts, fill=Y, outline=INK, width=6)
    elif name == "arrow":
        pts = [(cx - 100, cy - 30), (cx + 20, cy - 30), (cx + 20, cy - 75),
               (cx + 110, cy), (cx + 20, cy + 75), (cx + 20, cy + 30), (cx - 100, cy + 30)]
        d.polygon(pts, fill=(235, 90, 80), outline=INK, width=6)
    elif name == "clock":
        outline_circle(d, cx, cy, 85, "white")
        d.line([(cx, cy), (cx, cy - 55)], fill=INK, width=7)
        d.line([(cx, cy), (cx + 40, cy + 20)], fill=INK, width=7)
    elif name == "coin":
        outline_circle(d, cx, cy, 80, Y)
        centered_text(d, "$", cx, cy, 90)
    elif name == "heart":
        d.ellipse([cx - 90, cy - 80, cx, cy + 10], fill=(235, 80, 100), outline=INK, width=6)
        d.ellipse([cx, cy - 80, cx + 90, cy + 10], fill=(235, 80, 100), outline=INK, width=6)
        d.polygon([(cx - 88, cy - 15), (cx + 88, cy - 15), (cx, cy + 100)],
                  fill=(235, 80, 100), outline=INK, width=6)
        d.ellipse([cx - 80, cy - 72, cx - 5, cy + 2], fill=(235, 80, 100))
        d.ellipse([cx + 5, cy - 72, cx + 80, cy + 2], fill=(235, 80, 100))
        d.polygon([(cx - 80, cy - 12), (cx + 80, cy - 12), (cx, cy + 90)], fill=(235, 80, 100))
    elif name == "warning":
        d.polygon([(cx, cy - 95), (cx + 105, cy + 80), (cx - 105, cy + 80)],
                  fill=Y, outline=INK, width=7)
        centered_text(d, "!", cx, cy + 30, 110)
    elif name == "gear":
        for k in range(8):
            a = k * math.pi / 4
            d.line([(cx + 55 * math.cos(a), cy + 55 * math.sin(a)),
                    (cx + 95 * math.cos(a), cy + 95 * math.sin(a))], fill=INK, width=26)
        outline_circle(d, cx, cy, 72, (170, 170, 180))
        outline_circle(d, cx, cy, 28, "white")
    elif name == "book":
        d.rectangle([cx - 100, cy - 70, cx, cy + 70], fill="white", outline=INK, width=6)
        d.rectangle([cx, cy - 70, cx + 100, cy + 70], fill="white", outline=INK, width=6)
        for k in range(3):
            d.line([(cx - 80, cy - 30 + k * 30), (cx - 20, cy - 30 + k * 30)], fill=INK, width=4)
            d.line([(cx + 20, cy - 30 + k * 30), (cx + 80, cy - 30 + k * 30)], fill=INK, width=4)


def draw_frame(scene, bob, path):
    bg = BGS[scene["bg"]]
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    d.rectangle([0, GROUND - 5, W, H], fill=darker(bg))
    d.line([(0, GROUND - 5), (W, GROUND - 5)], fill=INK, width=4)
    if scene["label"]:
        centered_text(d, scene["label"], W // 2, 70, 68)
    fig_x = 400 if scene["prop"] != "none" else W // 2
    draw_figure(d, fig_x, scene["pose"], -8 if bob else 0)
    draw_prop(d, scene["prop"], 900, 370 + (-8 if bob else 0))
    img.save(path)


# ----------------------------------------------------------------- video ---
def build_scene_clip(i, scene, audio):
    a, b = TMP / f"s{i:02d}_a.png", TMP / f"s{i:02d}_b.png"
    draw_frame(scene, False, a)
    draw_frame(scene, True, b)
    dur = duration(audio) + 0.4
    lines, t, k = [], 0.0, 0
    while t < dur:
        lines.append(f"file '{(a if k % 2 == 0 else b).resolve()}'\nduration 0.5")
        t += 0.5
        k += 1
    lines.append(f"file '{a.resolve()}'")
    lst = TMP / f"s{i:02d}.txt"
    lst.write_text("\n".join(lines))
    clip = TMP / f"s{i:02d}.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-i", str(audio), "-vf", "fps=24,format=yuv420p", "-af", "apad",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-c:a", "aac", "-ar", "44100", "-ac", "2", "-t", f"{dur:.2f}", str(clip)],
        check=True)
    return clip


def main():
    OUT.mkdir(exist_ok=True)
    TMP.mkdir(parents=True, exist_ok=True)
    print(f"Topic: {TOPIC}")
    data = get_script()
    scenes = data["scenes"]
    print(f"Got {len(scenes)} scenes: {data.get('title')}")
    clips = []
    for i, sc in enumerate(scenes, 1):
        print(f"Scene {i}/{len(scenes)}: {sc['label']}")
        audio = make_voice(sc["narration"], str(TMP / f"s{i:02d}"))
        clips.append(build_scene_clip(i, sc, Path(audio)))
    lst = TMP / "all.txt"
    lst.write_text("\n".join(f"file '{c.resolve()}'" for c in clips))
    final = OUT / "final.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", str(lst), "-c", "copy", str(final)], check=True)
    info = [f"TITLE: {data.get('title', TOPIC)}", "",
            f"DESCRIPTION: {data.get('description', '')}", "", "SCRIPT:"]
    info += [f"{i}. {s['narration']}" for i, s in enumerate(scenes, 1)]
    (OUT / "info.txt").write_text("\n".join(info))
    print(f"Done: {final} ({duration(final):.0f} seconds)")


if __name__ == "__main__":
    main()
