"""
Wholesale Cosmetics Monitor — Clean Rewrite
Monitors https://www.wholesale-cosmetics.co.uk/products/new/1/

Alerts on:
  🆕 New listings (in stock, first seen)
  🟢 Back in stock (was OOS/missing, now appears on listing)
  📦 Restocks (price drop on known product — used as restock signal)
  📉 Price drops (>1% AND >£0.02)

No alerts for: price increases, going OOS.

Key improvements over previous version:
  - No Playwright — uses plain requests (faster, no browser install needed)
  - Only scrapes product detail pages for NEW products (saves time)
  - Fixed image extraction (og:image was broken on this site)
  - Fixed price parsing — uses scraper logic to avoid container bleed
  - SAS EAN + SAS Title links in every embed
  - Atomic snapshot saves — crash-safe
  - BOGOF price halving

Env vars:
  DISCORD_WEBHOOK   required
  CHECK_INTERVAL    seconds between checks (default 1800 = 30min)
  RUN_ONCE          "true" for GitHub Actions single-shot mode

Usage:
  pip install requests beautifulsoup4
  python wholesale_cosmetics_monitor.py
"""

import json
import os
import re
import time
import requests
from datetime import datetime, timezone
from urllib.parse import quote
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

BASE_URL        = "https://www.wholesale-cosmetics.co.uk"
LISTING_URL     = f"{BASE_URL}/products/new/"
SNAPSHOT_FILE   = "snapshot_wholesalecosmetics.json"
BASELINE_FLAG   = "baseline_done_wholesalecosmetics.txt"
CHECK_INTERVAL  = int(os.getenv("CHECK_INTERVAL", "1800"))
RUN_ONCE        = os.getenv("RUN_ONCE", "false").lower() == "true"
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")
PAGE_DELAY      = 2.0
DETAIL_DELAY    = 1.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept": "text/html,*/*;q=0.9",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# Embed colours
COL_NEW    = 0xE91E8C   # pink
COL_BACK   = 0x9B59B6   # purple
COL_STOCK  = 0x3498DB   # blue
COL_DROP20 = 0x00C853   # deep green
COL_DROP10 = 0x2ECC71   # green
COL_DROP   = 0x82E0AA   # light green

# ---------------------------------------------------------------------------
# SCRAPING — requests only, no Playwright
# ---------------------------------------------------------------------------

def fetch_page(url, retries=3):
    for attempt in range(retries):
        try:
            r = SESSION.get(url, timeout=20)
            if r.status_code == 429:
                wait = 15 * (attempt + 1)
                print(f"  [!] Rate limited — waiting {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.text
        except Exception as e:
            print(f"  [!] Fetch error ({url}): {e} — attempt {attempt+1}/{retries}")
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
    return None


def parse_listing_page(html):
    """
    Parse one listing page. Returns (products list, has_next bool).

    Price extraction looks at text STRICTLY between consecutive product
    title h3 elements to prevent price bleed from adjacent products.
    Image is taken from the product card img, not og:image (which is
    broken on this site — returns a URL with no filename).
    """
    soup = BeautifulSoup(html, "html.parser")
    products = []

    # Find all product title links — format: /product/slug/ID/
    product_h3s = [
        h for h in soup.find_all(["h3", "h2"])
        if h.find("a", href=lambda h: h and "/product/" in h)
    ]

    for idx, h3 in enumerate(product_h3s):
        link = h3.find("a", href=lambda h: h and "/product/" in h)
        if not link:
            continue

        title       = link.get_text(strip=True)
        product_url = link["href"]
        if not product_url.startswith("http"):
            product_url = BASE_URL + product_url

        # Extract product ID from URL: /product/slug/ID/
        id_m = re.search(r"/product/[^/]+/(\d+)/?$", product_url)
        if not id_m:
            continue
        product_id = id_m.group(1)

        # Get text STRICTLY between this h3 and the next one
        # Prevents price/image bleed from adjacent products
        text_between = []
        node = h3.next_sibling
        next_h3 = product_h3s[idx + 1] if idx + 1 < len(product_h3s) else None
        while node and node != next_h3:
            if hasattr(node, "get_text"):
                t = node.get_text(" ", strip=True)
                if t:
                    text_between.append(t)
            elif isinstance(node, str):
                text_between.append(node.strip())
            node = node.next_sibling
        text = " ".join(text_between)

        # Prices
        m_reduced = re.search(r"Reduced:\s*£?\s*([\d.]+)", text, re.IGNORECASE)
        m_pack    = re.search(r"Pack\s+Price:\s*£?\s*([\d.]+)", text, re.IGNORECASE)
        m_unit    = re.search(r"\(£?\s*([\d.]+)\s*per\s*unit\)", text, re.IGNORECASE)
        m_bogof   = re.search(r"WITH\s+BOGOF:\s*\(£?\s*([\d.]+)\s*per\s*unit\)", text, re.IGNORECASE)

        pack_price    = m_pack.group(1) if m_pack else ""
        reduced_price = m_reduced.group(1) if m_reduced else ""
        per_unit      = m_bogof.group(1) if m_bogof else (m_unit.group(1) if m_unit else "")
        is_bogof      = bool(m_bogof)

        # Pack qty from title
        pack_qty = 1
        m_qty = re.match(r"^(\d+)\s+x\s+", title, re.IGNORECASE)
        if m_qty:
            pack_qty = int(m_qty.group(1))

        # Image — from container img, NOT og:image (broken on this site)
        # Walk up from h3 to find a container with an img
        container = h3
        for _ in range(6):
            container = container.parent
            if container is None:
                break
            if container.name in ("li", "div", "article"):
                break
        image = ""
        if container:
            img_el = container.find("img")
            if img_el:
                src = (img_el.get("data-src") or img_el.get("src") or "")
                if src and "logo" not in src.lower() and "blank" not in src.lower():
                    image = src if src.startswith("http") else BASE_URL + src

        products.append({
            "id":            product_id,
            "title":         title,
            "url":           product_url,
            "image":         image,
            "pack_price":    pack_price,
            "reduced_price": reduced_price,
            "per_unit":      per_unit,
            "is_bogof":      is_bogof,
            "pack_qty":      pack_qty,
        })

    # Has next page?
    has_next = bool(soup.find("a", href=lambda h: h and re.search(r"/products/new/\d+/", h or "")))

    return products, has_next


def fetch_all_products():
    """Paginate through all new arrivals listing pages."""
    all_products = []
    seen_ids = set()
    page = 1

    while True:
        url = f"{LISTING_URL}{page}/"
        html = fetch_page(url)
        if not html:
            print(f"  Page {page}: failed to fetch")
            break

        products, has_next = parse_listing_page(html)

        new = [p for p in products if p["id"] not in seen_ids]
        for p in new:
            seen_ids.add(p["id"])
        all_products.extend(new)

        print(f"  Page {page}: +{len(new)} products (total: {len(all_products)})")

        if not new or not has_next:
            break

        page += 1
        time.sleep(PAGE_DELAY)

    return all_products


def fetch_product_detail(product):
    """
    Scrape a product detail page for:
    - Barcode / EAN (from .barcodetext or text)
    - Better image (from main product section, not og:image)
    - Brand name
    - Pack size
    - RRP

    Called ONLY for new products — not on every run.
    """
    url = product["url"]
    html = fetch_page(url)
    if not html:
        return product

    soup = BeautifulSoup(html, "html.parser")

    # Restrict to before "Related Products" section
    full_text = soup.get_text(" ", strip=True)
    related_idx = full_text.lower().find("related products")
    main_text = full_text[:related_idx] if related_idx > 0 else full_text

    # Barcode — try .barcodetext class first (site-specific)
    barcode_el = soup.find(class_="barcodetext")
    if barcode_el:
        product["barcode"] = barcode_el.get_text(strip=True)
    else:
        m = re.search(r"(?:EAN|Barcode|GTIN)[\s:]+([0-9]{6,14})", main_text, re.IGNORECASE)
        product["barcode"] = m.group(1) if m else ""

    # Brand
    brand_m = re.search(r"Brand:\s*([^\n,]+)", main_text, re.IGNORECASE)
    product["brand"] = brand_m.group(1).strip() if brand_m else ""

    # Pack size
    ps_m = re.search(r"Pack\s+Size:\s*(\d+)\s*units?", main_text, re.IGNORECASE)
    product["pack_size"] = ps_m.group(1) if ps_m else str(product.get("pack_qty", ""))

    # RRP
    rrp_m = re.search(r"RRP\s*£?([\d.]+)\s*each", main_text, re.IGNORECASE)
    product["rrp"] = rrp_m.group(1) if rrp_m else ""

    # Better image — find main product image before Related Products
    # og:image is broken on this site (returns URL with no filename)
    # Instead look for real product image patterns
    if not product.get("image"):
        for img in soup.find_all("img", src=re.compile(r"\.(jpg|jpeg|png|webp)", re.IGNORECASE)):
            src = img.get("src", "")
            if src and "logo" not in src.lower() and "blank" not in src.lower():
                product["image"] = src if src.startswith("http") else BASE_URL + src
                break

    return product

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def effective_price(product):
    """Best available price — reduced if available, else pack price."""
    return product.get("reduced_price") or product.get("pack_price") or ""


def safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def vat(price_str):
    f = safe_float(price_str)
    return f"{f * 1.2:.2f}" if f else price_str


def sas_ean(barcode, cost):
    if not barcode:
        return None
    return f"https://sas.selleramp.com/sas/lookup/?search_term={barcode}&sas_cost_price={vat(cost)}"


def sas_title(title, cost):
    return f"https://sas.selleramp.com/sas/lookup/?search_term={quote(title)}&sas_cost_price={vat(cost)}"


# ---------------------------------------------------------------------------
# DISCORD
# ---------------------------------------------------------------------------

def _send(payload):
    if not DISCORD_WEBHOOK:
        return
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


def _base_fields(product):
    """Common fields shown in all embed types."""
    barcode  = product.get("barcode", "")
    per_unit = product.get("per_unit", "")
    pack_qty = product.get("pack_qty") or product.get("pack_size", "?")
    rrp      = product.get("rrp", "")
    brand    = product.get("brand", "")
    is_bogof = product.get("is_bogof", False)
    cost     = effective_price(product)

    fields = [
        {"name": "🏷️ Brand",            "value": brand or "-",                                    "inline": True},
        {"name": "📦 Pack Qty",          "value": f"{pack_qty} units" + (" 🎁 BOGOF" if is_bogof else ""), "inline": True},
        {"name": "🏷️ RRP (each)",        "value": f"£{rrp}" if rrp else "-",                      "inline": True},
        {"name": "🔢 Barcode / EAN",     "value": f"`{barcode}`" if barcode else "-",              "inline": True},
    ]

    ean_url   = sas_ean(barcode, cost)
    title_url = sas_title(product.get("title", ""), cost)
    if ean_url:
        fields.append({"name": "🔍 SAS EAN",   "value": f"[Search by barcode]({ean_url})",  "inline": True})
    fields.append(    {"name": "🔍 SAS Title", "value": f"[Search by title]({title_url})",  "inline": True})

    return fields


def _embed(title, url, colour, fields, product, footer_extra=""):
    embed = {
        "title":     title,
        "url":       url,
        "color":     colour,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": f"Wholesale Cosmetics Monitor • wholesale-cosmetics.co.uk{footer_extra}"},
    }
    image = product.get("image", "")
    if image:
        embed["thumbnail"] = {"url": image}
    return embed


def notify_new(product):
    pack_price = product.get("pack_price", "")
    reduced    = product.get("reduced_price", "")
    per_unit   = product.get("per_unit", "")
    price_display = f"~~£{pack_price}~~ → **£{reduced}**" if reduced else f"**£{pack_price}**"

    fields = [
        {"name": "💰 Pack Price (ex-VAT)",  "value": price_display,                                   "inline": True},
        {"name": "💷 Per Unit (ex-VAT)",    "value": f"£{per_unit}" if per_unit else "-",             "inline": True},
        {"name": "💷 Per Unit (inc-VAT)",   "value": f"£{vat(per_unit)}" if per_unit else "-",        "inline": True},
    ] + _base_fields(product)

    _send({"embeds": [_embed(
        f"🆕  NEW LISTING — {product['title']}",
        product["url"], COL_NEW, fields, product
    )]})
    print(f"  ✅ Discord: NEW — {product['title'][:60]}")


def notify_back_in_stock(product):
    per_unit = product.get("per_unit", "")
    fields = [
        {"name": "💰 Pack Price (ex-VAT)", "value": f"**£{effective_price(product)}**", "inline": True},
        {"name": "💷 Per Unit (ex-VAT)",   "value": f"£{per_unit}" if per_unit else "-","inline": True},
        {"name": "💷 Per Unit (inc-VAT)",  "value": f"£{vat(per_unit)}" if per_unit else "-", "inline": True},
    ] + _base_fields(product)

    _send({"embeds": [_embed(
        f"🟢  BACK IN STOCK — {product['title']}",
        product["url"], COL_BACK, fields, product
    )]})
    print(f"  ✅ Discord: BACK IN STOCK — {product['title'][:55]}")


def notify_price_drop(product, old_price, new_price, pct):
    per_unit = product.get("per_unit", "")
    diff     = abs(safe_float(old_price) - safe_float(new_price))
    pct_str  = f"{pct*100:.1f}%"
    colour   = COL_DROP20 if pct >= 0.20 else (COL_DROP10 if pct >= 0.10 else COL_DROP)
    icon     = "🔥" if pct >= 0.20 else ("💰" if pct >= 0.10 else "💵")

    fields = [
        {"name": "💰 Was",                "value": f"£{old_price}",                                 "inline": True},
        {"name": "💰 Now",                "value": f"**£{new_price}**",                             "inline": True},
        {"name": "📉 Drop",               "value": f"↓ £{diff:.2f} (**{pct_str}**)",               "inline": True},
        {"name": "💷 Per Unit (ex-VAT)",  "value": f"£{per_unit}" if per_unit else "-",             "inline": True},
        {"name": "💷 Per Unit (inc-VAT)", "value": f"£{vat(per_unit)}" if per_unit else "-",        "inline": True},
    ] + _base_fields(product)

    _send({"embeds": [_embed(
        f"{icon}  PRICE DROP -{pct_str} — {product['title']}",
        product["url"], colour, fields, product,
        footer_extra=f" • was £{old_price}"
    )]})
    print(f"  ✅ Discord: PRICE DROP -{pct_str} — {product['title'][:45]}")


# ---------------------------------------------------------------------------
# SNAPSHOT
# ---------------------------------------------------------------------------

def load_snapshot():
    if os.path.exists(SNAPSHOT_FILE):
        try:
            with open(SNAPSHOT_FILE) as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            bak = f"{SNAPSHOT_FILE}.bak.{int(time.time())}"
            print(f"  [!] Snapshot corrupted — backed up to {bak}")
            try:
                os.rename(SNAPSHOT_FILE, bak)
            except OSError:
                pass
    return {}


def save_snapshot(data):
    """Atomic write — crash won't corrupt the snapshot."""
    tmp = SNAPSHOT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SNAPSHOT_FILE)


def to_entry(product):
    return {
        "title":         product.get("title", ""),
        "url":           product.get("url", ""),
        "image":         product.get("image", ""),
        "barcode":       product.get("barcode", ""),
        "brand":         product.get("brand", ""),
        "pack_price":    product.get("pack_price", ""),
        "reduced_price": product.get("reduced_price", ""),
        "per_unit":      product.get("per_unit", ""),
        "is_bogof":      product.get("is_bogof", False),
        "pack_qty":      product.get("pack_qty", 1),
        "pack_size":     product.get("pack_size", ""),
        "rrp":           product.get("rrp", ""),
        "in_stock":      product.get("in_stock", True),
        "first_seen":    product.get("first_seen", datetime.now(timezone.utc).isoformat()),
        "last_updated":  datetime.now(timezone.utc).isoformat(),
    }

# ---------------------------------------------------------------------------
# MAIN CHECK
# ---------------------------------------------------------------------------

def run_check():
    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    print(f"\n[{now_str}] Checking Wholesale Cosmetics new arrivals...")

    snapshot      = load_snapshot()
    known_ids     = set(snapshot.keys())
    baseline_done = os.path.exists(BASELINE_FLAG)
    is_first_run  = not baseline_done

    # Step 1: Fetch all listing pages
    products = fetch_all_products()
    if not products:
        print("  [!] No products found — skipping this cycle")
        return

    current_ids = {p["id"] for p in products}
    new_ids     = current_ids - known_ids
    # Products that were known but not in current listing — went OOS
    gone_ids    = known_ids - current_ids

    print(f"  {len(products)} products | {len(new_ids)} new | {len(gone_ids)} gone from listing")

    if is_first_run:
        print(f"  First run — building baseline ({len(products)} products). No alerts.")

    alerts_sent = 0

    for product in products:
        pid    = product["id"]
        is_new = pid in new_ids
        old    = snapshot.get(pid, {})

        # Step 2: Scrape detail page ONLY for new products
        if is_new and not is_first_run:
            time.sleep(DETAIL_DELAY)
            product = fetch_product_detail(product)

        elif is_new and is_first_run:
            # On baseline, carry image/barcode if we already have it
            pass  # no detail scrape on first run — keep it fast

        # Carry forward cached fields for existing products
        if not is_new:
            for key in ("barcode", "brand", "image", "pack_size", "rrp"):
                if not product.get(key):
                    product[key] = old.get(key, "")

        product["in_stock"] = True  # it's on the listing, so in stock

        if is_first_run:
            entry = to_entry(product)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            snapshot[pid] = entry
            continue

        # --- ALERTS ---

        # New product
        if is_new:
            notify_new(product)
            alerts_sent += 1
            time.sleep(1.5)
            entry = to_entry(product)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            snapshot[pid] = entry
            continue

        # Back in stock (was marked OOS, now on listing)
        if not old.get("in_stock", True):
            notify_back_in_stock(product)
            alerts_sent += 1
            time.sleep(1.5)

        # Price drop (pack price or reduced price decreased)
        old_price = old.get("reduced_price") or old.get("pack_price") or ""
        new_price = product.get("reduced_price") or product.get("pack_price") or ""
        old_f     = safe_float(old_price)
        new_f     = safe_float(new_price)

        if old_f and new_f and old_f > 0:
            pct = (old_f - new_f) / old_f
            if pct > 0.01 and (old_f - new_f) > 0.02:
                notify_price_drop(product, old_price, new_price, pct)
                alerts_sent += 1
                time.sleep(1.5)

        # Update snapshot
        entry = to_entry(product)
        entry["first_seen"] = old.get("first_seen", entry["first_seen"])
        snapshot[pid] = entry

    # Mark gone products as OOS in snapshot (don't delete — needed for back-in-stock detection)
    for pid in gone_ids:
        if pid in snapshot:
            snapshot[pid]["in_stock"] = False
            snapshot[pid]["last_updated"] = datetime.now(timezone.utc).isoformat()

    save_snapshot(snapshot)

    if is_first_run:
        with open(BASELINE_FLAG, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
        print(f"  Baseline saved — {len(snapshot)} products tracked. Monitoring begins next cycle.")
    else:
        print(f"  Done — {alerts_sent} alert(s) | {len(snapshot)} products tracked.")

# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

def main():
    print("=" * 58)
    print("  Wholesale Cosmetics New Arrivals Monitor")
    print(f"  {LISTING_URL}")
    print(f"  Alerts: new listings | back in stock | price drops")
    print(f"  Interval: every {CHECK_INTERVAL}s ({CHECK_INTERVAL//60} min)")
    print("=" * 58)

    if not DISCORD_WEBHOOK:
        print("\n  ⚠️  DISCORD_WEBHOOK not set — alerts will be suppressed")

    if RUN_ONCE:
        run_check()
        return

    while True:
        try:
            run_check()
        except Exception as e:
            print(f"  [!] Unexpected error: {e}")
        print(f"  Sleeping {CHECK_INTERVAL}s...\n")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
