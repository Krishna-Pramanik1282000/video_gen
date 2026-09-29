import os
import time
import requests

API_KEY = os.getenv("GEMINI_API_KEY")

MODELS = [
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
]

def query_gemini_rest(prompt: str, max_retries: int = 3) -> str:
    payload = {
        "contents": [{"parts": [{"text": prompt}]}]
    }

    for model in MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={API_KEY}"
        
        for attempt in range(1, max_retries + 1):
            try:
                print(f"Calling {model} (attempt {attempt})...")
                response = requests.post(url, json=payload, timeout=30)

                if response.status_code == 200:
                    data = response.json()
                    return data["candidates"][0]["content"]["parts"][0]["text"]

                # Handle temporary capacity issues (503 / 429)
                if response.status_code in [503, 429]:
                    wait = 2 ** attempt
                    print(f"Server returned {response.status_code}. Backing off for {wait}s...")
                    time.sleep(wait)
                    continue

                # 404 Not Found or 400 Bad Request: move immediately to next model
                print(f"Failed with HTTP {response.status_code}: {response.text}")
                break

            except requests.RequestException as e:
                print(f"Network error on {model}: {e}")
                time.sleep(2)

    raise RuntimeError("Failed to generate content across all models.")
