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
    print("Error: Missing API Keys in Secrets.")
    exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
]

def get_available_models():
    """Fetches the list of models your key is allowed to use."""
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
        response = requests.get(url)
        if response.status_code == 200:
            return [m['name'] for m in response.json().get('models', [])]
    except:
        pass
    return []

def select_best_model():
    """Picks the best 'Free Tier' friendly model."""
    available = get_available_models()
    print(f"🔍 Available Models: {available}")
    
    # Priority list (Cheapest/Fastest first)
    priorities = [
        "models/gemini-1.5-flash",
        "models/gemini-1.5-flash-001",
        "models/gemini-1.5-flash-latest",
        "models/gemini-1.0-pro",
        "models/gemini-pro"
    ]
    
    for p in priorities:
        if p in available:
            print(f"✅ Selected: {p}")
            return p
            
    # Fallback: Pick the first one that isn't an embedding model
    for m in available:
        if "embedding" not in m and "vision" not in m:
            print(f"⚠️ Fallback Model: {m}")
            return m
            
    return "models/gemini-1.5-flash" # Blind hope

CURRENT_MODEL = select_best_model()

def get_price_from_gemini_direct(text_content):
    url = f"https://generativelanguage.googleapis.com/v1beta/{CURRENT_MODEL}:generateContent?key={GEMINI_API_KEY}"
    headers = {'Content-Type': 'application/json'}
    
    # Truncate to 6000 chars to save tokens (prevent 429)
    payload = {
        "contents": [{
            "parts": [{"text": f"Find the price. Return number ONLY (e.g. 2495.00). Text: {text_content[:6000]}"}]
        }]
    }

    # RETRY LOOP (The Fix for 429 Errors)
    for attempt in range(3):
        try:
            response = requests.post(url, headers=headers, data=json.dumps(payload))
            
            if response.status_code == 200:
                data = response.json()
                try:
                    price_str = data['candidates'][0]['content']['parts'][0]['text']
                    clean_price = price_str.strip().replace('$', '').replace(',', '')
                    match = re.search(r"(\d+\.?\d*)", clean_price)
                    if match:
                        return float(match.group(1))
                except:
                    pass
                return 0.0
            
            elif response.status_code == 429:
                print(f"⏳ Quota Limit (429). Sleeping 20s... (Attempt {attempt+1}/3)")
                time.sleep(20) # Wait for quota reset
            else:
                print(f"API Error {response.status_code}")
                return 0.0
                
        except Exception as e:
            print(f"Request Failed: {e}")
            
    return 0.0

async def process_product(browser, row):
    url = row['url']
    print(f"Checking: {url}...")
    try:
        page = await browser.new_page(user_agent=random.choice(USER_AGENTS))
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await asyncio.sleep(5)
        
        body_text = await page.inner_text('body')
        price = get_price_from_gemini_direct(body_text)
        
        if price > 0:
            print(f"Found Price: ${price}")
            supabase.table("products").update({"price": price}).eq("id", row['product_id']).execute()
            supabase.table("product_sources").update({"last_price": price, "last_checked": "now()"}).eq("id", row['id']).execute()
        else:
            print("No price found.")
        await page.close()
    except Exception as e:
        print(f"Failed: {e}")

async def main():
    sources = supabase.table("product_sources").select("*").execute().data
    if not sources: return
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for row in sources: await process_product(browser, row)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
