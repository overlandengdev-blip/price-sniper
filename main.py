import asyncio
import os
import random
import re
import time
import json
import logging
import requests
from playwright.async_api import async_playwright
from supabase import create_client, Client
from bs4 import BeautifulSoup
from datetime import datetime

# --- CONFIGURATION ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

CONCURRENCY = 3 

# Logging
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

if not all([SUPABASE_URL, SUPABASE_KEY, GEMINI_API_KEY]):
    logger.error("❌ Critical: Missing Secrets.")
    exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
]

BANNED_PHRASES = [
    "login", "password", "cart", "checkout", "loading", "rights reserved", 
    "privacy policy", "terms", "newsletter", "shipping", "click here"
]

# --- 1. INTELLIGENT PARSERS ---

def clean_text(text):
    if not text: return None
    return re.sub(r'\s+', ' ', text).strip()

def validate_description(text, title):
    if not text: return None
    clean = clean_text(text)
    low = clean.lower()
    
    if len(clean) < 50: return None
    if any(b in low for b in BANNED_PHRASES): return None
    # Reject if it's just the title repeated
    if title and title.lower() in low and len(clean) < len(title) + 20: return None
    
    return clean

def extract_json_ld(soup):
    """Extracts Structured Data (The Gold Standard)."""
    data = {}
    scripts = soup.find_all('script', type='application/ld+json')
    for script in scripts:
        try:
            content = script.string
            if not content: continue
            js = json.loads(content)
            items = js if isinstance(js, list) else [js]
            
            for item in items:
                # Normalize type
                itype = item.get('@type')
                if isinstance(itype, list): itype = itype[0]
                
                if itype in ['Product', 'ItemPage']:
                    if 'name' in item: data['name'] = item['name']
                    if 'description' in item: data['description'] = item['description']
                    if 'image' in item:
                        img = item['image']
                        data['image_url'] = img[0] if isinstance(img, list) else img
                    
                    # Price Logic
                    offers = item.get('offers')
                    if offers:
                        offer_list = offers if isinstance(offers, list) else [offers]
                        for o in offer_list:
                            # Prefer 'lowPrice' (Sale) or standard 'price'
                            p = o.get('lowPrice') or o.get('price')
                            if p: 
                                data['price'] = float(p)
                                break 
        except: continue
    return data

# --- 2. PRICE COURT (The Fixing Engine) ---

def get_price_verdict(soup, json_price, body_text):
    candidates = []

    # Source 1: JSON-LD (High Trust: 10)
    if json_price:
        candidates.append({"src": "JSON", "val": json_price, "trust": 10})

    # Source 2: Meta Tags (Medium Trust: 8)
    # These are usually correct ($369)
    metas = [
        soup.find("meta", property="og:price:amount"),
        soup.find("meta", property="product:price:amount"),
        soup.find("meta", itemprop="price")
    ]
    for m in metas:
        if m:
            try:
                val = float(m.get("content"))
                candidates.append({"src": "Meta", "val": val, "trust": 8})
            except: pass

    # Source 3: Visual Price (Medium Trust: 6)
    # Looks for simple distinct price strings like "$369.00"
    # Helps confirm the Meta tag
    visual_matches = re.findall(r'\$\s?([0-9,]+\.[0-9]{2})', body_text)
    for m in visual_matches:
        try:
            v = float(m.replace(',', ''))
            if 15 < v < 10000:
                candidates.append({"src": "Visual", "val": v, "trust": 6})
        except: pass

    # Source 4: Regex Max (Low Trust: 2)
    # This is the "Fallback of Last Resort". We demote its trust so it can't beat Meta.
    matches = re.findall(r'\$\s?([0-9,]+\.?[0-9]*)', body_text)
    regex_prices = []
    for m in matches:
        try:
            v = float(m.replace(',', ''))
            if 15 < v < 50000: regex_prices.append(v)
        except: pass
    
    if regex_prices:
        # We add MAX, but with LOW TRUST (2).
        # This prevents "Win $1000" (Trust 2) from beating "Price $369" (Trust 8)
        candidates.append({"src": "RegexMax", "val": max(regex_prices), "trust": 2})

    # --- THE VERDICT ---
    if not candidates: return None

    # Group by value to find consensus
    # If "$369" appears in Meta AND Visual, it gets a score boost
    votes = {}
    for c in candidates:
        val = c['val']
        votes[val] = votes.get(val, 0) + c['trust']

    # Pick the price with the highest score
    winner_price = max(votes, key=votes.get)
    
    # Log the court proceedings
    log_str = " | ".join([f"{c['src']}: ${c['val']}" for c in candidates])
    logger.info(f"   ⚖️ Price Court: {log_str} -> WINNER: ${winner_price}")
    
    return winner_price

# --- 3. DESCRIPTION HUNTER ---

def get_best_description(soup, title, json_desc):
    
    # 1. JSON-LD (Best)
    if validate_description(json_desc, title):
        return json_desc

    # 2. Meta Tag
    meta = soup.find("meta", property="og:description")
    if meta:
        val = validate_description(meta.get("content"), title)
        if val: return val

    # 3. Smart Selectors
    selectors = [
        ".product-description", "#product-description", 
        ".description", "#description",
        ".product-details", ".tab-content", 
        "div[itemprop='description']"
    ]
    
    for sel in selectors:
        elem = soup.select_one(sel)
        if elem:
            val = validate_description(elem.get_text(" ", strip=True), title)
            if val: return val

    # 4. Paragraph Fallback
    best_p = ""
    for p in soup.find_all('p'):
        text = p.get_text().strip()
        if len(text) > len(best_p):
            if validate_description(text, title):
                best_p = text
                
    return best_p if best_p else "Description unavailable."

# --- 4. CORE WORKER ---

async def process_product(sem, browser, row):
    async with sem:
        url = row['url']
        pid = row['product_id']
        source_id = row['id']
        logger.info(f"🔎 Checking: {url}")

        # --- AUTO-LINKER ---
        if pid is None or pid == "None":
            try:
                new_prod = supabase.table("products").insert({
                    "name": "Scanning...",
                    "is_approved": False,
                    "category": "Uncategorized" 
                }).execute()
                pid = new_prod.data[0]['id']
                supabase.table("product_sources").update({"product_id": pid}).eq("id", source_id).execute()
            except Exception as e:
                logger.error(f"   ❌ DB Fix Failed: {e}")
                return

        try:
            page = await browser.new_page(user_agent=random.choice(USER_AGENTS))
            await page.goto(url, timeout=60000, wait_until="domcontentloaded")
            await asyncio.sleep(4) 

            # Expanders
            for key in ["spec", "dimen", "desc", "more"]:
                try: await page.locator(f"text=/{key}/i").first.click(timeout=500)
                except: pass

            html = await page.content()
            body_text = await page.inner_text("body")
            soup = BeautifulSoup(html, 'html.parser')

            # --- EXTRACTION ---
            
            json_data = extract_json_ld(soup)
            
            name = json_data.get('name')
            if not name:
                t = soup.find("meta", property="og:title")
                name = t.get("content") if t else soup.title.string

            desc = get_best_description(soup, name, json_data.get('description'))

            img_url = json_data.get('image_url')
            if not img_url:
                i = soup.find("meta", property="og:image")
                img_url = i.get("content") if i else None

            # --- PRICE VERDICT ---
            price = get_price_verdict(soup, json_data.get('price'), body_text)

            # --- SAVE ---
            update_data = {
                "name": (name or "Unknown")[:255],
                "description": desc,
                "updated_at": datetime.now().isoformat()
            }
            if img_url: update_data['image_url'] = img_url
            if price: update_data['price'] = price

            supabase.table("products").update(update_data).eq("id", pid).execute()

            if price:
                supabase.table("product_sources").update({
                    "last_price": price, "last_checked": "now()"
                }).eq("id", source_id).execute()
                supabase.table("price_history").insert({
                    "product_id": pid, "price": price
                }).execute()
            else:
                logger.warning("   ⚠️ No valid price found.")

            await page.close()

        except Exception as e:
            logger.error(f"   ⚠️ Scrape Error: {e}")

async def main():
    logger.info("📡 Fetching patrol list...")
    response = supabase.table("product_sources").select("*, products(*)").execute()
    sources = response.data
    
    if not sources: return

    sem = asyncio.Semaphore(CONCURRENCY)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        tasks = [process_product(sem, browser, row) for row in sources]
        await asyncio.gather(*tasks)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
