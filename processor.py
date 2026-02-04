import os
import json
import base64
import requests
import asyncio
from moviepy import VideoFileClip, AudioFileClip, concatenate_videoclips, vfx
from dotenv import load_dotenv

load_dotenv()
SARVAM_API_KEY = os.getenv("SARVAM_API_KEY")

def parse_time(time_str):
    h, m, s = time_str.split(':')
    return int(h) * 3600 + int(m) * 60 + float(s)

def generate_audio_sync(text, filename):
    """Blocking function to generate audio"""
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
    except Exception as e:
        print(f"Audio Error: {e}")
    return False

async def process_video_task(video_path, map_path, output_path, status_callback):
    """
    Main processing logic. 
    status_callback is a generic async function to update UI text.
    """
    await status_callback("📂 Loading Map & Video...")

    if not os.path.exists(map_path):
        raise FileNotFoundError("map.json not found!")

    with open(map_path, 'r', encoding='utf-8') as f:
        segments = json.load(f)

    # Ensure temp folder
    temp_dir = "temp_audio"
    os.makedirs(temp_dir, exist_ok=True)

    final_clips = []
    
    # We must run blocking MoviePy/Requests in a thread to not freeze the bot
    def _blocking_edit():
        try:
            original_video = VideoFileClip(video_path)
            processed_clips = []
            
            total_segs = len(segments)

            for i, seg in enumerate(segments):
                # Update status via a wrapper could be tricky from thread, 
                # so we just print here and assume the main loop handles major updates.
                print(f"Processing Segment {i+1}/{total_segs}")
                
                audio_path = f"{temp_dir}/audio_{seg['id']}.wav"
                
                # Generate Audio
                if not os.path.exists(audio_path):
                    generate_audio_sync(seg['explanation_text'], audio_path)
                
                if not os.path.exists(audio_path): 
                    continue

                # Edit Video
                audio_clip = AudioFileClip(audio_path)
                start_t = parse_time(seg['start_time'])
                end_t = parse_time(seg['end_time'])
                
                if start_t >= end_t: continue

                video_chunk = original_video.subclipped(start_t, end_t)
                
                if video_chunk.duration == 0: continue

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
            
            if processed_clips:
                final_video = concatenate_videoclips(processed_clips, method="compose")
                final_video.write_videofile(
                    output_path, 
                    codec="libx264", 
                    audio_codec="aac", 
                    fps=24, 
                    preset='ultrafast',
                    threads=4,
                    logger=None # Disable stdout spam
                )
                original_video.close()
                return True
            
            original_video.close()
            return False
            
        except Exception as e:
            print(f"Processing Error: {e}")
            return False

    # Run the heavy blocking function in a separate thread
    await status_callback("🎙️ Generating Audio & Editing Scenes... (This takes time)")
    success = await asyncio.to_thread(_blocking_edit)
    
    if not success:
        raise Exception("Failed to create video clips.")
    
    return output_path
