# Wholesale Cosmetics New Arrivals Monitor

Monitors `https://www.wholesale-cosmetics.co.uk/products/new/` for new products
and sends Discord embeds with barcode, pricing (ex/inc VAT), and a SellerAmp SAS link.

## What it detects
- New product added to the New Arrivals page
- Discord embed includes: product name, pack price, per-unit price (ex & inc VAT), pack size, RRP, EAN barcode, SellerAmp link

## Setup

### 1. Create a GitHub repo
Create a new **private** repo (e.g. `wholesale-cosmetics-monitor`) and push these files:
- `wholesale_cosmetics_monitor.py`
- `requirements.txt`
- `.github/workflows/monitor.yml`

### 2. Add Discord webhook secret
- Go to your repo → **Settings** → **Secrets and variables** → **Actions**
- Click **New repository secret**
- Name: `DISCORD_WEBHOOK`
- Value: your Discord webhook URL

### 3. Enable GitHub Actions
- Go to **Actions** tab in your repo
- Click **Enable GitHub Actions**
- Run manually first via **Run workflow** to test

The monitor runs every 30 minutes automatically. On first run it builds a
`snapshot.json` of all current products (no alerts). From then on, only
new additions trigger a Discord alert.

## Running locally
```bash
pip install playwright requests beautifulsoup4
python -m playwright install chromium
python wholesale_cosmetics_monitor.py
```

Set `CHECK_INTERVAL=60` env var to check every 60 seconds locally.
