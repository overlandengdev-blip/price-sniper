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

# --- STRATEGY 1: META TAGS (The most accurate) ---
def get_meta_price(html):
    soup = BeautifulSoup(html, 'html.parser')
    
    # List of tags where stores hide the real price
    candidates = [
        soup.find("meta", property="og:price:amount"),
        soup.find("meta", property="product:price:amount"),
        soup.find("meta", itemprop="price"),
        soup.find("span", itemprop="price"),
        soup.find("meta", {"name": "twitter:data1"})
    ]
    
    for tag in candidates:
        if tag:
            val = tag.get("content") or tag.get_text()
            if val:
                # Remove currency symbols and text
                clean = re.sub(r'[^0-9.]', '', val)
                try:
                    price = float(clean)
                    if price > 5: return price
                except: continue
    return None

# --- STRATEGY 2: REGEX MAX (The Brute Force) ---
def get_regex_price(text):
    # Find all patterns looking like money: $1,200.00 or 1200.00
    # We avoid small numbers to skip "4 interest free payments of $20"
    matches = re.findall(r'\$\s?([0-9,]+\.?[0-9]*)', text)
    valid_prices = []
    
    for m in matches:
        try:
            val = float(m.replace(',', ''))
            # Filter: 4x4 parts are rarely under $15 or over $50k
            if 15 < val < 50000: 
                valid_prices.append(val)
        except: pass
        
    if valid_prices:
        # Return the MAX price found (Safest for 4x4 parts to avoid accessory prices)
        return max(valid_prices)
    return None

# --- STRATEGY 3: AI (The Helper) ---
def call_gemini(text):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{
            "parts": [{
                "text": f"Extract JSON with 'description' (summary) and 'specs' (object). Text: {text[:10000]}"
            }]
        }]
    }
    try:
        resp = requests.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=20)
        if resp.status_code == 200:
            raw = resp.json()['candidates'][0]['content']['parts'][0]['text']
            return json.loads(raw.replace('```json', '').replace('```', '').strip())
    except:
        return None
    return None

# --- MAIN PROCESSOR ---
async def process_product(browser, row):
    url = row['url']
    pid = row['product_id']
    source_id = row['id']
    
    print(f"🔎 Processing: {url}...")

    # 1. AUTO-LINKER (Wrap in Try/Except to prevent DB crashes)
    if pid is None or pid == "None":
        print("   🛠️ Orphan link found. Attempting to create Product...")
        try:
            new_prod = supabase.table("products").insert({
                "name": "Scanning Product...", 
                "is_approved": False,
                # Category removed/optional now
            }).execute()
            pid = new_prod.data[0]['id']
            # Link back
            supabase.table("product_sources").update({"product_id": pid}).eq("id", source_id).execute()
            print(f"   ✅ Fixed! Linked to ID: {pid}")
        except Exception as e:
            print(f"   ❌ DB Error: {e}. Skipping this item.")
            return # Skip, do not crash

    # 2. SCRAPE
    try:
        page = await browser.new_page(user_agent=random.choice(USER_AGENTS))
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await asyncio.sleep(5) # Wait for dynamic prices
        
        html = await page.content()
        body_text = await page.inner_text("body")
        
        # 3. GET DATA (The Multi-Layer Strategy)
        
        # A. Try Meta Tags (Fast & Accurate)
        price = get_meta_price(html)
        method = "Meta Tag"
        
        # B. Try Regex Max (Fallback)
        if not price:
            price = get_regex_price(body_text)
            method = "Regex (Max)"
            
        # C. Get Description via AI (Optional)
        ai_data = call_gemini(body_text)
        
        # 4. SAVE
        if price:
            print(f"   💰 Price Found: ${price} (via {method})")
            
            # Prepare update
            prod_update = {"price": price, "updated_at": "now()"}
            
            # If AI worked, add description
            if ai_data:
                prod_update['description'] = ai_data.get('description')
                prod_update['specs'] = ai_data.get('specs')
                
            # Try to grab image
            try:
                img = await page.locator('meta[property="og:image"]').get_attribute('content')
                if img: prod_update['image_url'] = img
            except: pass

            # Execute Updates
            supabase.table("products").update(prod_update).eq("id", pid).execute()
            supabase.table("product_sources").update({
                "last_price": price, 
                "last_checked": "now()"
            }).eq("id", source_id).execute()
            supabase.table("price_history").insert({
                "product_id": pid, 
                "price": price
            }).execute()
            
        else:
            print("   ⚠️ No price found by any method.")

        await page.close()

    except Exception as e:
        print(f"   ⚠️ Scrape Error: {e}")

async def main():
    print("🚀 Starting Patrol...")
    response = supabase.table("product_sources").select("*, products(*)").execute()
    sources = response.data
    
    if not sources:
        print("💤 No products to track.")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for row in sources:
            await process_product(browser, row)
            time.sleep(2) 
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
