import os
import json
import base64
import requests
import asyncio
import re
import gc
import time
from proglog import ProgressBarLogger
from moviepy import VideoFileClip, AudioFileClip, concatenate_videoclips, vfx
from dotenv import load_dotenv

load_dotenv()
SARVAM_API_KEY = os.getenv("SARVAM_API_KEY")

# --- CUSTOM LOGGER ---
class TelegramLogger(ProgressBarLogger):
    def __init__(self, shared_state, batch_index, total_batches):
        super().__init__()
        self.shared_state = shared_state
        self.batch_info = f"Batch {batch_index}/{total_batches}"

    def callback(self, **changes):
        if self.shared_state.get('stop_signal', False): raise Exception("⛔ Task Stopped")

        for (parameter, value) in changes.items():
            if parameter == 'bars' and 't' in value:
                total = value['t']['total']
                index = value['t']['index']
                if total > 0:
                    percent = int((index / total) * 100)
                    # Update state immediately
                    self.shared_state['text'] = f"🎞️ **Rendering {self.batch_info}...**"
                    self.shared_state['percent'] = percent
                    self.shared_state['current'] = index
                    self.shared_state['total'] = total

# --- UTILS ---
def make_progress_bar(current, total):
    if total == 0: return "[░░░░░░░░░░] 0%"
    percentage = min(current * 100 / total, 100)
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
        content = re.sub(
            r'("explanation_text"\s*:\s*")(.*?)("\s*[,}])', 
            lambda m: m.group(1) + m.group(2).replace('"', '\\"') + m.group(3), 
            content, flags=re.DOTALL
        )
        try: return json.loads(content)
        except: raise ValueError("❌ Critical JSON Error: Could not auto-fix file.")

# --- BATCH RENDERER ---
def render_batch(video_path, segments, batch_index, total_batches, temp_dir, shared_state):
    if shared_state.get('stop_signal', False): return None
    
    # FIX: Force an immediate update so the user sees "Preparing..."
    shared_state['text'] = f"🔨 **Preparing Batch {batch_index}/{total_batches}...**"
    shared_state['percent'] = 0
    
    processed_clips = []
    original_video = VideoFileClip(video_path)
    
    try:
        for i, seg in enumerate(segments):
            if shared_state.get('stop_signal', False): raise Exception("⛔ Task Stopped")

            audio_path = f"{temp_dir}/audio_{seg.get('id', f'b{batch_index}_{i}')}.wav"
            if not os.path.exists(audio_path): continue
            
            try:
                audio_clip = AudioFileClip(audio_path)
                start_t = parse_time(seg.get('start_time', '0:00'))
                end_t = parse_time(seg.get('end_time', '0:00'))
                
                if start_t >= end_t: audio_clip.close(); continue

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
        tg_logger = TelegramLogger(shared_state, batch_index, total_batches)

        final_video = concatenate_videoclips(processed_clips, method="compose")
        
        # Write file with Logger
        final_video.write_videofile(
            batch_output, 
            codec="libx264", 
            audio_codec="aac", 
            fps=24, 
            preset='ultrafast', 
            threads=4, 
            logger=tg_logger 
        )
        
        final_video.close()
        for c in processed_clips: c.close()
        original_video.close()
        gc.collect()
        
        return batch_output

    except Exception as e:
        print(f"Batch Render Stopped: {e}")
        original_video.close()
        for c in processed_clips: c.close()
        return None

# --- MAIN TASK ---
async def process_video_task(video_path, map_path, output_path, status_callback, shared_state):
    await status_callback("📂 **Loading Resources...**")
    
    segments = load_and_heal_json(map_path)
    if isinstance(segments, dict): segments = [segments]
    
    temp_dir = "temp_audio"
    os.makedirs(temp_dir, exist_ok=True)
    total = len(segments)
    
    # 1. AUDIO GENERATION
    for i, seg in enumerate(segments):
        if shared_state.get('stop_signal', False): raise Exception("⛔ Task Stopped")
        
        if i % 5 == 0:
            await status_callback(f"🎙️ **Generating Audio...**\n{make_progress_bar(i, total)}")
        audio_path = f"{temp_dir}/audio_{seg.get('id', i)}.wav"
        if not os.path.exists(audio_path):
            await asyncio.to_thread(generate_audio_sync, seg.get('explanation_text', ''), audio_path)

    # 2. BATCH RENDERING
    BATCH_SIZE = 10
    total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    batch_files = []

    render_running = True

    async def progress_monitor():
        last_text = ""
        while render_running:
            if shared_state.get('stop_signal', False): break 
            
            await asyncio.sleep(3) # Update every 3s
            
            # Construct Status Message
            percent = shared_state.get('percent', 0)
            text_header = shared_state.get('text', "⏳ Starting Render...")
            
            bar = make_progress_bar(percent, 100) # Use percent directly
            new_text = f"{text_header}\n{bar}"
            
            if new_text != last_text:
                try:
                    await status_callback(new_text)
                    last_text = new_text
                except: pass

    monitor_task = asyncio.create_task(progress_monitor())

    try:
        for i in range(0, total, BATCH_SIZE):
            if shared_state.get('stop_signal', False): raise Exception("⛔ Task Stopped")
            
            batch_segments = segments[i : i + BATCH_SIZE]
            batch_idx = i // BATCH_SIZE + 1
            
            # Pass shared_state to the thread
            batch_file = await asyncio.to_thread(
                render_batch, 
                video_path, 
                batch_segments, 
                batch_idx, 
                total_batches, 
                temp_dir, 
                shared_state
            )
            
            if batch_file: batch_files.append(batch_file)
            else:
                if shared_state.get('stop_signal', False): raise Exception("⛔ Task Stopped")
    finally:
        render_running = False
        monitor_task.cancel()

    if not batch_files: raise Exception("No video segments generated.")

    # 3. MERGE
    await status_callback("🚀 **Final Stitching...**")
    list_file_path = f"{temp_dir}/inputs.txt"
    with open(list_file_path, "w") as f:
        for path in batch_files: f.write(f"file '{path}'\n")

    command = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file_path, "-c", "copy", output_path]
    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    await process.communicate()

    for f in batch_files:
        if os.path.exists(f): os.remove(f)
    if os.path.exists(list_file_path): os.remove(list_file_path)

    return output_path
