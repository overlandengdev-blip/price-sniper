import asyncio
import os
import random
import re
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

def find_working_model():
    """
    Asks Google API which models are actually available for this API Key
    and picks the best one automatically.
    """
    print("🔍 Auto-detecting available AI models...")
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
        response = requests.get(url)
        
        if response.status_code != 200:
            print(f"⚠️ Could not list models. Status: {response.status_code}")
            print(f"Response: {response.text}")
            return "models/gemini-pro" # Fallback

        data = response.json()
        models = data.get('models', [])
        
        # Priority list: Try to find these in order
        preferences = [
            "gemini-1.5-flash",
            "gemini-1.5-pro",
            "gemini-1.0-pro",
            "gemini-pro"
        ]
        
        for pref in preferences:
            for m in models:
                if pref in m['name']:
                    print(f"✅ Selected Model: {m['name']}")
                    return m['name']
        
        # If none match, take the first available generative model
        if models:
            fallback = models[0]['name']
            print(f"⚠️ No preferred model found. Using fallback: {fallback}")
            return fallback
            
    except Exception as e:
        print(f"⚠️ Model detection failed: {e}")
    
    return "models/gemini-pro" # Ultimate fallback

# Get the model ONCE at startup
CURRENT_MODEL = find_working_model()

def get_price_from_gemini_direct(text_content):
    url = f"https://generativelanguage.googleapis.com/v1beta/{CURRENT_MODEL}:generateContent?key={GEMINI_API_KEY}"
    headers = {'Content-Type': 'application/json'}
    
    payload = {
        "contents": [{
            "parts": [{
                "text": (
                    f"Analyze this product text and find the current selling price. "
                    f"Return ONLY the number as a float (e.g. 2450.00). "
                    f"Ignore currency symbols. If multiple exist, choose the lowest 'Sale' price. "
                    f"If no price is found, return 0.\n\n"
                    f"TEXT CONTENT:\n{text_content[:8000]}"
                )
            }]
        }]
    }

    try:
        response = requests.post(url, headers=headers, data=json.dumps(payload))
        
        if response.status_code == 200:
            data = response.json()
            try:
                answer = data['candidates'][0]['content']['parts'][0]['text']
                price_str = answer.strip().replace('$', '').replace(',', '')
                match = re.search(r"(\d+\.?\d*)", price_str)
                if match:
                    return float(match.group(1))
            except (KeyError, IndexError):
                print(f"AI Response Format Error: {data}")
        else:
            print(f"API Error {response.status_code}: {response.text}")
            
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
            supabase.table("product_sources").update({
                "last_price": price, 
                "last_checked": "now()"
            }).eq("id", row['id']).execute()
            supabase.table("price_history").insert({
                "product_id": row['product_id'], 
                "price": price
            }).execute()
        else:
            print("No price found (returned 0).")
        
        await page.close()
    except Exception as e:
        print(f"Failed processing {url}: {e}")

async def main():
    sources = supabase.table("product_sources").select("*").execute().data
    if not sources:
        print("No products to track.")
        return

    print(f"Starting patrol for {len(sources)} products...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for row in sources:
            await process_product(browser, row)
            await asyncio.sleep(5)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
