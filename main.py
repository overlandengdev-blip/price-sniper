import asyncio
import os
import random
import re
import time
import requests
import json
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

# List of models to try in order of preference
MODELS_TO_TRY = ["gemini-1.5-flash", "gemini-1.5-flash-latest", "gemini-pro"]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
]

# --- GEMINI AI HELPERS (With Fallback) ---

def call_gemini(payload):
    """Sends a request to Google Gemini API with model fallback."""
    headers = {'Content-Type': 'application/json'}
    
    for model in MODELS_TO_TRY:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
        
        for attempt in range(2): # Try each model twice
            try:
                response = requests.post(url, headers=headers, data=json.dumps(payload))
                
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 429:
                    print(f"   ⏳ Rate Limit on {model}. Sleeping 5s...")
                    time.sleep(5)
                elif response.status_code == 404:
                    print(f"   ⚠️ Model {model} not found. Switching to backup...")
                    break # Break inner loop to try next model
                else:
                    print(f"   ⚠️ API Error {response.status_code} on {model}")
                    break
            except Exception as e:
                print(f"   ❌ Connection Error: {e}")
                time.sleep(2)
    
    print("   ❌ All AI models failed.")
    return None

def get_full_analysis_prompt(text):
    return {
        "contents": [{
            "parts": [{
                "text": (
                    f"Analyze this product page text and extract detailed data. "
                    f"Return ONLY a raw JSON object (no markdown formatting) with these keys:\n"
                    f"- 'price': (float) The current price.\n"
                    f"- 'description': (string) A short, punchy marketing summary (max 200 chars).\n"
                    f"- 'weight': (string) The weight if found (e.g. '25kg'), else 'N/A'.\n"
                    f"- 'compatibility': (string) Vehicle fitment details (e.g. 'Fits Ford Ranger 2012+'), else 'Universal'.\n"
                    f"- 'specs': (object) A dictionary of other key specs found.\n\n"
                    f"TEXT CONTENT:\n{text[:12000]}"
                )
            }]
        }]
    }

def get_price_only_prompt(text):
    return {
        "contents": [{
            "parts": [{
                "text": (
                    f"Find the price. Return ONLY the number as a float (e.g. 2450.00). "
                    f"Ignore currency. If multiple, use lowest sale price.\n"
                    f"TEXT:\n{text[:6000]}"
                )
            }]
        }]
    }

# --- SCRAPING HELPERS ---

async def get_image(page):
    try:
        img = await page.locator('meta[property="og:image"]').get_attribute('content')
        if img: return img
        img = await page.locator('img[class*="product"]').first.get_attribute('src')
        return img
    except:
        return None

# --- CORE LOGIC ---

async def process_product(browser, row):
    url = row['url']
    pid = row['product_id']
    
    product_data = row.get('products', {}) or {}
    has_description = product_data.get('description') is not None
    
    mode = "PATROL" if has_description else "DISCOVERY"
    print(f"🔎 Checking {url} [Mode: {mode}]...")

    try:
        page = await browser.new_page(user_agent=random.choice(USER_AGENTS))
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await asyncio.sleep(3)
        body_text = await page.inner_text('body')

        update_data = {"updated_at": "now()"}
        current_price = 0.0

        if mode == "DISCOVERY":
            print("   🚀 New Product! Grabbing Image & Details...")
            
            img_url = await get_image(page)
            if img_url: 
                update_data['image_url'] = img_url

            resp = call_gemini(get_full_analysis_prompt(body_text))
            
            if resp: # CRASH FIX: Only proceed if we got a response
                try:
                    candidates = resp.get('candidates', [])
                    if candidates:
                        raw_text = candidates[0]['content']['parts'][0]['text']
                        clean_json = raw_text.replace('```json', '').replace('```', '').strip()
                        data = json.loads(clean_json)
                        
                        current_price = float(data.get('price', 0))
                        update_data.update({
                            "price": current_price,
                            "description": data.get('description'),
                            "weight": data.get('weight'),
                            "compatibility": data.get('compatibility'),
                            "specs": data.get('specs'),
                            "is_approved": False
                        })
                        print(f"   🧠 AI Analysis Success: ${current_price}")
                    else:
                        print("   ⚠️ AI returned no candidates.")
                except Exception as e:
                    print(f"   ⚠️ AI Parsing Failed: {e}")

        else:
            print("   ⚡ Known Product. Checking Price only...")
            resp = call_gemini(get_price_only_prompt(body_text))
            
            if resp:
                try:
                    candidates = resp.get('candidates', [])
                    if candidates:
                        text = candidates[0]['content']['parts'][0]['text']
                        clean_price = text.strip().replace('$', '').replace(',', '')
                        match = re.search(r"(\d+\.?\d*)", clean_price)
                        if match: 
                            current_price = float(match.group(1))
                            update_data["price"] = current_price
                except:
                    print("   ⚠️ Could not parse price.")

        # --- SAVE TO DATABASE ---
        if current_price > 0:
            print(f"   💰 Saving Price: ${current_price}")
            
            supabase.table("products").update(update_data).eq("id", pid).execute()
            
            supabase.table("product_sources").update({
                "last_price": current_price,
                "last_checked": "now()"
            }).eq("id", row['id']).execute()
            
            supabase.table("price_history").insert({
                "product_id": pid,
                "price": current_price
            }).execute()
        
        await page.close()

    except Exception as e:
        print(f"   ❌ Failed to process URL: {e}")

async def main():
    print("📡 Fetching patrol list from Supabase...")
    response = supabase.table("product_sources").select("*, products(*)").execute()
    sources = response.data
    
    if not sources:
        print("💤 No products to track.")
        return

    print(f"🔥 Starting patrol for {len(sources)} products...")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for row in sources:
            await process_product(browser, row)
            await asyncio.sleep(random.randint(2, 5))
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
