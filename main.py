import asyncio
import os
import random
import re
import time
import json
import requests
from playwright.async_api import async_playwright
from supabase import create_client, Client
from bs4 import BeautifulSoup

# --- CONFIGURATION ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not all([SUPABASE_URL, SUPABASE_KEY, GEMINI_API_KEY]):
    print("❌ Critical Error: Missing Secrets. Check GitHub Settings.")
    exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
]

# --- THE TRASH FILTER ---
# If a description contains ANY of these, we reject it.
BANNED_PHRASES = [
    "login", "sign in", "create account", "password", 
    "shopping cart", "checkout", "empty", "loading",
    "enable cookies", "javascript", "browser",
    "contact us", "privacy policy", "terms", "returns",
    "subscribe", "newsletter", "welcome to", "best prices",
    "fast shipping", "australia wide", "click here"
]

def validate_description(text, title):
    """Returns the text if it's good, or None if it's trash."""
    if not text: return None
    
    clean = text.strip()
    low = clean.lower()
    
    # Rule 1: Too short? (Garbage like "Home")
    if len(clean) < 30: 
        return None
        
    # Rule 2: Contains banned generic words?
    if any(b in low for b in BANNED_PHRASES):
        return None
        
    # Rule 3: Is it just the Title repeated?
    if title and title.lower() in low and len(clean) < len(title) + 10:
        return None # It's just "Product X" repeated
        
    return clean

# --- 1. STATIC SCRAPER ---
def get_static_details(html):
    soup = BeautifulSoup(html, 'html.parser')
    data = {}

    # A. TITLE
    title = soup.find("meta", property="og:title")
    if not title: title = soup.find("meta", {"name": "twitter:title"})
    
    title_text = ""
    if title: 
        title_text = title.get("content").strip()
    else:
        h1 = soup.find("h1")
        if h1: title_text = h1.get_text().strip()
        else: 
            t = soup.find("title")
            if t: title_text = t.get_text().strip()
    
    if title_text: data['name'] = title_text

    # B. DESCRIPTION (With Filtering)
    desc = soup.find("meta", property="og:description")
    if not desc: desc = soup.find("meta", {"name": "description"})
    
    if desc:
        raw_desc = desc.get("content")
        valid_desc = validate_description(raw_desc, title_text)
        if valid_desc:
            data['description'] = valid_desc
        else:
            print(f"   🗑️ Filtered out generic description: '{raw_desc[:30]}...'")

    # C. IMAGE
    img = soup.find("meta", property="og:image")
    if img:
        data['image_url'] = img.get("content").strip()

    return data

def get_meta_price(html):
    soup = BeautifulSoup(html, 'html.parser')
    candidates = [
        soup.find("meta", property="og:price:amount"),
        soup.find("meta", property="product:price:amount"),
        soup.find("meta", itemprop="price"),
        soup.find("span", itemprop="price")
    ]
    for tag in candidates:
        if tag:
            val = tag.get("content") or tag.get_text()
            if val:
                clean = re.sub(r'[^0-9.]', '', val)
                try:
                    p = float(clean)
                    if p > 5: return p
                except: continue
    return None

def get_regex_price(text):
    matches = re.findall(r'\$\s?([0-9,]+\.?[0-9]*)', text)
    prices = []
    for m in matches:
        try:
            val = float(m.replace(',', ''))
            if 15 < val < 50000: prices.append(val)
        except: pass
    if prices: return max(prices)
    return None

# --- 2. AI ENHANCER (Optional) ---
def call_gemini(text):
    # Only runs if quota is available
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{
            "parts": [{
                "text": f"Extract raw JSON with 'description' (summary) and 'specs' (object). Text: {text[:10000]}"
            }]
        }]
    }
    try:
        resp = requests.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=20)
        if resp.status_code == 200:
            raw = resp.json()['candidates'][0]['content']['parts'][0]['text']
            return json.loads(raw.replace('```json', '').replace('```', '').strip())
        elif resp.status_code == 429:
            print("   ⏳ AI Quota Exceeded. Skipping AI enhancement.")
    except: pass
    return None

# --- MAIN PROCESSOR ---
async def process_product(browser, row):
    url = row['url']
    pid = row['product_id']
    source_id = row['id']
    
    print(f"🔎 Processing: {url}...")

    # 1. AUTO-LINKER
    if pid is None or pid == "None":
        print("   🛠️ Orphan link. Creating placeholder...")
        try:
            new_prod = supabase.table("products").insert({
                "name": "Scanning...", 
                "is_approved": False
            }).execute()
            pid = new_prod.data[0]['id']
            supabase.table("product_sources").update({"product_id": pid}).eq("id", source_id).execute()
        except Exception as e:
            print(f"   ❌ DB Error: {e}. Skipping.")
            return

    try:
        page = await browser.new_page(user_agent=random.choice(USER_AGENTS))
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await asyncio.sleep(5)
        
        html = await page.content()
        body_text = await page.inner_text("body")
        
        # 2. GATHER DATA (Static)
        data = get_static_details(html)
        
        # 3. GET PRICE
        price = get_meta_price(html)
        if not price: price = get_regex_price(body_text)
        
        if price: 
            data['price'] = price
            print(f"   💰 Price: ${price}")
        else:
            print("   ⚠️ No price found.")

        # 4. TRY AI (Only if Description is missing or filtered out)
        if 'description' not in data:
            print("   🧠 Missing description. Asking AI...")
            ai_data = call_gemini(body_text)
            if ai_data:
                if 'description' in ai_data: data['description'] = ai_data['description']
                if 'specs' in ai_data: data['specs'] = ai_data['specs']

        # 5. SAVE TO DB
        data['updated_at'] = "now()"
        
        supabase.table("products").update(data).eq("id", pid).execute()
        
        if price:
            supabase.table("product_sources").update({
                "last_price": price, 
                "last_checked": "now()"
            }).eq("id", source_id).execute()
            
            supabase.table("price_history").insert({
                "product_id": pid, 
                "price": price
            }).execute()

        await page.close()

    except Exception as e:
        print(f"   ⚠️ Scrape Error: {e}")

async def main():
    print("🚀 Starting Patrol...")
    response = supabase.table("product_sources").select("*, products(*)").execute()
    sources = response.data
    
    if not sources:
        print("💤 No products.")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for row in sources:
            await process_product(browser, row)
            time.sleep(2) 
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
