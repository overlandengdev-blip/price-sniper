import asyncio
import os
import random
import re
import time
import json
import requests
from playwright.async_api import async_playwright
from supabase import create_client, Client

# --- CONFIGURATION ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not all([SUPABASE_URL, SUPABASE_KEY, GEMINI_API_KEY]):
    print("❌ Error: Missing API Keys in Secrets.")
    exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
]

# --- 1. AI HANDLER (With Rate Limit Protection) ---

def call_gemini(payload):
    # Try the most stable model first
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    headers = {'Content-Type': 'application/json'}
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 429:
            print("   ⏳ Quote Exceeded (429). Skipping AI...")
            return None # Force fallback
        else:
            print(f"   ⚠️ AI Error {response.status_code}")
            return None
    except:
        return None

def get_analysis_payload(text):
    return {
        "contents": [{
            "parts": [{
                "text": (
                    f"Return raw JSON with: 'price' (number), 'description' (string), 'specs' (object). "
                    f"Text: {text[:12000]}"
                )
            }]
        }]
    }

# --- 2. FALLBACK HANDLERS (The "Smart" Dumb Mode) ---

async def get_meta_price(page):
    """Looks for high-accuracy meta tags used by Facebook/Google Shopping."""
    selectors = [
        'meta[property="og:price:amount"]',
        'meta[property="product:price:amount"]',
        'meta[name="twitter:data1"]',
        'span[itemprop="price"]'
    ]
    
    for sel in selectors:
        try:
            # Try attribute 'content'
            val = await page.locator(sel).first.get_attribute('content')
            if not val: # Try inner text
                val = await page.locator(sel).first.inner_text()
            
            if val:
                # Clean clean clean
                clean = re.sub(r'[^0-9.]', '', val)
                price = float(clean)
                if price > 5: return price
        except:
            continue
    return None

def regex_fallback(text):
    """Last resort: Find the biggest number that looks like a price."""
    matches = re.findall(r'\$\s?([0-9,]+\.?[0-9]*)', text)
    prices = []
    for m in matches:
        try:
            val = float(m.replace(',', ''))
            if 10 < val < 50000: # Ignore accessories <$10 and crazy errors
                prices.append(val)
        except: pass
    
    if prices:
        # Use MAX because 4x4 parts are expensive. 
        # MIN finds weekly payments or accessories.
        return max(prices) 
    return 0.0

# --- 3. PROCESSOR ---

async def process_product(browser, row):
    url = row['url']
    pid = row['product_id']
    source_id = row['id']
    
    print(f"🔎 Checking {url}...")

    # --- AUTO-LINKER: Fix Broken Database Rows ---
    if pid is None or pid == "None":
        print("   🛠️ Found Orphan Link! Creating Product...")
        new_prod = supabase.table("products").insert({
            "name": "New Scanned Item",
            "is_approved": False
        }).execute()
        pid = new_prod.data[0]['id']
        # Link it back
        supabase.table("product_sources").update({"product_id": pid}).eq("id", source_id).execute()
        print(f"   ✅ Linked to new Product ID: {pid}")

    try:
        page = await browser.new_page(user_agent=random.choice(USER_AGENTS))
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await asyncio.sleep(5) # Let JS load
        
        # 1. Grab Image (Always try this)
        img_url = None
        try:
            img_url = await page.locator('meta[property="og:image"]').get_attribute('content')
        except: pass

        # 2. Get Text
        body_text = await page.inner_text('body')
        
        # --- STRATEGY 1: Meta Data (Most Accurate, Cheapest) ---
        current_price = await get_meta_price(page)
        method = "Meta Tag"
        
        # --- STRATEGY 2: AI (If Meta failed and Quota allows) ---
        ai_desc = None
        if not current_price:
            ai_resp = call_gemini(get_analysis_payload(body_text))
            if ai_resp and 'candidates' in ai_resp:
                try:
                    raw = ai_resp['candidates'][0]['content']['parts'][0]['text']
                    data = json.loads(raw.replace('```json', '').replace('```', '').strip())
                    current_price = float(data.get('price', 0))
                    ai_desc = data.get('description')
                    method = "AI"
                except: pass
        
        # --- STRATEGY 3: Regex (Brute Force) ---
        if not current_price:
            current_price = regex_fallback(body_text)
            method = "Regex (Max)"

        # --- SAVE RESULT ---
        if current_price > 0:
            print(f"   💰 Found: ${current_price} via {method}")
            
            update_data = {
                "price": current_price,
                "updated_at": "now()"
            }
            if img_url: update_data['image_url'] = img_url
            if ai_desc: update_data['description'] = ai_desc
            
            # Update DB
            supabase.table("products").update(update_data).eq("id", pid).execute()
            supabase.table("product_sources").update({
                "last_price": current_price,
                "last_checked": "now()"
            }).eq("id", source_id).execute()
            supabase.table("price_history").insert({
                "product_id": pid,
                "price": current_price
            }).execute()
        else:
            print("   ❌ Could not find price.")
            
        await page.close()

    except Exception as e:
        print(f"   ❌ Failed: {e}")

async def main():
    print("📡 Fetching patrol list...")
    response = supabase.table("product_sources").select("*, products(*)").execute()
    sources = response.data
    
    if not sources:
        print("💤 No products.")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for row in sources:
            await process_product(browser, row)
            # Sleep to respect rate limits
            time.sleep(5) 
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
