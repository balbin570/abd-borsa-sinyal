# ABD Borsa Sinyal

TradingView sinyallerini Render üzerinden Telegram'a ileten başlangıç projesi.

## Gizli bilgiler
Telegram bot tokenınızı veya WEBHOOK_SECRET değerini GitHub'a yüklemeyin.
Bunlar Render > Environment bölümünde girilecektir.

## Render
Build Command:
`pip install -r requirements.txt`

Start Command:
`uvicorn app.main:app --host 0.0.0.0 --port $PORT`

## TradingView webhook
Render adresiniz oluşunca webhook adresi şu biçimde olacaktır:

`https://UYGULAMA-ADRESINIZ.onrender.com/webhook/tradingview?key=WEBHOOK_SECRET`

Alarm mesajı örneği:

```json
{
  "symbol": "{{ticker}}",
  "price": {{close}},
  "timeframe": "{{interval}}",
  "signal": "BULLISH"
}
```

Not: WEBHOOK_SECRET değerini güçlü ve benzersiz seçin ve kimseyle paylaşmayın.
