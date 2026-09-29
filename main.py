import os
import json
import asyncio
import subprocess
from PIL import Image, ImageDraw, ImageFont
import edge_tts
from google import genai

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TOPIC = os.getenv("VIDEO_TOPIC", "Government of India Act 1935: Federalism & Autonomy")
VOICE = os.getenv("VOICE_NAME", "en-IN-NeerjaNeural")

os.makedirs("output", exist_ok=True)
os.makedirs("temp", exist_ok=True)

# 1. Generate Script
def get_script(topic):
    prompt = f"""
    Create a 3-slide educational script about '{topic}'.
    Return strictly JSON matching this structure:
    {{
      "slides": [
        {{
          "title": "Title",
          "bullets": ["Point 1", "Point 2", "Point 3"],
          "narration": "Detailed explanation sentence for narration."
        }}
      ]
    }}
    """
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
        )
        clean_text = response.text.strip().removeprefix("```json").removesuffix("```").strip()
        return json.loads(clean_text)
    except Exception as e:
        print(f"Fallback to default due to: {e}")
        return {
            "slides": [
                {
                    "title": "Government of India Act 1935",
                    "bullets": ["Passed by British Parliament", "All-India Federation proposed", "Abolished Provincial Dyarchy"],
                    "narration": "The Government of India Act 1935 proposed an All-India Federation and granted provincial autonomy."
                }
            ]
        }

# 2. Render Slide Image
def create_slide_image(slide_data, index):
    img = Image.new("RGB", (1920, 1080), color="#0F172A")
    draw = ImageDraw.Draw(img)
    
    # Text overlays
    draw.text((120, 120), slide_data["title"], fill="#38BDF8")
    y = 300
    for bullet in slide_data["bullets"]:
        draw.text((160, y), f"•  {bullet}", fill="#F8FAFC")
        y += 100
        
    img_path = f"temp/slide_{index}.png"
    img.save(img_path)
    return img_path

# 3. Audio & Video Assembly
async def render_pipeline():
    script_data = get_script(TOPIC)
    segment_files = []
    
    for idx, slide in enumerate(script_data["slides"]):
        # Audio
        audio_path = f"temp/audio_{idx}.mp3"
        communicate = edge_tts.Communicate(slide["narration"], VOICE)
        await communicate.save(audio_path)
        
        # Slide image
        img_path = create_slide_image(slide, idx)
        
        # Clip with FFmpeg
        clip_path = f"temp/clip_{idx}.mp4"
        cmd = [
            "ffmpeg", "-y", "-loop", "1", "-i", img_path,
            "-i", audio_path, "-c:v", "libx264", "-tune", "stillimage",
            "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p",
            "-shortest", clip_path
        ]
        subprocess.run(cmd, check=True)
        segment_files.append(clip_path)

    # Concat file
    with open("temp/concat.txt", "w") as f:
        for seg in segment_files:
            f.write(f"file '{os.path.abspath(seg)}'\n")
            
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", "temp/concat.txt", "-c", "copy", "output/final.mp4"
    ], check=True)
    
    with open("output/info.txt", "w") as f:
        f.write(f"Topic: {TOPIC}\nVoice: {VOICE}\nSlides: {len(segment_files)}")

if __name__ == "__main__":
    asyncio.run(render_pipeline())
