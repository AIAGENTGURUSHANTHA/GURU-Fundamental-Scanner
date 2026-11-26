# GURU Fundamental Scanner

A comprehensive stock and ETF analysis platform for Indian markets (NSE).

## Features

- Fundamental stock screening (screener.in data)
- Technical analysis — DMA, EMA, RSI, SMC zones
- Base Price Screener (3-yr / 5-yr SMA)
- GURU SMART BUSINESS screener
- Turtle Trading screener
- My Holdings — portfolio tracker with live NSE prices & virtual P&L
- Paiso Ka Jungle — SIP / lump-sum tracker for index ETFs
- Persistent data storage via Neon PostgreSQL

## Deployment

### Streamlit Community Cloud (primary)
1. Push to GitHub
2. Connect repo at [share.streamlit.io](https://share.streamlit.io)
3. Set secrets in app settings:
   ```
   ADMIN_PHONE = "+91XXXXXXXXXX"
   NEON_DATABASE_URL = "postgresql+psycopg2://..."

   [screener]
   username = "your@email.com"
   password = "yourpassword"
   ```

### Render.com (backup)
1. Connect GitHub repo in [Render dashboard](https://render.com)
2. Select **Web Service → Docker**
3. Add environment variables:
   - `ADMIN_PHONE`
   - `NEON_DATABASE_URL`
   - `SCREENER_USERNAME`
   - `SCREENER_PASSWORD`
4. Deploy — Render uses the included `Dockerfile` automatically

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

Add a `.streamlit/secrets.toml` with the keys above for local development.
