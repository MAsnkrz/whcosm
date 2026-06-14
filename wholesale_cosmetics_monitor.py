"""
Wholesale Cosmetics Monitor
Monitors https://www.wholesale-cosmetics.co.uk/products/new/

Detects and alerts on Discord for:
  - New product listings
  - Price drops
  - Price increases
  - Stock increases (restock)
  - Stock decreases
  - Out of stock
  - Back in stock

Deps:  pip install playwright requests beautifulsoup4
       python -m playwright install chromium
"""

import json
import os
import random
import re
import time
import requests
from datetime import datetime, timezone
from urllib.parse import quote
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

BASE_URL       = "https://www.wholesale-cosmetics.co.uk"
NEW_URL        = f"{BASE_URL}/products/new/"
SNAPSHOT_FILE  = "snapshot.json"
REQUEST_DELAY  = 4.0   # seconds between page fetches — increased to avoid 429s
RUN_ONCE       = os.getenv("RUN_ONCE", "false").lower() == "true"
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
HEADLESS       = os.getenv("HEADLESS", "true").lower() == "true"

DISCORD_WEBHOOK = os.getenv(
    "DISCORD_WEBHOOK",
    "https://discord.com/api/webhooks/1515746540453625906/NrYIHjN-RqfuqV-gAqL8WIL3roU1XxYJfzRz9vXGJ7uwuFEA7-zF4-MWyW2aBq89Vpl7"
)

# Discord embed colours
COLOUR_NEW        = 0xE91E8C   # Pink    — new product
COLOUR_PRICE_DROP = 0x2ECC71   # Green   — price drop
COLOUR_PRICE_UP   = 0xE74C3C   # Red     — price increase
COLOUR_RESTOCK    = 0x3498DB   # Blue    — stock went up
COLOUR_LOW_STOCK  = 0xF39C12   # Orange  — stock went down
COLOUR_OOS        = 0x95A5A6   # Grey    — out of stock
COLOUR_BACK       = 0x9B59B6   # Purple  — back in stock

# ---------------------------------------------------------------------------
# BROWSER
# ---------------------------------------------------------------------------

def make_browser(playwright):
    browser = playwright.chromium.launch(headless=HEADLESS)
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="en-GB",
        viewport={"width": 1280, "height": 800},
    )
    return browser, context


def fetch_html(context, url, wait_selector=None, timeout=20000, retries=3):
    """Fetch page HTML with automatic retry on 429 / Too Many Requests."""
    for attempt in range(retries):
        page = context.new_page()
        try:
            page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=8000)
                except PWTimeout:
                    pass
            html = page.content()

            # Detect rate limiting via page title
            title = page.title()
            if "too many requests" in title.lower() or "429" in title:
                wait_secs = 15 * (attempt + 1)
                print(f"  [!] Rate limited (attempt {attempt+1}/{retries}) — waiting {wait_secs}s")
                page.close()
                time.sleep(wait_secs)
                continue

            return html
        except Exception as e:
            print(f"  [!] Fetch error ({url}): {e} — attempt {attempt+1}/{retries}")
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
        finally:
            try:
                page.close()
            except Exception:
                pass
    return None


# ---------------------------------------------------------------------------
# SCRAPING
# ---------------------------------------------------------------------------

def parse_listing_page(html):
    soup = BeautifulSoup(html, "html.parser")
    products = []
    seen = set()

    for a in soup.find_all("a", href=re.compile(r"/product/[^/]+/\d+/")):
        href = a["href"]
        m = re.search(r"/product/([^/]+)/(\d+)/", href)
        if not m:
            continue
        slug, pid = m.group(1), m.group(2)
        if pid in seen:
            continue
        seen.add(pid)

        card = a.find_parent("div") or a.find_parent("li")
        title = pack_price = reduced_price = per_unit = ""

        if card:
            for tl in card.find_all("a", href=href):
                txt = tl.get_text(strip=True)
                if len(txt) > 5 and txt.lower() != "view":
                    title = txt
                    break
            ct = card.get_text(" ", strip=True)
            pp = re.search(r"Pack Price:\s*£?([\d.]+)", ct)
            if pp: pack_price = pp.group(1)
            rp = re.search(r"Reduced:\s*£?([\d.]+)", ct)
            if rp: reduced_price = rp.group(1)
            pu = re.search(r"\(£?([\d.]+)\s*per unit\)", ct)
            if pu: per_unit = pu.group(1)

        full_url = href if href.startswith("http") else BASE_URL + href
        products.append({
            "id": pid, "slug": slug, "title": title,
            "pack_price": pack_price, "reduced_price": reduced_price,
            "per_unit": per_unit, "url": full_url,
        })

    soup2 = soup
    next_pg = None
    for a in soup2.find_all("a", href=re.compile(r"/products/new/\d+/")):
        nm = re.search(r"/products/new/(\d+)/", a["href"])
        if nm:
            next_pg = int(nm.group(1))

    return products, next_pg is not None


def scrape_all_new_arrivals(context):
    all_products = []
    page_num = 1
    while True:
        url = f"{NEW_URL}{page_num}/"
        print(f"  Fetching listing page {page_num}...")
        html = fetch_html(context, url)
        if not html:
            break
        products, _ = parse_listing_page(html)
        if not products:
            break
        all_products.extend(products)
        soup = BeautifulSoup(html, "html.parser")
        if not soup.find("a", href=re.compile(rf"/products/new/{page_num + 1}/")):
            break
        page_num += 1
        time.sleep(REQUEST_DELAY)
    return all_products


def scrape_product_detail(context, product):
    url = product["url"]
    html = fetch_html(context, url, wait_selector=".barcodetext", timeout=15000)
    if not html:
        return product

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    barcode_el = soup.find(class_="barcodetext")
    product["barcode"] = barcode_el.get_text(strip=True) if barcode_el else ""

    sku_m = re.search(r"\b([A-Z0-9]{5,20})\b\s+\d{13}", text)
    product["sku"] = sku_m.group(1) if sku_m else ""

    ps = re.search(r"Pack Size:\s*(\d+)\s*units?", text)
    product["pack_size"] = ps.group(1) if ps else ""

    rrp = re.search(r"RRP\s*£?([\d.]+)", text)
    product["rrp"] = rrp.group(1) if rrp else ""

    if not product["title"]:
        h1 = soup.find("h1")
        product["title"] = h1.get_text(strip=True) if h1 else product["slug"].replace("-", " ").title()

    pp = re.search(r"Price:\s*£([\d.]+)", text)
    if pp: product["pack_price"] = pp.group(1)
    rp = re.search(r"Reduced:\s*£([\d.]+)", text)
    if rp: product["reduced_price"] = rp.group(1)
    pu = re.search(r"\(£([\d.]+)\s*per unit\)", text)
    if pu: product["per_unit"] = pu.group(1)

    img_tag = soup.find("img", src=re.compile(r"/images/C[\s(]", re.IGNORECASE))
    product["image"] = (BASE_URL + img_tag["src"]) if img_tag else ""

    return product


# ---------------------------------------------------------------------------
# STOCK CHECK
# ---------------------------------------------------------------------------

def get_stock_from_page(context, product_url, pack_size):
    """Set qty=9999, JS caps to max packs in stock. No login needed."""
    page = context.new_page()
    try:
        page.goto(product_url, timeout=20000, wait_until="domcontentloaded")
        try:
            page.wait_for_selector("#quantity", timeout=8000)
        except PWTimeout:
            return None, None

        page.evaluate("""
            const q = document.getElementById('quantity');
            if (q) {
                q.value = '9999';
                q.dispatchEvent(new Event('change', {bubbles: true}));
                q.dispatchEvent(new Event('blur',   {bubbles: true}));
                q.dispatchEvent(new Event('input',  {bubbles: true}));
            }
        """)
        time.sleep(0.5)

        capped = page.evaluate("document.getElementById('quantity')?.value")
        if not capped or not str(capped).isdigit():
            return None, None

        packs = int(capped)
        ps    = int(pack_size) if pack_size and str(pack_size).isdigit() else 1
        return packs, packs * ps

    except Exception as e:
        print(f"  [!] Stock check error: {e}")
        return None, None
    finally:
        page.close()


# ---------------------------------------------------------------------------
# PRICING HELPERS
# ---------------------------------------------------------------------------

def effective_price(product):
    return product.get("reduced_price") or product.get("pack_price") or "0"


def vat_price(price_str):
    try:
        return f"{float(price_str) * 1.2:.2f}"
    except (ValueError, TypeError):
        return price_str


def selleramp_url(barcode, cost_price_str):
    if not barcode:
        return None
    return (
        f"https://sas.selleramp.com/sas/lookup/"
        f"?search_term={barcode}&sas_cost_price={vat_price(cost_price_str)}"
    )


def safe_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# DISCORD EMBEDS
# ---------------------------------------------------------------------------

def _base_fields(product):
    """Common fields used across all embed types."""
    barcode  = product.get("barcode", "")
    per_unit = product.get("per_unit", "")
    pack_size = product.get("pack_size", "?")
    rrp      = product.get("rrp", "")
    packs    = product.get("packs_in_stock")
    units    = product.get("units_in_stock")
    sas_url  = selleramp_url(barcode, per_unit or effective_price(product))

    fields = [
        {"name": "📦 Pack Size",     "value": f"{pack_size} units" if pack_size else "-", "inline": True},
        {"name": "🏷️ RRP (each)",    "value": f"£{rrp}" if rrp else "-",                  "inline": True},
        {"name": "🔢 Barcode / EAN", "value": f"`{barcode}`" if barcode else "-",         "inline": True},
        {"name": "📊 Stock (Packs)", "value": f"**{packs}** packs" if packs is not None else "-", "inline": True},
        {"name": "📊 Stock (Units)", "value": f"**{units}** units" if units is not None else "-", "inline": True},
    ]

    if sas_url:
        fields.append({"name": "🔍 SellerAmp SAS", "value": f"[Open in SellerAmp]({sas_url})", "inline": False})

    return fields


def _send_embed(embed):
    payload = {"embeds": [embed]}
    try:
        r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        if r.status_code == 429:
            wait = float(r.json().get("retry_after", 5)) + 0.5
            print(f"  [!] Discord rate limited — waiting {wait:.1f}s")
            time.sleep(wait)
            requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        else:
            r.raise_for_status()
    except Exception as e:
        print(f"  [!] Discord error: {e}")


def _thumbnail(product):
    image = product.get("image", "")
    return {"url": quote(image, safe=":/?=&")} if image else None


def notify_new(product):
    pack_price = product.get("pack_price", "")
    reduced    = product.get("reduced_price", "")
    per_unit   = product.get("per_unit", "")

    price_display = f"£{pack_price} -> **£{reduced}**" if reduced else f"**£{pack_price}**"

    fields = [
        {"name": "💰 Pack Price (ex. VAT)", "value": price_display,                                  "inline": True},
        {"name": "💷 Per Unit (ex. VAT)",   "value": f"£{per_unit}" if per_unit else "-",            "inline": True},
        {"name": "💷 Per Unit (inc. VAT)",  "value": f"£{vat_price(per_unit)}" if per_unit else "-", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"🆕  NEW LISTING — {product.get('title', '')}",
        "url":       product.get("url", BASE_URL),
        "color":     COLOUR_NEW,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Wholesale Cosmetics Monitor • wholesale-cosmetics.co.uk"},
    }
    thumb = _thumbnail(product)
    if thumb: embed["thumbnail"] = thumb
    _send_embed(embed)


def notify_price_change(product, old_price, new_price, is_drop):
    per_unit   = product.get("per_unit", "")
    old_f      = safe_float(old_price)
    new_f      = safe_float(new_price)
    diff       = f"£{abs(new_f - old_f):.2f}" if old_f and new_f else "?"
    pct        = f"{abs((new_f - old_f) / old_f * 100):.1f}%" if old_f and new_f else "?"

    if is_drop:
        title  = f"💰  PRICE DROP — {product.get('title', '')}"
        colour = COLOUR_PRICE_DROP
        change = f"↓ -{diff} (-{pct})"
    else:
        title  = f"📈  PRICE INCREASE — {product.get('title', '')}"
        colour = COLOUR_PRICE_UP
        change = f"↑ +{diff} (+{pct})"

    fields = [
        {"name": "💰 Old Pack Price", "value": f"£{old_price}",  "inline": True},
        {"name": "💰 New Pack Price", "value": f"**£{new_price}**", "inline": True},
        {"name": "📉 Change",         "value": change,            "inline": True},
        {"name": "💷 Per Unit (ex. VAT)",  "value": f"£{per_unit}" if per_unit else "-",            "inline": True},
        {"name": "💷 Per Unit (inc. VAT)", "value": f"£{vat_price(per_unit)}" if per_unit else "-", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     title,
        "url":       product.get("url", BASE_URL),
        "color":     colour,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Wholesale Cosmetics Monitor • wholesale-cosmetics.co.uk"},
    }
    thumb = _thumbnail(product)
    if thumb: embed["thumbnail"] = thumb
    _send_embed(embed)


def notify_stock_change(product, old_packs, new_packs, is_restock):
    pack_size = product.get("pack_size", "1")
    ps        = int(pack_size) if str(pack_size).isdigit() else 1
    old_units = old_packs * ps if old_packs is not None else "?"
    new_units = new_packs * ps if new_packs is not None else "?"
    diff_packs = (new_packs - old_packs) if (new_packs is not None and old_packs is not None) else "?"

    if is_restock:
        title  = f"🟢  RESTOCK — {product.get('title', '')}"
        colour = COLOUR_RESTOCK
        arrow  = f"↑ +{diff_packs} packs"
    else:
        title  = f"📉  STOCK DROP — {product.get('title', '')}"
        colour = COLOUR_LOW_STOCK
        arrow  = f"↓ {diff_packs} packs"

    fields = [
        {"name": "📊 Old Stock", "value": f"{old_packs} packs / {old_units} units", "inline": True},
        {"name": "📊 New Stock", "value": f"**{new_packs} packs / {new_units} units**", "inline": True},
        {"name": "📉 Change",    "value": arrow,                                        "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     title,
        "url":       product.get("url", BASE_URL),
        "color":     colour,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Wholesale Cosmetics Monitor • wholesale-cosmetics.co.uk"},
    }
    thumb = _thumbnail(product)
    if thumb: embed["thumbnail"] = thumb
    _send_embed(embed)


def notify_oos(product):
    fields = _base_fields(product)
    embed = {
        "title":     f"🔴  OUT OF STOCK — {product.get('title', '')}",
        "url":       product.get("url", BASE_URL),
        "color":     COLOUR_OOS,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Wholesale Cosmetics Monitor • wholesale-cosmetics.co.uk"},
    }
    thumb = _thumbnail(product)
    if thumb: embed["thumbnail"] = thumb
    _send_embed(embed)


def notify_back_in_stock(product):
    per_unit = product.get("per_unit", "")
    fields = [
        {"name": "💷 Per Unit (ex. VAT)",  "value": f"£{per_unit}" if per_unit else "-",            "inline": True},
        {"name": "💷 Per Unit (inc. VAT)", "value": f"£{vat_price(per_unit)}" if per_unit else "-", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"🟢  BACK IN STOCK — {product.get('title', '')}",
        "url":       product.get("url", BASE_URL),
        "color":     COLOUR_BACK,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Wholesale Cosmetics Monitor • wholesale-cosmetics.co.uk"},
    }
    thumb = _thumbnail(product)
    if thumb: embed["thumbnail"] = thumb
    _send_embed(embed)


# ---------------------------------------------------------------------------
# SNAPSHOT
# ---------------------------------------------------------------------------

def load_snapshot():
    if os.path.exists(SNAPSHOT_FILE):
        with open(SNAPSHOT_FILE) as f:
            return json.load(f)
    return {}


def save_snapshot(data):
    with open(SNAPSHOT_FILE, "w") as f:
        json.dump(data, f, indent=2)


def snapshot_entry(product):
    """Build the snapshot record for a product."""
    return {
        "title":         product.get("title", ""),
        "url":           product.get("url", ""),
        "barcode":       product.get("barcode", ""),
        "image":         product.get("image", ""),
        "pack_price":    product.get("pack_price", ""),
        "reduced_price": product.get("reduced_price", ""),
        "per_unit":      product.get("per_unit", ""),
        "pack_size":     product.get("pack_size", ""),
        "rrp":           product.get("rrp", ""),
        "packs_in_stock": product.get("packs_in_stock"),
        "in_stock":      product.get("packs_in_stock", 0) is not None and (product.get("packs_in_stock") or 0) > 0,
        "first_seen":    datetime.now(timezone.utc).isoformat(),
        "last_updated":  datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# CHANGE DETECTION
# ---------------------------------------------------------------------------

def check_changes(product, old):
    """
    Compare current product data against snapshot.
    Fires relevant Discord notifications for any changes found.
    Returns updated snapshot entry.
    """
    old_price   = old.get("reduced_price") or old.get("pack_price") or ""
    new_price   = product.get("reduced_price") or product.get("pack_price") or ""
    old_packs   = old.get("packs_in_stock")
    new_packs   = product.get("packs_in_stock")
    was_in_stock = old.get("in_stock", True)
    now_in_stock = new_packs is not None and new_packs > 0

    old_f = safe_float(old_price)
    new_f = safe_float(new_price)

    # Fill in cached fields for embed if detail scrape was skipped
    if not product.get("image"):      product["image"]    = old.get("image", "")
    if not product.get("barcode"):    product["barcode"]  = old.get("barcode", "")
    if not product.get("rrp"):        product["rrp"]      = old.get("rrp", "")
    if not product.get("pack_size"):  product["pack_size"] = old.get("pack_size", "")

    # 1. Back in stock
    if not was_in_stock and now_in_stock:
        print(f"  -> BACK IN STOCK: {product['title']}")
        notify_back_in_stock(product)
        time.sleep(1)

    # 2. Out of stock
    elif was_in_stock and not now_in_stock and new_packs is not None:
        print(f"  -> OUT OF STOCK: {product['title']}")
        notify_oos(product)
        time.sleep(1)

    # 3. Price drop
    elif old_f and new_f and new_f < old_f - 0.01:
        print(f"  -> PRICE DROP: {product['title']} £{old_price} -> £{new_price}")
        notify_price_change(product, old_price, new_price, is_drop=True)
        time.sleep(1)

    # 4. Price increase
    elif old_f and new_f and new_f > old_f + 0.01:
        print(f"  -> PRICE UP: {product['title']} £{old_price} -> £{new_price}")
        notify_price_change(product, old_price, new_price, is_drop=False)
        time.sleep(1)

    # 5. Restock (stock went up)
    if old_packs is not None and new_packs is not None and now_in_stock:
        if new_packs > old_packs + 2:  # +2 buffer to avoid noise
            print(f"  -> RESTOCK: {product['title']} {old_packs} -> {new_packs} packs")
            notify_stock_change(product, old_packs, new_packs, is_restock=True)
            time.sleep(1)
        elif new_packs < old_packs - 2:
            print(f"  -> STOCK DROP: {product['title']} {old_packs} -> {new_packs} packs")
            notify_stock_change(product, old_packs, new_packs, is_restock=False)
            time.sleep(1)

    return snapshot_entry(product)


# ---------------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------------

def run_check():
    print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}] Running check...")

    with sync_playwright() as pw:
        browser, context = make_browser(pw)
        try:
            snapshot = load_snapshot()
            known_ids = set(snapshot.keys())

            # 1. Scrape all listing pages
            all_products = scrape_all_new_arrivals(context)
            if not all_products:
                print("  [!] No products scraped — possible site issue")
                return

            current_ids = {p["id"] for p in all_products}
            new_ids = current_ids - known_ids
            print(f"  {len(all_products)} products found, {len(new_ids)} new, "
                  f"{len(current_ids - new_ids)} existing to check")

            for product in all_products:
                pid = product["id"]
                is_new = pid in new_ids

                # Always fetch detail + stock for new products
                # For existing: fetch detail + stock to detect changes
                time.sleep(REQUEST_DELAY + random.uniform(0, 2))
                product = scrape_product_detail(context, product)

                time.sleep(REQUEST_DELAY + random.uniform(0, 2))
                packs, units = get_stock_from_page(
                    context, product["url"], product.get("pack_size", "1")
                )
                product["packs_in_stock"] = packs
                product["units_in_stock"] = units

                if is_new:
                    print(f"  -> NEW: [{pid}] {product['title']}")
                    notify_new(product)
                    time.sleep(1.5)
                    snapshot[pid] = snapshot_entry(product)
                else:
                    old = snapshot[pid]
                    updated = check_changes(product, old)
                    # Always update snapshot with latest data
                    updated["first_seen"] = old.get("first_seen", updated["first_seen"])
                    snapshot[pid] = updated

            save_snapshot(snapshot)
            print(f"  Snapshot saved ({len(snapshot)} products tracked)")

        finally:
            browser.close()


def main():
    print("=" * 55)
    print("  Wholesale Cosmetics Monitor")
    print(f"  Watching: {NEW_URL}")
    print("  Tracking: new listings, price changes, stock changes")
    print("=" * 55)

    if RUN_ONCE:
        run_check()
    else:
        while True:
            try:
                run_check()
            except Exception as e:
                print(f"  [!] Unexpected error: {e}")
            print(f"  Sleeping {CHECK_INTERVAL}s...")
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
