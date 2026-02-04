import os
import json
import base64
import requests
import asyncio
import re
from moviepy import VideoFileClip, AudioFileClip, concatenate_videoclips, vfx
from dotenv import load_dotenv

load_dotenv()
SARVAM_API_KEY = os.getenv("SARVAM_API_KEY")

def parse_time(time_str):
    h, m, s = time_str.split(':')
    return int(h) * 3600 + int(m) * 60 + float(s)

def generate_audio_sync(text, filename):
    """Blocking function to generate audio"""
    if not text or not text.strip(): return False
    
    url = "https://api.sarvam.ai/text-to-speech"
    payload = {
        "text": text,
        "target_language_code": "hi-IN",
        "speaker": "shubh",
        "model": "bulbul:v3-beta"
    }
    headers = {"api-subscription-key": SARVAM_API_KEY, "Content-Type": "application/json"}
    
    try:
        response = requests.post(url, json=payload, headers=headers)
        if response.status_code == 200:
            data = response.json()
            with open(filename, "wb") as f:
                f.write(base64.b64decode(data["audios"][0]))
            return True
        else:
            print(f"Sarvam API Error: {response.text}")
    except Exception as e:
        print(f"Audio Connection Error: {e}")
    return False

def load_and_heal_json(file_path):
    """
    Attempts to load JSON. If it fails, it tries to fix:
    1. Invisible newlines inside strings.
    2. Unescaped quotes inside explanation_text.
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        raw_content = f.read()

    # Attempt 1: Direct Load (Best Case)
    try:
        return json.loads(raw_content)
    except json.JSONDecodeError:
        print("⚠️ JSON is broken. Attempting Auto-Repair...")

    # Attempt 2: Fix Newlines (The "Invalid Control Character" Error)
    # This removes line breaks that are NOT part of the structure
    clean_content = re.sub(r'[\x00-\x1f\x7f]', ' ', raw_content)
    
    try:
        return json.loads(clean_content)
    except json.JSONDecodeError:
        print("⚠️ Still broken. Attempting deep repair on Quotes...")

    # Attempt 3: Fix Unescaped Quotes in "explanation_text"
    # This logic looks for the pattern: "explanation_text": " ... "
    # and escapes any quotes inside the value.
    
    def fix_quotes(match):
        # The regex captures:
        # Group 1: "explanation_text": "
        # Group 2: The actual text content
        # Group 3: The closing quote "
        prefix = match.group(1)
        content = match.group(2)
        suffix = match.group(3)
        
        # Escape all double quotes inside the content
        fixed_content = content.replace('"', '\\"')
        return f'{prefix}{fixed_content}{suffix}'

    # Regex explanation:
    # Look for "explanation_text" followed by optional space and a quote.
    # Capture everything until the LAST quote before the next key or end of object.
    # We assume the next key starts with a comma or closing brace.
    pattern = r'("explanation_text"\s*:\s*")(.*?)("\s*[,}])'
    
    # We use re.DOTALL so (.) matches newlines if they exist
    healed_content = re.sub(pattern, fix_quotes, raw_content, flags=re.DOTALL)
    
    # Also apply the newline fix again to the healed content
    healed_content = re.sub(r'[\x00-\x1f\x7f]', ' ', healed_content)

    try:
        return json.loads(healed_content)
    except json.JSONDecodeError as e:
        # If all repairs fail, we must stop.
        raise ValueError(f"❌ Could not auto-fix map.json. \nError details: {e}")

async def process_video_task(video_path, map_path, output_path, status_callback):
    await status_callback("📂 Loading Map & Video...")

    if not os.path.exists(map_path):
        raise FileNotFoundError("map.json not found!")

    # --- USE THE AUTO-HEAL LOADER ---
    try:
        segments = load_and_heal_json(map_path)
        # Ensure it's a list (handle case where user didn't put [] brackets)
        if isinstance(segments, dict):
            segments = [segments]
    except Exception as e:
        raise Exception(f"JSON Critical Error: {str(e)}")
    # ---------------------------------

    temp_dir = "temp_audio"
    os.makedirs(temp_dir, exist_ok=True)

    def _blocking_edit():
        try:
            original_video = VideoFileClip(video_path)
            processed_clips = []
            
            total_segs = len(segments)

            for i, seg in enumerate(segments):
                print(f"Processing Segment {i+1}/{total_segs}")
                
                audio_path = f"{temp_dir}/audio_{seg.get('id', i)}.wav"
                text = seg.get('explanation_text', '')
                
                # Generate Audio
                if not os.path.exists(audio_path):
                    success = generate_audio_sync(text, audio_path)
                    if not success: continue
                
                if not os.path.exists(audio_path): continue

                try:
                    audio_clip = AudioFileClip(audio_path)
                    
                    # Handle missing start/end times gracefully
                    if 'start_time' not in seg or 'end_time' not in seg:
                        print(f"Skipping seg {i}: Missing timestamps")
                        continue

                    start_t = parse_time(seg['start_time'])
                    end_t = parse_time(seg['end_time'])
                    
                    if start_t >= end_t: continue

                    video_chunk = original_video.subclipped(start_t, end_t)
                    
                    if video_chunk.duration <= 0.1: continue 

                    ratio = audio_clip.duration / video_chunk.duration
                    final_chunk = None

                    if ratio > 1.0:
                        if ratio > 1.5:
                            loop_count = int(ratio) + 1
                            video_chunk = video_chunk.with_effects([vfx.Loop(n=loop_count)])
                            final_chunk = video_chunk.subclipped(0, audio_clip.duration)
                        else:
                            final_chunk = video_chunk.with_effects([vfx.MultiplySpeed(1/ratio)])
                    else:
                        final_chunk = video_chunk.subclipped(0, audio_clip.duration)

                    final_chunk = final_chunk.with_audio(audio_clip)
                    processed_clips.append(final_chunk)
                except Exception as clip_error:
                    print(f"⚠️ Error editing clip {i}: {clip_error}")
                    continue
            
            if processed_clips:
                final_video = concatenate_videoclips(processed_clips, method="compose")
                final_video.write_videofile(
                    output_path, 
                    codec="libx264", 
                    audio_codec="aac", 
                    fps=24, 
                    preset='ultrafast',
                    threads=4,
                    logger=None
                )
                original_video.close()
                return True
            
            original_video.close()
            return False
            
        except Exception as e:
            print(f"Processing Error: {e}")
            return False

    await status_callback("🎙️ Generating Audio & Editing Scenes... (This takes time)")
    success = await asyncio.to_thread(_blocking_edit)
    
    if not success:
        raise Exception("Failed to create video clips.")
    
    return output_path
