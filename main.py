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

# Initialize Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
]

# --- DIRECT API HELPERS (NO SDK) ---

def call_gemini_raw(prompt_payload):
    """
    Directly hits the Gemini API via HTTP Request.
    Bypasses SDK versioning issues by trying known stable endpoints.
    """
    headers = {'Content-Type': 'application/json'}
    
    # 1. Define the endpoints we want to try (Stable v1 first, then v1beta)
    # We use explicit model names for each endpoint.
    endpoints = [
        # Strategy A: Gemini 1.5 Flash on Stable v1 (Best)
        f"https://generativelanguage.googleapis.com/v1/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}",
        # Strategy B: Gemini 1.5 Flash on Beta (Fallback)
        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}",
        # Strategy C: Gemini Pro (Old Reliable)
        f"https://generativelanguage.googleapis.com/v1/models/gemini-pro:generateContent?key={GEMINI_API_KEY}"
    ]

    for url in endpoints:
        try:
            print(f"   🤖 Sending request to: ...{url.split('models/')[1].split(':')[0]}...")
            response = requests.post(url, headers=headers, json=prompt_payload, timeout=30)
            
            if response.status_code == 200:
                return response.json()
            
            # If 404, the model/version combo is wrong. Try next.
            # If 429, we are rate limited. Sleep and retry (or skip).
            elif response.status_code == 429:
                print("   ⏳ Rate Limit. Sleeping 5s...")
                time.sleep(5)
                continue
            else:
                print(f"   ⚠️ API Error {response.status_code}: {response.text[:100]}")
                
        except Exception as e:
            print(f"   ❌ Connection Error: {e}")
            time.sleep(1)
            
    print("   ❌ All API endpoints failed.")
    return None

# --- PROMPTS ---

def get_full_analysis_payload(text):
    return {
        "contents": [{
            "parts": [{
                "text": (
                    f"Analyze this product page text and extract detailed data. "
                    f"Return ONLY a raw JSON object (no markdown) with these keys:\n"
                    f"- 'price': (float) The current price (0 if not found).\n"
                    f"- 'description': (string) A short, punchy marketing summary (max 200 chars).\n"
                    f"- 'weight': (string) The weight if found (e.g. '25kg'), else 'N/A'.\n"
                    f"- 'compatibility': (string) Vehicle fitment details, else 'Universal'.\n"
                    f"- 'specs': (object) A dictionary of other key specs found.\n\n"
                    f"TEXT CONTENT:\n{text[:15000]}"
                )
            }]
        }]
    }

def get_price_only_payload(text):
    return {
        "contents": [{
            "parts": [{
                "text": (
                    f"Find the price. Return a JSON object with one key: 'price' (float). "
                    f"Ignore currency. If multiple, use lowest sale price. "
                    f"If no price is found, return 0.\n"
                    f"TEXT:\n{text[:8000]}"
                )
            }]
        }]
    }

# --- CORE LOGIC ---

async def get_image(page):
    try:
        img = await page.locator('meta[property="og:image"]').get_attribute('content')
        if img: return img
        img = await page.locator('img[class*="product"]').first.get_attribute('src')
        return img
    except:
        return None

async def process_product(browser, row):
    url = row['url']
    pid = row['product_id']
    
    product_data = row.get('products', {}) or {}
    # Discovery mode if description is missing
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
            if img_url: update_data['image_url'] = img_url

            # Call AI (Raw)
            resp = call_gemini_raw(get_full_analysis_payload(body_text))
            
            if resp and 'candidates' in resp:
                try:
                    raw_text = resp['candidates'][0]['content']['parts'][0]['text']
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
                    print(f"   🧠 AI Success: Price ${current_price} | {data.get('description')[:30]}...")
                except Exception as e:
                    print(f"   ⚠️ JSON Parse Error: {e}")
            else:
                 print("   ⚠️ No valid AI response.")

        else:
            print("   ⚡ Known Product. Checking Price only...")
            resp = call_gemini_raw(get_price_only_payload(body_text))
            
            if resp and 'candidates' in resp:
                try:
                    raw_text = resp['candidates'][0]['content']['parts'][0]['text']
                    clean_json = raw_text.replace('```json', '').replace('```', '').strip()
                    data = json.loads(clean_json)
                    current_price = float(data.get('price', 0))
                    if current_price > 0:
                        update_data["price"] = current_price
                except:
                    print("   ⚠️ Could not parse price JSON.")

        # SAVE TO DB
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
        print(f"   ❌ Failed: {e}")

async def main():
    print("📡 Fetching patrol list...")
    response = supabase.table("product_sources").select("*, products(*)").execute()
    sources = response.data
    
    if not sources:
        print("💤 No products.")
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
