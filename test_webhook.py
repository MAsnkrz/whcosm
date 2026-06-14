"""
Test webhook for Wholesale Cosmetics Monitor.
Scrapes the first new arrival, checks stock via JS quantity cap trick, sends Discord embed.

How stock works: set qty input to 9999, fire change event, JS caps it to max in stock (packs).
Multiply packs by pack size = total units.

Usage:
    pip install playwright requests beautifulsoup4
    python -m playwright install chromium
    python test_webhook.py
"""

import os
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
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "https://discord.com/api/webhooks/1515746540453625906/NrYIHjN-RqfuqV-gAqL8WIL3roU1XxYJfzRz9vXGJ7uwuFEA7-zF4-MWyW2aBq89Vpl7")
# ---------------------------------------------------------------------------

BASE_URL   = "https://www.wholesale-cosmetics.co.uk"
NEW_URL    = f"{BASE_URL}/products/new/1/"
COLOUR_NEW = 0xE91E8C


def make_context(playwright):
    browser = playwright.chromium.launch(headless=True)
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


def fetch_html(context, url, wait_selector=None, timeout=20000):
    page = context.new_page()
    try:
        page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        if wait_selector:
            try:
                page.wait_for_selector(wait_selector, timeout=8000)
            except PWTimeout:
                pass
        return page.content()
    except Exception as e:
        print(f"  [!] Page load error ({url}): {e}")
        return None
    finally:
        page.close()


def get_stock_from_page(context, product_url, pack_size):
    """
    Navigate to product page, set qty=9999, trigger change event.
    The site's JS will cap the value to the actual packs in stock.
    Returns (packs_in_stock, units_in_stock).
    """
    page = context.new_page()
    try:
        page.goto(product_url, timeout=20000, wait_until="domcontentloaded")
        try:
            page.wait_for_selector('#quantity', timeout=8000)
        except PWTimeout:
            print("  [!] Quantity input not found")
            return None, None

        # Set qty to 9999 and fire change — JS will cap it to stock level
        page.evaluate("""
            const q = document.getElementById('quantity');
            if (q) {
                q.value = '9999';
                q.dispatchEvent(new Event('change', {bubbles: true}));
                q.dispatchEvent(new Event('blur',   {bubbles: true}));
                q.dispatchEvent(new Event('input',  {bubbles: true}));
            }
        """)
        time.sleep(0.5)  # let JS settle

        capped = page.evaluate("document.getElementById('quantity')?.value")

        if not capped or not str(capped).isdigit():
            print(f"  [!] Could not read capped qty (got: {capped})")
            return None, None

        packs = int(capped)
        ps    = int(pack_size) if pack_size and str(pack_size).isdigit() else 1
        units = packs * ps

        return packs, units

    except Exception as e:
        print(f"  [!] Stock check error: {e}")
        return None, None
    finally:
        page.close()


def vat_price(price_str):
    try:
        return f"{float(price_str) * 1.2:.2f}"
    except (ValueError, TypeError):
        return price_str


def selleramp_url(barcode, cost_price_str):
    if not barcode:
        return None
    vat = vat_price(cost_price_str)
    return f"https://sas.selleramp.com/sas/lookup/?search_term={barcode}&sas_cost_price={vat}"


def send_discord(product):
    title       = product.get("title", "Unknown Product")
    url         = product.get("url", BASE_URL)
    barcode     = product.get("barcode", "")
    sku         = product.get("sku", "")
    pack_size   = product.get("pack_size", "?")
    pack_price  = product.get("pack_price", "")
    reduced     = product.get("reduced_price", "")
    per_unit    = product.get("per_unit", "")
    rrp         = product.get("rrp", "")
    image       = product.get("image", "")
    packs_stock = product.get("packs_in_stock")
    units_stock = product.get("units_in_stock")

    cost    = reduced or pack_price or "0"
    sas_url = selleramp_url(barcode, per_unit or cost)

    if reduced:
        price_display = f"£{pack_price} -> **£{reduced}**"
    else:
        price_display = f"**£{pack_price}**" if pack_price else "-"

    fields = [
        {"name": "💰 Pack Price (ex. VAT)", "value": price_display,                                    "inline": True},
        {"name": "💷 Per Unit (ex. VAT)",   "value": f"£{per_unit}" if per_unit else "-",              "inline": True},
        {"name": "💷 Per Unit (inc. VAT)",  "value": f"£{vat_price(per_unit)}" if per_unit else "-",   "inline": True},
        {"name": "📦 Pack Size",            "value": f"{pack_size} units" if pack_size else "-",        "inline": True},
        {"name": "🏷️ RRP (each)",           "value": f"£{rrp}" if rrp else "-",                        "inline": True},
        {"name": "🔢 Barcode / EAN",        "value": f"`{barcode}`" if barcode else "-",               "inline": True},
        {"name": "📊 Stock (Packs)",        "value": f"**{packs_stock}** packs" if packs_stock is not None else "-", "inline": True},
        {"name": "📊 Stock (Units)",        "value": f"**{units_stock}** units" if units_stock is not None else "-", "inline": True},
    ]

    if sku:
        fields.append({"name": "🔖 SKU", "value": f"`{sku}`", "inline": True})

    if sas_url:
        fields.append({
            "name":   "🔍 SellerAmp SAS",
            "value":  f"[Open in SellerAmp]({sas_url})",
            "inline": False,
        })

    embed = {
        "title":     f"🧪 TEST — {title}",
        "url":       url,
        "color":     COLOUR_NEW,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Wholesale Cosmetics Monitor — TEST NOTIFICATION"},
    }

    if image:
        embed["thumbnail"] = {"url": quote(image, safe=":/?=&")}

    r = requests.post(DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=10)
    if not r.ok:
        print(f"  Discord error {r.status_code}: {r.text}")
    r.raise_for_status()


def main():
    print("Wholesale Cosmetics — Discord Webhook Test")
    print("=" * 45)

    with sync_playwright() as pw:
        browser, context = make_context(pw)
        try:
            # Step 1: grab first product from listing
            print("Fetching new arrivals listing...")
            html = fetch_html(context, NEW_URL, wait_selector="a[href*='/product/']")
            soup = BeautifulSoup(html, "html.parser")

            target = None
            seen = set()
            for a in soup.find_all("a", href=re.compile(r"/product/[^/]+/\d+/")):
                href = a["href"]
                m = re.search(r"/product/([^/]+)/(\d+)/", href)
                if not m or m.group(2) in seen:
                    continue
                seen.add(m.group(2))
                slug, pid = m.group(1), m.group(2)
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

                target = {
                    "id": pid, "slug": slug, "title": title,
                    "pack_price": pack_price, "reduced_price": reduced_price,
                    "per_unit": per_unit,
                    "url": BASE_URL + href if href.startswith("/") else href,
                }
                break

            if not target:
                print("ERROR: No products found on listing page.")
                return

            print(f"Found: [{target['id']}] {target['title'] or target['slug']}")

            # Step 2: fetch detail page
            print("Fetching product detail page...")
            time.sleep(1)
            dhtml = fetch_html(context, target["url"], wait_selector=".barcodetext")
            dsoup = BeautifulSoup(dhtml, "html.parser")
            dtext = dsoup.get_text(" ", strip=True)

            barcode_el = dsoup.find(class_="barcodetext")
            target["barcode"] = barcode_el.get_text(strip=True) if barcode_el else ""

            sku_m = re.search(r"\b([A-Z0-9]{5,20})\b\s+\d{13}", dtext)
            target["sku"] = sku_m.group(1) if sku_m else ""

            ps = re.search(r"Pack Size:\s*(\d+)\s*units?", dtext)
            target["pack_size"] = ps.group(1) if ps else ""

            rrp = re.search(r"RRP\s*£?([\d.]+)", dtext)
            target["rrp"] = rrp.group(1) if rrp else ""

            if not target["title"]:
                h1 = dsoup.find("h1")
                target["title"] = h1.get_text(strip=True) if h1 else target["slug"].replace("-", " ").title()

            pp = re.search(r"Price:\s*£([\d.]+)", dtext)
            if pp: target["pack_price"] = pp.group(1)
            rp = re.search(r"Reduced:\s*£([\d.]+)", dtext)
            if rp: target["reduced_price"] = rp.group(1)
            pu = re.search(r"\(£([\d.]+)\s*per unit\)", dtext)
            if pu: target["per_unit"] = pu.group(1)

            img_tag = dsoup.find("img", src=re.compile(r"/images/C[\s(]", re.IGNORECASE))
            target["image"] = (BASE_URL + img_tag["src"]) if img_tag else ""

            # Step 3: get stock via JS qty cap trick (no login needed)
            print("Checking stock via quantity cap...")
            packs, units = get_stock_from_page(context, target["url"], target.get("pack_size", "1"))
            target["packs_in_stock"] = packs
            target["units_in_stock"] = units

        finally:
            browser.close()

    # Step 4: print & send
    print(f"\nProduct details:")
    print(f"  Title:       {target['title']}")
    print(f"  Barcode:     {target['barcode']}")
    print(f"  Pack price:  £{target['pack_price']}  Per unit: £{target['per_unit']}")
    print(f"  Pack size:   {target['pack_size']} units")
    print(f"  RRP:         £{target['rrp']}")
    print(f"  Stock:       {target['packs_in_stock']} packs / {target['units_in_stock']} units")
    print(f"  Image:       {target['image']}")
    print(f"\nSending test Discord notification...")
    send_discord(target)
    print("✅ Webhook sent! Check your Discord channel.")


if __name__ == "__main__":
    main()