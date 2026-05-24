import asyncio
import base64
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

import requests
import websockets

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

async def get_websocket_credentials():
    logger.info("Step 1: Getting WebSocket credentials from /query")
    response = requests.get("https://justdancenow.com/query")
    response.raise_for_status()
    data = response.json()
    logger.info(f"Credentials received: {data}")
    return data

async def get_signed_cookie_and_video(creds, song_id):
    ws_url = f"{creds['wsUrl']}?wsTag={creds['wsTag']}"
    logger.info(f"Connecting to WebSocket at {ws_url}")
    headers = {
        "Origin": "https://justdancenow.com",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    async with websockets.connect(ws_url, additional_headers=headers) as websocket:
        def to_base36(n):
            if n == 0: return '0'
            res = ""
            while n:
                n, r = divmod(n, 36)
                res = "0123456789abcdefghijklmnopqrstuvwxyz"[r] + res
            return res.zfill(4)

        logger.info("Sending songSelect directly...")
        s = json.dumps({'func': 'songSelect', 'song': song_id}, separators=(',', ':'))
        await websocket.send(to_base36(len(s)) + s)

        while True:
            response = await asyncio.wait_for(websocket.recv(), timeout=10.0)
            logger.info(f"Screen Received: {response}")
            if len(response) > 4:
                json_str = response[4:]
                try:
                    data = json.loads(json_str)
                    if data.get("func") == "registerRoom":
                        logger.info("Room registered! Sending songSelect from Screen...")
                        s = json.dumps({'func': 'songSelect', 'song': song_id}, separators=(',', ':'))
                        await websocket.send(to_base36(len(s)) + s)
                    elif data.get("func") == "songLaunched":
                        logger.info("Received songLaunched!")
                        return data.get("video"), data.get("cookieValue")
                except json.JSONDecodeError:
                    pass

def download_video(song_id, video_url, cookie_value):
    logger.info(f"Step 3: Downloading video with ffmpeg")
    headers = f"Cookie: hlscookie={cookie_value}\r\nReferer: https://justdancenow.com/\r\n"
    output_file = f"{song_id}_720p.mp4"
    
    cmd = [
        "ffmpeg",
        "-headers", headers,
        "-i", video_url,
        "-c", "copy",
        "-y",
        output_file
    ]
    logger.info(f"Running ffmpeg: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)
    logger.info(f"Downloaded video to {output_file}")

async def main(song_id):
    try:
        creds = await get_websocket_credentials()
        video_url, cookie_val = await get_signed_cookie_and_video(creds, song_id)
        if video_url and cookie_val:
            download_video(song_id, video_url, cookie_val)
        else:
            logger.error("Failed to extract video URL or cookie value")
    except Exception as e:
        logger.error(f"Error: {e}")

if __name__ == "__main__":
    song_id = sys.argv[1] if len(sys.argv) > 1 else "canttameher"
    asyncio.run(main(song_id))
