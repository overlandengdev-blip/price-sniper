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

# Concurrency Limit (Safe for GitHub Actions)
CONCURRENCY = 3 

# Logging Setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

if not all([SUPABASE_URL, SUPABASE_KEY, GEMINI_API_KEY]):
    logger.error("❌ Critical Error: Missing Secrets.")
    exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
]

BANNED_PHRASES = [
    "login", "sign in", "password", "cart", "checkout", "loading", 
    "cookie", "javascript", "rights reserved", "privacy policy", 
    "terms", "newsletter", "shipping", "click here", "read more"
]

# --- GLOBAL STATE ---
# If AI fails too many times, we flip this switch to stop asking
AI_CIRCUIT_BROKEN = False
AI_FAILURE_COUNT = 0

# --- 1. INTELLIGENT CONTENT PARSERS ---

def clean_text(text):
    if not text: return None
    # Remove extra whitespace and newlines
    return re.sub(r'\s+', ' ', text).strip()

def validate_description(text, title):
    """Filters out garbage descriptions."""
    if not text: return None
    clean = clean_text(text)
    low = clean.lower()
    
    if len(clean) < 40: return None
    if any(b in low for b in BANNED_PHRASES): return None
    # If description is just the title repeated
    if title and title.lower() in low and len(clean) < len(title) + 25: return None
    
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
                # Look for Product Schema
                if item.get('@type') in ['Product', 'ItemPage']:
                    if 'name' in item: data['name'] = item['name']
                    if 'description' in item: data['description'] = item['description']
                    if 'image' in item:
                        img = item['image']
                        data['image_url'] = img[0] if isinstance(img, list) else img
                    
                    # Grab Price
                    offers = item.get('offers')
                    if offers:
                        offer = offers[0] if isinstance(offers, list) else offers
                        if 'price' in offer: data['price'] = float(offer['price'])
                        if 'priceCurrency' in offer: data['currency'] = offer['priceCurrency']
        except: continue
    return data

def extract_specs_regex(text):
    """Extracts specs using patterns when AI is dead."""
    specs = {}
    clean = text.lower()
    
    # Weight (e.g. 25kg)
    w = re.search(r'(\d+(\.\d+)?\s?(kg|lbs))', clean)
    if w: specs['weight'] = w.group(1).replace(' ', '')
    
    # Dimensions (e.g. 100x200mm)
    d = re.search(r'(\d+\s?[xX]\s?\d+(\s?[xX]\s?\d+)?\s?(mm|cm|m|in))', clean)
    if d: specs['dimensions'] = d.group(1).replace(' ', '')
    
    # Compatibility
    vehicles = []
    makes = ["toyota", "ford", "nissan", "mitsubishi", "isuzu", "mazda", "jeep", "ram"]
    for m in makes:
        if m in clean:
            # Match "Toyota ... 2015" or "2015 ... Toyota"
            pat = re.search(fr'({m}.*?20\d\d[-+]?)|(20\d\d[-+]?.*?{m})', clean)
            if pat: vehicles.append(pat.group(0).strip().title())
    
    if vehicles: specs['compatibility'] = ", ".join(list(set(vehicles))[:5])
    return specs

# --- 2. AI HANDLER (With Circuit Breaker) ---

def call_ai_enhancer(text):
    global AI_CIRCUIT_BROKEN, AI_FAILURE_COUNT
    
    if AI_CIRCUIT_BROKEN:
        return None

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": f"Extract raw JSON with 'description' (summary) and 'specs' (object). Text: {text[:12000]}"}]}]
    }
    
    try:
        resp = requests.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=15)
        if resp.status_code == 200:
            raw = resp.json()['candidates'][0]['content']['parts'][0]['text']
            return json.loads(raw.replace('```json', '').replace('```', '').strip())
        elif resp.status_code == 429:
            logger.warning("   ⏳ AI Rate Limit hit. Breaking Circuit.")
            AI_CIRCUIT_BROKEN = True
        else:
            AI_FAILURE_COUNT += 1
            if AI_FAILURE_COUNT > 3: AI_CIRCUIT_BROKEN = True
    except:
        AI_FAILURE_COUNT += 1
        
    return None

# --- 3. CORE WORKER ---

async def process_product(sem, browser, row):
    async with sem: # Limits concurrency to 3
        url = row['url']
        pid = row['product_id']
        source_id = row['id']
        logger.info(f"🔎 Processing: {url}")

        # --- AUTO-LINKER (CRASH FIX) ---
        if pid is None or pid == "None":
            logger.info("   🛠️ Fixing orphan link...")
            try:
                # FIX: We now provide 'category' to satisfy the DB constraint
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
            context = await browser.new_context(user_agent=random.choice(USER_AGENTS))
            page = await context.new_page()
            
            # Anti-detection & Timeout Handling
            try:
                await page.goto(url, timeout=45000, wait_until="domcontentloaded")
                await asyncio.sleep(2) # Allow JS rendering
            except:
                logger.warning(f"   ⚠️ Timeout loading {url}, trying to scrape what loaded...")

            # Click "Expand" buttons
            for key in ["spec", "dimen", "desc", "more", "detail"]:
                loc = page.locator(f"text=/{key}/i")
                if await loc.count() > 0:
                    try: await loc.first.click(timeout=500)
                    except: pass

            html = await page.content()
            body_text = await page.inner_text("body")
            soup = BeautifulSoup(html, 'html.parser')

            # --- DATA EXTRACTION STRATEGY ---
            
            # 1. JSON-LD (Best)
            data = extract_json_ld(soup)
            
            # 2. Meta Tags (Fallback for Title/Desc/Image)
            if not data.get('name'):
                t = soup.find("meta", property="og:title")
                data['name'] = t.get("content") if t else soup.title.string
            
            if not data.get('image_url'):
                i = soup.find("meta", property="og:image")
                if i: data['image_url'] = i.get("content")

            # 3. Description Smart-Fill
            current_desc = data.get('description')
            if not validate_description(current_desc, data.get('name')):
                # Try finding standard containers
                for cls in ["product-description", "description", "details", "tab-content"]:
                    div = soup.find("div", class_=re.compile(cls, re.I))
                    if div:
                        txt = clean_text(div.get_text())
                        if validate_description(txt, data.get('name')):
                            data['description'] = txt
                            break
            
            # 4. Price Hunter (Meta > Regex)
            price = data.get('price')
            if not price:
                # Meta tags
                meta_price = soup.find("meta", property="og:price:amount")
                if meta_price: 
                    try: price = float(meta_price.get("content"))
                    except: pass
            
            if not price:
                # Regex Max Strategy
                matches = re.findall(r'\$\s?([0-9,]+\.?[0-9]*)', body_text)
                valid_prices = []
                for m in matches:
                    try:
                        v = float(m.replace(',', ''))
                        if 15 < v < 50000: valid_prices.append(v)
                    except: pass
                if valid_prices: price = max(valid_prices)

            # 5. Specs & AI (Luxury Layer)
            specs = extract_specs_regex(body_text)
            
            # Only ask AI if we are missing critical data AND circuit is closed
            if (not data.get('description') or not specs) and not AI_CIRCUIT_BROKEN:
                logger.info("   🧠 Asking AI helper...")
                ai_data = call_ai_enhancer(body_text)
                if ai_data:
                    if not data.get('description'): data['description'] = ai_data.get('description')
                    if ai_data.get('specs'): specs.update(ai_data['specs'])

            # --- SAVE TO DB ---
            update_data = {
                "name": (data.get('name') or "Unknown Product")[:255],
                "description": data.get('description'),
                "specs": specs,
                "updated_at": datetime.now().isoformat()
            }
            if data.get('image_url'): update_data['image_url'] = data['image_url']
            if price: update_data['price'] = price

            # Update Product
            supabase.table("products").update(update_data).eq("id", pid).execute()

            # Update Price History
            if price:
                logger.info(f"   💰 Price Saved: ${price}")
                supabase.table("product_sources").update({
                    "last_price": price, "last_checked": "now()"
                }).eq("id", source_id).execute()
                supabase.table("price_history").insert({
                    "product_id": pid, "price": price
                }).execute()

            await page.close()
            await context.close()

        except Exception as e:
            logger.error(f"   ⚠️ Scrape Error on {url}: {e}")

async def main():
    logger.info("📡 Fetching patrol list...")
    response = supabase.table("product_sources").select("*, products(*)").execute()
    sources = response.data
    
    if not sources:
        logger.info("💤 No products.")
        return

    # Semaphore controls concurrency (3 tabs at once)
    sem = asyncio.Semaphore(CONCURRENCY)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        
        # Create all tasks
        tasks = [process_product(sem, browser, row) for row in sources]
        
        # Run them all in parallel
        await asyncio.gather(*tasks)
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
