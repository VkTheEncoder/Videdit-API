import os
import time
import asyncio
import aiohttp
import aiofiles
import re
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

DOWNLOAD_DIR = "downloads"
OUTPUT_DIR = "outputs"
TEMP_AUDIO = "temp_audio"

for d in [DOWNLOAD_DIR, OUTPUT_DIR, TEMP_AUDIO]:
    os.makedirs(d, exist_ok=True)

# --- GLOBAL QUEUE SYSTEM ---
task_queue = asyncio.Queue()
is_processing = False
current_task_info = {} # Stores info about current running task for cancellation

user_sessions = {}
STATE_WAIT_JSON = 1
STATE_WAIT_VIDEO = 2
STATE_WAIT_NAME = 3
STATE_QUEUED = 4

# --- WORKER LOOP ---
async def queue_worker():
    global is_processing, current_task_info
    print("👷 Worker started...")
    
    while True:
        # Wait for a task
        task_data = await task_queue.get()
        is_processing = True
        
        user_id = task_data['user_id']
        chat_id = task_data['chat_id']
        status_msg = task_data['status_msg']
        
        # Setup Stop Signal
        shared_state = {'stop_signal': False, 'text': '', 'percent': 0, 'current': 0, 'total': 0}
        current_task_info = {'user_id': user_id, 'shared_state': shared_state, 'status_msg': status_msg}

        try:
            await status_msg.edit(f"🎬 **Starting Task!**\nQueue Position: 0 (Running)")
            
            # 1. Download Video
            input_video_path = os.path.join(DOWNLOAD_DIR, f"{user_id}_input.mp4")
            if task_data["video_source"] == "link":
                await status_msg.edit("⬇️ **Downloading Video from Link...**")
                if not await download_from_link(task_data["video_link"], input_video_path, status_msg, shared_state):
                     raise Exception("Download failed.")
            elif task_data["video_source"] == "telegram":
                await status_msg.edit("⬇️ **Downloading Video from Telegram...**")
                await task_data["video_message"].download(
                    file_name=input_video_path,
                    progress=progress_bar,
                    progress_args=("⬇️ **Downloading...**", time.time(), status_msg)
                )

            if shared_state['stop_signal']: raise Exception("⛔ Task Stopped")

            # 2. Process Video
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
                shared_state
            )

            # 3. Upload
            await status_msg.edit("⬆️ **Uploading Final Video...**")
            await app.send_video(
                chat_id=chat_id,
                video=output_video_path,
                caption=f"✅ **{task_data['filename']}** is ready!",
                progress=progress_bar,
                progress_args=("⬆️ **Uploading...**", time.time(), status_msg)
            )
            await status_msg.delete()
            
            # Cleanup
            await clean_up([task_data['json_path'], input_video_path, output_video_path])

        except Exception as e:
            if "Stopped" in str(e):
                await status_msg.edit("🛑 **Process Stopped by Admin.**")
            else:
                await status_msg.edit(f"❌ **Error:** {str(e)}")
                print(f"Task Error: {e}")
            
            # Cleanup on error
            try:
                paths = [task_data['json_path'], os.path.join(DOWNLOAD_DIR, f"{user_id}_input.mp4")]
                await clean_up(paths)
            except: pass
            
        finally:
            is_processing = False
            current_task_info = {}
            task_queue.task_done()
            user_sessions.pop(user_id, None)

Here is the complete, corrected code block for download_from_link.

You should replace the entire existing download_from_link function in your bot.py with this code.

Prerequisite: Ensure you have added import re at the very top of your bot.py file along with the other imports.

Python
async def download_from_link(url, dest_path, status_msg, shared_state):
    """
    Downloads using local Aria2c binary (16 connections).
    Updates the Telegram status message with real-time progress.
    """
    try:
        # Ensure directory exists & remove old file
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        if os.path.exists(dest_path): os.remove(dest_path)

        # Get the absolute path of aria2c in the current folder
        # This fixes the "No such file or directory" error
        aria2_path = os.path.abspath("aria2c")

        # Check if it exists before running
        if not os.path.exists(aria2_path):
            print(f"❌ Error: aria2c binary not found at {aria2_path}")
            # Fallback: Try system aria2c if local is missing
            aria2_path = "aria2c" 

        # Command to run local ./aria2c
        command = [
            aria2_path, url,
            "-o", os.path.basename(dest_path),
            "-d", os.path.dirname(dest_path),
            "-x", "16", "-s", "16", "-k", "1M",
            "--user-agent", "Mozilla/5.0",
            "--summary-interval", "1"
        ]

        # Start the process
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        # Regex to find "(45%)" in the logs
        progress_pattern = re.compile(r"\((\d+)%\)")
        start_time = time.time()
        last_update_time = 0

        while True:
            # Check for STOP command from /stopall
            if shared_state.get('stop_signal', False):
                try: process.kill()
                except: pass
                return False

            line = await process.stdout.readline()
            if not line: break
            
            decoded_line = line.decode().strip()
            
            # Find percentage
            match = progress_pattern.search(decoded_line)
            if match:
                percent = int(match.group(1))
                
                # Update UI every 4 seconds to avoid spamming Telegram
                now = time.time()
                if now - last_update_time > 4:
                    last_update_time = now
                    
                    # Create a visual bar [████░░░░░░]
                    filled = int(percent / 10)
                    bar = f"[{'█' * filled}{'░' * (10 - filled)}] {percent}%"
                    elapsed = int(now - start_time)
                    
                    try:
                        await status_msg.edit(
                            f"⬇️ **Downloading (High Speed)...**\n"
                            f"{bar}\n"
                            f"**Time Elapsed:** {elapsed}s"
                        )
                    except: pass # Ignore edit errors

        await process.wait()
        
        # Check if file exists and download finished successfully
        if process.returncode == 0 and os.path.exists(dest_path):
            return True
        else:
            print(f"Aria2 Failed. Return Code: {process.returncode}")
            return False

    except Exception as e:
        print(f"Aria2 Error: {e}")
        return False

async def clean_up(paths):
    for p in paths:
        if p and os.path.exists(p):
            try: os.remove(p)
            except: pass

# --- COMMANDS ---

@app.on_message(filters.command("stopall"))
async def stop_all(client, message):
    global is_processing, current_task_info
    
    # 1. Clear Queue
    q_size = task_queue.qsize()
    while not task_queue.empty():
        try: task_queue.get_nowait()
        except: break
    
    msg = f"🛑 **Stopping Everything...**\nDeleted {q_size} queued tasks."
    
    # 2. Stop Current Task
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
        path = os.path.join(DOWNLOAD_DIR, f"{uid}_map.json")
        await message.download(file_name=path)
        
        sess["data"]["json_path"] = path
        sess["state"] = STATE_WAIT_VIDEO
        await status.edit("✅ **Map Saved!**\nStep 2: Send Video or Link.")

@app.on_message(filters.video | filters.text)
async def handle_input(client, message):
    uid = message.from_user.id
    sess = user_sessions.get(uid)
    if not sess: return

    # HANDLE VIDEO/LINK
    if sess["state"] == STATE_WAIT_VIDEO:
        if message.video:
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
            await message.reply_text("❌ Invalid. Send Video or Link.")

    # HANDLE NAME -> QUEUE
    elif sess["state"] == STATE_WAIT_NAME:
        name = message.text.strip().replace(" ", "_")
        sess["data"]["filename"] = name
        sess["data"]["user_id"] = uid
        sess["data"]["chat_id"] = message.chat.id
        
        status_msg = await message.reply_text("⏳ **Adding to Queue...**")
        sess["data"]["status_msg"] = status_msg
        
        # Add to Queue
        await task_queue.put(sess["data"])
        
        q_pos = task_queue.qsize()
        await status_msg.edit(f"✅ **Added to Queue!**\nPosition: #{q_pos}\nWaiting for worker...")
        
        sess["state"] = STATE_QUEUED

if __name__ == "__main__":
    print("🤖 Bot Started...")
    loop = asyncio.get_event_loop()
    loop.create_task(queue_worker()) # Start Worker
    app.run()
