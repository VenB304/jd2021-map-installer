import asyncio
import json
import logging
import urllib.parse
from typing import Optional, Dict

from playwright.async_api import async_playwright

logger = logging.getLogger("jd2021.extractors.jdnow_ws")

class JDNowWSExtractor:
    """
    Extracts Just Dance Now HLS video URLs and cookies by launching a Playwright
    browser instance and waiting for a controller (mobile phone) to select a song.
    """
    
    def __init__(self):
        self.cookie_value: Optional[str] = None
        self.video_url: Optional[str] = None
        self._got_event = asyncio.Event()
        self.target_song_id: Optional[str] = None

    async def _intercept_flow(self, p, headless: bool = False):
        browser = await p.chromium.launch(headless=headless)
        page = await browser.new_page()

        def on_websocket(ws):
            logger.info(f"WebSocket opened: {ws.url}")
            if "screen" not in ws.url:
                return

            def on_frame_received(text):
                if isinstance(text, str) and len(text) > 4:
                    try:
                        data = json.loads(text[4:])
                        func = data.get("func")
                        if func == "registerRoom":
                            room_id = data.get("roomID")
                            logger.info(f"Room created! Room ID: {room_id}")
                            print(f"\n=======================================================")
                            print(f"👉 PLEASE OPEN JUST DANCE NOW ON YOUR PHONE!")
                            print(f"👉 Enter Room Number: {room_id}")
                            print(f"👉 Select song: {self.target_song_id or 'ANY SONG'}")
                            print(f"=======================================================\n")
                            # Inject a giant overlay into the Playwright page!
                            overlay_js = f"""
                            (() => {{
                                const div = document.createElement('div');
                                div.style.position = 'fixed';
                                div.style.top = '0';
                                div.style.left = '0';
                                div.style.width = '100vw';
                                div.style.height = '100vh';
                                div.style.backgroundColor = 'rgba(0, 0, 0, 0.85)';
                                div.style.color = 'white';
                                div.style.zIndex = '999999';
                                div.style.display = 'flex';
                                div.style.flexDirection = 'column';
                                div.style.justifyContent = 'center';
                                div.style.alignItems = 'center';
                                div.style.fontFamily = 'sans-serif';
                                div.innerHTML = `
                                    <h1 style="font-size: 4rem; margin-bottom: 20px;">📲 JD2021 MAP INSTALLER</h1>
                                    <h2 style="font-size: 2.5rem; margin-bottom: 40px; color: #00ff00;">Please open Just Dance Now on your phone!</h2>
                                    <p style="font-size: 2rem; margin-bottom: 10px;">1. Connect to Room Number:</p>
                                    <p style="font-size: 6rem; font-weight: bold; color: #ff00ff; margin: 0 0 40px 0;">{room_id}</p>
                                    <p style="font-size: 2rem;">2. Select the song:</p>
                                    <p style="font-size: 3rem; font-weight: bold; color: #00ffff;">{self.target_song_id or 'Any Song'}</p>
                                    <p style="font-size: 1.5rem; margin-top: 50px; opacity: 0.8;">The video will automatically download once the song starts.</p>
                                `;
                                document.body.appendChild(div);
                            }})();
                            """
                            asyncio.create_task(page.evaluate(overlay_js))
                        elif func == "songLaunched":
                            self.cookie_value = data.get("cookieValue")
                            self.video_url = data.get("video")
                            logger.info(f"Intercepted songLaunched! Cookie: {self.cookie_value}")
                            self._got_event.set()
                    except Exception:
                        pass
            ws.on("framereceived", on_frame_received)

        page.on("websocket", on_websocket)

        logger.info("Navigating to Just Dance Now...")
        await page.goto("https://justdancenow.com")

        # Wait until the user selects the song and we get the cookie
        try:
            await asyncio.wait_for(self._got_event.wait(), timeout=300.0)
        except asyncio.TimeoutError:
            logger.error("Timed out waiting for user to select song on mobile.")
            
        await browser.close()

    async def get_hls_stream(self, song_id: str) -> Dict[str, str]:
        """
        Launch the browser, wait for user interaction, and return the video URL and cookie.
        """
        self.target_song_id = song_id
        self.cookie_value = None
        self.video_url = None
        self._got_event.clear()

        async with async_playwright() as p:
            await self._intercept_flow(p, headless=False)

        if not self.cookie_value or not self.video_url:
            raise Exception("Failed to intercept HLS cookie.")

        return {
            "video_url": self.video_url,
            "cookie": self.cookie_value
        }

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    extractor = JDNowWSExtractor()
    result = asyncio.run(extractor.get_hls_stream("canttameher"))
    print("Final Result:", result)
