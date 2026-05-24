import asyncio
import websockets
import json
import logging

logging.basicConfig(level=logging.INFO)

async def test_play_ws():
    import requests
    creds = requests.get("https://justdancenow.com/query").json()
    ws_url = f"{creds['wsUrl']}/play?room=11138&client={creds['wsClient']}&tag={creds['wsTag']}"
    headers = {"Origin": "https://justdancenow.com"}
    try:
        async with websockets.connect(ws_url, additional_headers=headers, subprotocols=["play.justdancenow.com"]) as ws:
            logging.info("Connected to PLAY websocket!")
            await asyncio.sleep(2)
    except Exception as e:
        logging.error(f"Failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_play_ws())
