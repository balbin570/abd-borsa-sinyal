import os
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import httpx

app = FastAPI(title="ABD Borsa Sinyal", version="1.0")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

class Signal(BaseModel):
    symbol: str
    price: float
    timeframe: str = "?"
    signal: str = "BULLISH"

@app.get("/")
async def home():
    return {"status": "online", "service": "ABD Borsa Sinyal"}

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.post("/webhook/tradingview")
async def tradingview_webhook(request: Request, signal: Signal):
    # TradingView custom headers are not relied on here.
    # The secret is supplied as ?key=... in the webhook URL.
    key = request.query_params.get("key", "")
    if not WEBHOOK_SECRET or key != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise HTTPException(status_code=500, detail="Telegram settings missing")

    message = (
        "TRADINGVIEW SIGNAL\n\n"
        f"Symbol: {signal.symbol}\n"
        f"Price: ${signal.price:.2f}\n"
        f"Timeframe: {signal.timeframe}\n"
        f"Signal: {signal.signal}"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.post(
            url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message}
        )
        response.raise_for_status()

    return {"status": "received", "symbol": signal.symbol}
