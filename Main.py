"""
Free stick-figure explainer video maker.
Topic -> script (GitHub Models, free) -> voice (edge-tts, free)
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
SCENES = int(os.environ.get("SCENES", "12"))
VOICE = os.environ.get("VOICE", "en-US-GuyNeural")

W, H = 1280, 720
GROUND = 600
INK = (30, 30, 30)
OUT = Path("output")
TMP = OUT / "tmp"

BGS = {
    "peach": (250, 190, 145),
