import os
import json
import base64
import requests
import asyncio
import re
import gc
import subprocess
from moviepy import VideoFileClip, AudioFileClip, concatenate_videoclips, vfx
from dotenv import load_dotenv

load_dotenv()
SARVAM_API_KEY = os.getenv("SARVAM_API_KEY")

def make_progress_bar(current, total):
    percentage = current * 100 / total
    filled = int(percentage / 10)
    return f"[{'█' * filled}{'░' * (10 - filled)}] {int(percentage)}%"

def parse_time(time_str):
    try:
        parts = time_str.split(':')
        if len(parts) == 3: return int(parts[0])*3600 + int(parts[1])*60 + float(parts[2])
        if len(parts) == 2: return int(parts[0])*60 + float(parts[1])
    except: return 0
    return 0

def generate_audio_sync(text, filename):
    if not text or not text.strip(): return False
    try:
        response = requests.post(
            "https://api.sarvam.ai/text-to-speech",
            json={"text": text, "target_language_code": "hi-IN", "speaker": "shubh", "model": "bulbul:v3-beta"},
            headers={"api-subscription-key": SARVAM_API_KEY}
        )
        if response.status_code == 200:
            with open(filename, "wb") as f:
                f.write(base64.b64decode(response.json()["audios"][0]))
            return True
    except Exception as e:
        print(f"Audio Error: {e}")
    return False

def load_and_heal_json(file_path):
    with open(file_path, 'r', encoding='utf-8') as f: content = f.read()
    try: return json.loads(content)
    except:
        content = re.sub(r'[\x00-\x1f\x7f]', ' ', content)
        content = re.sub(r'("explanation_text"\s*:\s*")(.*?)("\s*[,}])', 
                         lambda m: f'{m.group(1)}{m.group(2).replace("\"", "\\\"")}{m.group(3)}', 
                         content, flags=re.DOTALL)
        try: return json.loads(content)
        except: raise ValueError("❌ Critical JSON Error: Could not auto-fix file.")

def render_batch(video_path, segments, batch_index, temp_dir):
    """
    Renders a batch and returns the filename.
    """
    processed_clips = []
    original_video = VideoFileClip(video_path)
    
    try:
        for i, seg in enumerate(segments):
            # Unique ID for temp audio to avoid conflicts
            audio_path = f"{temp_dir}/audio_{seg.get('id', f'b{batch_index}_{i}')}.wav"
            
            if not os.path.exists(audio_path): continue
            
            try:
                audio_clip = AudioFileClip(audio_path)
                start_t = parse_time(seg.get('start_time', '0:00'))
                end_t = parse_time(seg.get('end_time', '0:00'))
                
                if start_t >= end_t: 
                    audio_clip.close(); continue

                video_chunk = original_video.subclipped(start_t, end_t)
                
                if video_chunk.duration < 0.1:
                    video_chunk.close(); audio_clip.close(); continue

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
                
            except Exception as e:
                print(f"Clip Error: {e}")

        if not processed_clips:
            original_video.close()
            return None

        batch_output = os.path.abspath(f"{temp_dir}/batch_{batch_index}.mp4")
        
        # Important: We must use the same settings as final output would expect
        final_video = concatenate_videoclips(processed_clips, method="compose")
        final_video.write_videofile(
            batch_output, codec="libx264", audio_codec="aac", fps=24, preset='ultrafast', threads=4, logger=None
        )
        
        final_video.close()
        for c in processed_clips: c.close()
        original_video.close()
        gc.collect()
        
        return batch_output

    except Exception as e:
        print(f"Batch Render Error: {e}")
        original_video.close()
        return None

async def process_video_task(video_path, map_path, output_path, status_callback):
    await status_callback("📂 **Loading Resources...**")
    
    segments = load_and_heal_json(map_path)
    if isinstance(segments, dict): segments = [segments]
    
    temp_dir = "temp_audio"
    os.makedirs(temp_dir, exist_ok=True)
    
    total = len(segments)
    
    # --- PHASE 1: AUDIO GENERATION ---
    for i, seg in enumerate(segments):
        if i % 5 == 0:
            await status_callback(f"🎙️ **Generating Audio...**\n{make_progress_bar(i, total)}")
        
        audio_path = f"{temp_dir}/audio_{seg.get('id', i)}.wav"
        if not os.path.exists(audio_path):
            await asyncio.to_thread(generate_audio_sync, seg.get('explanation_text', ''), audio_path)

    # --- PHASE 2: BATCH VIDEO RENDERING ---
    await status_callback(f"🎞️ **Rendering Batches...**\n(Splitting work to save memory)")
    
    BATCH_SIZE = 10
    batch_files = []
    total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    
    for i in range(0, total, BATCH_SIZE):
        batch_segments = segments[i : i + BATCH_SIZE]
        batch_idx = i // BATCH_SIZE + 1
        
        await status_callback(f"🎞️ **Rendering Batch {batch_idx}/{total_batches}...**")
        batch_file = await asyncio.to_thread(render_batch, video_path, batch_segments, batch_idx, temp_dir)
        
        if batch_file: batch_files.append(batch_file)

    if not batch_files:
        raise Exception("No video segments were generated.")

    # --- PHASE 3: INSTANT MERGE (FFMPEG) ---
    await status_callback("🚀 **Instant Stitching...** (Final Step)")
    
    # Create FFmpeg List File
    list_file_path = f"{temp_dir}/inputs.txt"
    with open(list_file_path, "w") as f:
        for path in batch_files:
            # Escape paths for FFmpeg
            f.write(f"file '{path}'\n")

    # Run FFmpeg Command (Copy Stream = Zero Re-encoding)
    # This takes 2 seconds even for large videos
    command = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", 
        "-i", list_file_path, 
        "-c", "copy", 
        output_path
    ]
    
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    await process.communicate()

    # Cleanup Batches
    for f in batch_files:
        if os.path.exists(f): os.remove(f)
    if os.path.exists(list_file_path): os.remove(list_file_path)

    return output_path
