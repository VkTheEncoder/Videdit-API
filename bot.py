import os
import time
import asyncio
import aiohttp
import aiofiles
import shutil          # Needed for auto-install
import tarfile         # Needed for auto-install (This was missing)
import urllib.request  # Needed for auto-install
import stat            # Needed for auto-install
import re  # Added for Aria2 regex
from pyrogram import Client, filters
from dotenv import load_dotenv
from processor import process_video_task
from utils import progress_bar

load_dotenv()

app = Client(
    "video_editor_bot",
    api_id=int(os.getenv("API_ID")),
    api_hash=os.getenv("API_HASH"),
    bot_token=os.getenv("BOT_TOKEN"),
    workers=4
)

# --- ARIA2 AUTO-INSTALLER ---
def install_aria2_if_missing():
    aria_path = "aria2c"
    if os.path.exists(aria_path):
        print("✅ Aria2 is already installed.")
        return

    print("⬇️ Aria2 missing. Downloading static build...")
    ARIA_URL = "https://github.com/asdo92/aria2-static-builds/releases/download/v1.37.0/aria2-1.37.0-linux-gnu-64bit-build1.tar.bz2"
    TAR_FILENAME = "aria2_download.tar.bz2"
    EXTRACT_FOLDER = "aria2_extracted"

    try:
        # Download
        urllib.request.urlretrieve(ARIA_URL, TAR_FILENAME)
        
        # Extract
        with tarfile.open(TAR_FILENAME, "r:bz2") as tar:
            tar.extractall(EXTRACT_FOLDER)

        # Find binary
        binary_found = False
        for root, dirs, files in os.walk(EXTRACT_FOLDER):
            if "aria2c" in files:
                binary_path = os.path.join(root, "aria2c")
                shutil.move(binary_path, aria_path)
                binary_found = True
                break

        if binary_found:
            # Make executable (chmod +x)
            st = os.stat(aria_path)
            os.chmod(aria_path, st.st_mode | stat.S_IEXEC)
            print("✅ Aria2 installed successfully!")
        else:
            print("❌ Failed to find aria2c binary in download.")

    except Exception as e:
        print(f"❌ Auto-install failed: {e}")
    finally:
        # Cleanup
        if os.path.exists(TAR_FILENAME): os.remove(TAR_FILENAME)
        if os.path.exists(EXTRACT_FOLDER): shutil.rmtree(EXTRACT_FOLDER, ignore_errors=True)

# Run the check immediately
install_aria2_if_missing()

DOWNLOAD_DIR = "downloads"
OUTPUT_DIR = "outputs"
TEMP_AUDIO = "temp_audio"

for d in [DOWNLOAD_DIR, OUTPUT_DIR, TEMP_AUDIO]:
    os.makedirs(d, exist_ok=True)

# --- GLOBAL QUEUE SYSTEM ---
task_queue = asyncio.Queue()
is_processing = False
current_task_info = {} 

user_sessions = {}
STATE_WAIT_JSON = 1
STATE_WAIT_VIDEO = 2
STATE_WAIT_NAME = 3
STATE_QUEUED = 4

# --- HELPER: Aria2 Downloader ---
# Add this helper function ABOVE download_from_link
async def download_fallback_slow(url, dest_path, status_msg, shared_state):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status != 200:
                    print(f"❌ Fallback Download Failed: HTTP {response.status}")
                    return False
                
                total = int(response.headers.get('content-length', 0))
                downloaded = 0
                start = time.time()
                
                async with aiofiles.open(dest_path, mode='wb') as f:
                    async for chunk in response.content.iter_chunked(1024 * 1024):
                        if shared_state.get('stop_signal', False): return False
                        await f.write(chunk)
                        downloaded += len(chunk)
                        
                        # Update progress bar
                        if total > 0 and int(time.time()) % 4 == 0:
                            await progress_bar(downloaded, total, "⬇️ **Downloading (Slow Mode)...**", start, status_msg)
        return True
    except Exception as e:
        print(f"Fallback Error: {e}")
        return False

# Replace your existing download_from_link with this:
async def download_from_link(url, dest_path, status_msg, shared_state):
    """
    Tries High-Speed Aria2 first. If it fails, falls back to standard Python download.
    """
    # 1. SETUP
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    if os.path.exists(dest_path): os.remove(dest_path)

    # Locate Aria2
    aria2_path = os.path.abspath("aria2c")
    if not os.path.exists(aria2_path): aria2_path = "aria2c"

    # 2. TRY ARIA2 (FAST)
    print(f"🚀 Trying Aria2 download for: {url}")
    command = [
        aria2_path, url,
        "-o", os.path.basename(dest_path),
        "-d", os.path.dirname(dest_path),
        "-x", "16", "-s", "16", "-k", "1M",
        "--user-agent", "Mozilla/5.0",
        "--check-certificate=false", # Fix SSL errors
        "--summary-interval", "1"
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        progress_pattern = re.compile(r"\((\d+)%\)")
        start_time = time.time()
        last_update_time = 0

        while True:
            if shared_state.get('stop_signal', False):
                try: process.kill()
                except: pass
                return False

            line = await process.stdout.readline()
            if not line: break
            
            decoded_line = line.decode().strip()
            
            # Update Progress
            match = progress_pattern.search(decoded_line)
            if match:
                percent = int(match.group(1))
                now = time.time()
                if now - last_update_time > 4:
                    last_update_time = now
                    filled = int(percent / 10)
                    bar = f"[{'█' * filled}{'░' * (10 - filled)}] {percent}%"
                    elapsed = int(now - start_time)
                    try:
                        await status_msg.edit(
                            f"🚀 **Downloading (High Speed)...**\n{bar}\n**Time:** {elapsed}s"
                        )
                    except: pass

        await process.wait()
        
        # 3. CHECK SUCCESS
        if process.returncode == 0 and os.path.exists(dest_path):
            return True
        else:
            # Capture the error message
            stderr_data = await process.stderr.read()
            print(f"⚠️ Aria2 Failed (Code {process.returncode}). Error: {stderr_data.decode().strip()}")
            
            # 4. ACTIVATE FALLBACK (SLOW)
            await status_msg.edit(f"⚠️ **High Speed failed. Switching to Standard Download...**")
            return await download_fallback_slow(url, dest_path, status_msg, shared_state)

    except Exception as e:
        print(f"Aria2 Exception: {e}")
        return await download_fallback_slow(url, dest_path, status_msg, shared_state)
        
async def clean_up(paths):
    for p in paths:
        if p and os.path.exists(p):
            try: os.remove(p)
            except: pass

# --- WORKER LOOP ---
async def queue_worker():
    global is_processing, current_task_info
    print("👷 Worker started...")
    
    while True:
        task_data = await task_queue.get()
        is_processing = True
        
        user_id = task_data['user_id']
        chat_id = task_data['chat_id']
        status_msg = task_data['status_msg']
        task_id = task_data['task_id']
        
        shared_state = {'stop_signal': False, 'text': '', 'percent': 0, 'current': 0, 'total': 0}
        current_task_info = {'user_id': user_id, 'shared_state': shared_state, 'status_msg': status_msg}

        try:
            await status_msg.edit(f"🎬 **Starting Task!**\nQueue Position: 0 (Running)")
            
            # 1. Download
            input_video_path = os.path.join(DOWNLOAD_DIR, f"{user_id}_input.mp4")
            
            if task_data["video_source"] == "link":
                await status_msg.edit("⬇️ **Starting Download...**")
                success = await download_from_link(task_data["video_link"], input_video_path, status_msg, shared_state)
                if not success: raise Exception("Download failed.")
            
            elif task_data["video_source"] == "telegram":
                await status_msg.edit("⬇️ **Downloading from Telegram...**")
                await task_data["video_message"].download(
                    file_name=input_video_path,
                    progress=progress_bar,
                    progress_args=("⬇️ **Downloading...**", time.time(), status_msg)
                )

            if shared_state['stop_signal']: raise Exception("⛔ Task Stopped")

            # 2. Process
            output_video_path = os.path.join(OUTPUT_DIR, f"{task_data['filename']}.mp4")
            
            async def update_status_text(txt):
                if not shared_state['stop_signal']:
                    try: await status_msg.edit(f"⚙️ **Processing...**\n\n{txt}")
                    except: pass

            await process_video_task(
                input_video_path, 
                task_data['json_path'], 
                output_video_path, 
                update_status_text,
                shared_state,
                task_id
            )

            # 3. Generate Thumbnail & Upload
            thumb_path = os.path.join(OUTPUT_DIR, f"{task_data['filename']}.jpg")
            
            # Generate thumbnail using FFmpeg (takes a snapshot at 1 second)
            try:
                thumb_proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-i", output_video_path, "-ss", "00:00:05", "-vframes", "1", thumb_path, "-y",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                await thumb_proc.wait()
            except Exception as e:
                print(f"Thumbnail generation failed: {e}")

            await status_msg.edit("⬆️ **Uploading as Document...**")
            
            # Send as Document with Thumbnail
            await app.send_document(
                chat_id=chat_id,
                document=output_video_path,
                thumb=thumb_path if os.path.exists(thumb_path) else None,
                caption=f"✅ **{task_data['filename']}** is ready!",
                progress=progress_bar,
                progress_args=("⬆️ **Uploading...**", time.time(), status_msg)
            )
            await status_msg.delete()
            
            # Cleanup (Added thumb_path to cleanup list)
            await clean_up([task_data['json_path'], input_video_path, output_video_path, thumb_path])

        except Exception as e:
            msg = str(e)
            if "Stopped" in msg:
                await status_msg.edit("🛑 **Process Stopped by Admin.**")
            else:
                await status_msg.edit(f"❌ **Error:** {msg}")
                print(f"Task Error: {msg}")
            
            try:
                paths = [task_data.get('json_path'), os.path.join(DOWNLOAD_DIR, f"{user_id}_input.mp4")]
                await clean_up(paths)
            except: pass
            
        finally:
            is_processing = False
            current_task_info = {}
            task_queue.task_done()
            user_sessions.pop(user_id, None)

# --- COMMANDS ---

@app.on_message(filters.command("stopall"))
async def stop_all(client, message):
    global is_processing, current_task_info
    
    q_size = task_queue.qsize()
    while not task_queue.empty():
        try: task_queue.get_nowait()
        except: break
    
    msg = f"🛑 **Stopping Everything...**\nDeleted {q_size} queued tasks."
    
    if is_processing and current_task_info:
        current_task_info['shared_state']['stop_signal'] = True
        msg += "\nSent STOP signal to current process."
    else:
        msg += "\nNo active process found."
        
    await message.reply_text(msg)

@app.on_message(filters.command("start"))
async def start(client, message):
    user_sessions[message.from_user.id] = {"state": STATE_WAIT_JSON, "data": {}}
    await message.reply_text("👋 **Welcome!**\nStep 1: Send `map.json` file.")

@app.on_message(filters.document)
async def handle_doc(client, message):
    uid = message.from_user.id
    sess = user_sessions.get(uid)
    if not sess: return

    if sess["state"] == STATE_WAIT_JSON:
        if not message.document.file_name.endswith(".json"):
            await message.reply_text("❌ Send a valid .json file.")
            return
        
        status = await message.reply_text("📥 Saving Map...")
        
        # FIX: Add timestamp to make filename UNIQUE for every single task
        # This prevents "Episode 2" from overwriting "Episode 1"
        timestamp = int(time.time())
        task_id = f"{uid}_{timestamp}"
        unique_filename = f"{uid}_{timestamp}_map.json"
        path = os.path.join(DOWNLOAD_DIR, unique_filename)
        
        await message.download(file_name=path)
        
        sess["data"]["json_path"] = path
        sess["data"]["task_id"] = task_id
        sess["state"] = STATE_WAIT_VIDEO
        await status.edit("✅ **Map Saved!**\nStep 2: Send Video or Link.")

@app.on_message(filters.video | filters.document | filters.text)
async def handle_input(client, message):
    uid = message.from_user.id
    sess = user_sessions.get(uid)
    if not sess: return

    if sess["state"] == STATE_WAIT_VIDEO:
        # Check for Video OR Document (Video File)
        if message.video or (message.document and message.document.mime_type and message.document.mime_type.startswith("video/")):
            sess["data"]["video_source"] = "telegram"
            sess["data"]["video_message"] = message
            sess["state"] = STATE_WAIT_NAME
            await message.reply_text("✅ Video Received!\nStep 3: Send Output Name.")
        
        elif message.text and message.text.startswith("http"):
            sess["data"]["video_source"] = "link"
            sess["data"]["video_link"] = message.text
            sess["state"] = STATE_WAIT_NAME
            await message.reply_text("✅ Link Received!\nStep 3: Send Output Name.")
        
        else:
            await message.reply_text("❌ Invalid. Send a Video file or a Link.")

    elif sess["state"] == STATE_WAIT_NAME:
        # (This part stays exactly the same as before)
        name = message.text.strip().replace(" ", "_")
        sess["data"]["filename"] = name
        sess["data"]["user_id"] = uid
        sess["data"]["chat_id"] = message.chat.id
        
        status_msg = await message.reply_text("⏳ **Adding to Queue...**")
        sess["data"]["status_msg"] = status_msg
        
        await task_queue.put(sess["data"])
        
        q_pos = task_queue.qsize()
        await status_msg.edit(f"✅ **Added to Queue!**\nPosition: #{q_pos}\nWaiting for worker...")
        
        sess["state"] = STATE_QUEUED

if __name__ == "__main__":
    print("🤖 Bot Started...")
    loop = asyncio.get_event_loop()
    loop.create_task(queue_worker())
    app.run()
