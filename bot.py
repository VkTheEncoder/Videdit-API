import os
import time
import asyncio
import aiohttp
import aiofiles
from pyrogram import Client, filters, enums
from dotenv import load_dotenv
from processor import process_video_task
from utils import progress_bar

# Load Env
load_dotenv()

# Setup Client
app = Client(
    "video_editor_bot",
    api_id=int(os.getenv("API_ID")),
    api_hash=os.getenv("API_HASH"),
    bot_token=os.getenv("BOT_TOKEN"),
    workers=4  # Allow handling multiple users at once
)

# Directories
DOWNLOAD_DIR = "downloads"
OUTPUT_DIR = "outputs"
TEMP_AUDIO = "temp_audio"

for d in [DOWNLOAD_DIR, OUTPUT_DIR, TEMP_AUDIO]:
    os.makedirs(d, exist_ok=True)

# User State Storage (In-Memory)
# Structure: { user_id: { "state": "WAIT_JSON", "data": {...} } }
user_sessions = {}

# States
STATE_WAIT_JSON = 1
STATE_WAIT_VIDEO = 2
STATE_WAIT_NAME = 3
STATE_PROCESSING = 4

# --- Helper Functions ---

async def download_from_link(url, dest_path, status_msg):
    """Downloads file from direct URL."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return False
                
                total_size = int(response.headers.get('content-length', 0))
                downloaded = 0
                start_time = time.time()
                
                async with aiofiles.open(dest_path, mode='wb') as f:
                    async for chunk in response.content.iter_chunked(1024 * 1024): # 1MB chunks
                        await f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            await progress_bar(downloaded, total_size, "⬇️ **Downloading from Link...**", start_time, status_msg)
        return True
    except Exception as e:
        print(f"Link Download Error: {e}")
        return False

async def clean_up(file_paths):
    """Deletes temporary files."""
    for path in file_paths:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception as e:
                print(f"Error deleting {path}: {e}")

# --- Bot Handlers ---

@app.on_message(filters.command("start"))
async def start(client, message):
    user_id = message.from_user.id
    user_sessions[user_id] = {"state": STATE_WAIT_JSON, "data": {}}
    
    await message.reply_text(
        "👋 **Welcome! Let's create your video.**\n\n"
        "**Step 1:** Please upload the `map.json` file."
    )

@app.on_message(filters.document)
async def handle_document(client, message):
    user_id = message.from_user.id
    session = user_sessions.get(user_id)

    if not session:
        return # Ignore if user hasn't started

    # --- Step 1: Handle JSON ---
    if session["state"] == STATE_WAIT_JSON:
        if not message.document.file_name.endswith(".json"):
            await message.reply_text("❌ That doesn't look like a JSON file. Please try again.")
            return

        status_msg = await message.reply_text("📥 **Saving Map...**")
        
        # Save JSON with unique ID
        json_path = os.path.join(DOWNLOAD_DIR, f"{user_id}_map.json")
        await message.download(file_name=json_path)
        
        # Update Session
        session["data"]["json_path"] = json_path
        session["state"] = STATE_WAIT_VIDEO
        
        await status_msg.edit(
            "✅ **Map Saved!**\n\n"
            "**Step 2:** Now, send the **Video File** OR a **Direct Download Link**."
        )

    # --- Step 2 (Option A): Handle Video File Upload ---
    elif session["state"] == STATE_WAIT_VIDEO:
        # If user sends a document that is actually a video
        if message.document.mime_type.startswith("video"):
            session["data"]["video_source"] = "telegram"
            session["data"]["video_message"] = message # Store message to download later
            
            session["state"] = STATE_WAIT_NAME
            await message.reply_text(
                "✅ **Video received!**\n\n"
                "**Step 3:** Finally, send me the **Name** for the output file (e.g., `MyBigVideo`)."
            )
        else:
            await message.reply_text("❌ Please send a valid Video file or Link.")

@app.on_message(filters.video)
async def handle_video(client, message):
    user_id = message.from_user.id
    session = user_sessions.get(user_id)

    # --- Step 2 (Option B): Handle Video Media Upload ---
    if session and session["state"] == STATE_WAIT_VIDEO:
        session["data"]["video_source"] = "telegram"
        session["data"]["video_message"] = message
        
        session["state"] = STATE_WAIT_NAME
        await message.reply_text(
            "✅ **Video received!**\n\n"
            "**Step 3:** Finally, send me the **Name** for the output file (e.g., `MyBigVideo`)."
        )

@app.on_message(filters.text)
async def handle_text(client, message):
    user_id = message.from_user.id
    session = user_sessions.get(user_id)
    text = message.text.strip()

    if not session:
        return

    # --- Step 2 (Option C): Handle Link ---
    if session["state"] == STATE_WAIT_VIDEO:
        if text.startswith("http"):
            session["data"]["video_source"] = "link"
            session["data"]["video_link"] = text
            
            session["state"] = STATE_WAIT_NAME
            await message.reply_text(
                "✅ **Link received!**\n\n"
                "**Step 3:** Finally, send me the **Name** for the output file (e.g., `MyBigVideo`)."
            )
        else:
            await message.reply_text("❌ Invalid link. It must start with `http`.")

    # --- Step 3: Handle Name & START PROCESS ---
    elif session["state"] == STATE_WAIT_NAME:
        filename = text.replace(" ", "_").replace("/", "") # Sanitize
        session["state"] = STATE_PROCESSING # Lock state
        
        # Setup Paths
        json_path = session["data"]["json_path"]
        input_video_path = os.path.join(DOWNLOAD_DIR, f"{user_id}_input.mp4")
        output_video_path = os.path.join(OUTPUT_DIR, f"{filename}.mp4")
        
        status_msg = await message.reply_text("🚀 **Initializing Process...**")
        
        try:
            # 1. Download Video
            if session["data"]["video_source"] == "link":
                await status_msg.edit("⬇️ **Downloading Video from Link...**")
                success = await download_from_link(session["data"]["video_link"], input_video_path, status_msg)
                if not success:
                    raise Exception("Failed to download from link.")
            
            elif session["data"]["video_source"] == "telegram":
                await status_msg.edit("⬇️ **Downloading Video from Telegram...**")
                start_time = time.time()
                # Pyrogram's download with progress
                await session["data"]["video_message"].download(
                    file_name=input_video_path,
                    progress=progress_bar,
                    progress_args=("⬇️ **Downloading...**", start_time, status_msg)
                )

            # 2. Process Video
            async def update_status_text(txt):
                try:
                    await status_msg.edit(f"⚙️ **Processing...**\n\n{txt}")
                except: pass

            await update_status_text("Starting Engine...")
            await process_video_task(input_video_path, json_path, output_video_path, update_status_text)

            # 3. Upload Result
            await status_msg.edit("⬆️ **Uploading Final Video...**")
            start_time = time.time()
            
            await client.send_video(
                chat_id=message.chat.id,
                video=output_video_path,
                caption=f"✅ **{filename}** is ready!",
                progress=progress_bar,
                progress_args=("⬆️ **Uploading...**", start_time, status_msg)
            )

            await status_msg.delete()
            await message.reply_text("✨ **Job Done!** /start to do another.")

        except Exception as e:
            await status_msg.edit(f"❌ **Error:** {str(e)}")
            print(f"Error: {e}")
        
        finally:
            # 4. Cleanup
            await clean_up([json_path, input_video_path, output_video_path])
            user_sessions.pop(user_id, None)

if __name__ == "__main__":
    print("🤖 Bot Started...")
    app.run()
