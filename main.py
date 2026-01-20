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

# --- ADVANCED AI TROUBLESHOOTING ---

def get_valid_model():
    """
    Asks Google which models are actually available for this API Key.
    Returns the best available model name.
    """
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
    try:
        response = requests.get(url)
        if response.status_code == 200:
            data = response.json()
            models = [m['name'] for m in data.get('models', [])]
            
            # Priority List: Try to find the best model in the available list
            priorities = [
                'models/gemini-1.5-flash',
                'models/gemini-1.5-pro',
                'models/gemini-1.0-pro',
                'models/gemini-pro'
            ]
            
            print(f"   ℹ️ Available Models: {models}")
            
            for p in priorities:
                # We look for partial matches (e.g., 'models/gemini-1.5-flash-001')
                match = next((m for m in models if p in m), None)
                if match:
                    print(f"   ✅ Selected Model: {match}")
                    return match
            
            # If no priority match, take the first available gemini model
            fallback = next((m for m in models if 'gemini' in m), None)
            if fallback: return fallback
            
    except Exception as e:
        print(f"   ⚠️ Model Discovery Failed: {e}")
    
    # Ultimate Fallback if discovery fails
    return "models/gemini-pro"

# Find the model ONCE at startup
CURRENT_MODEL = get_valid_model()

def call_gemini(payload):
    """Hits the API using the dynamically discovered model."""
    url = f"https://generativelanguage.googleapis.com/v1beta/{CURRENT_MODEL}:generateContent?key={GEMINI_API_KEY}"
    headers = {'Content-Type': 'application/json'}
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        if response.status_code == 200:
            return response.json()
        else:
            print(f"   ⚠️ AI Error {response.status_code}: {response.text[:200]}")
    except Exception as e:
        print(f"   ❌ Connection Error: {e}")
    return None

def regex_fallback(text):
    """
    DUMB MODE: If AI fails, use Regex to find the price.
    Finds patterns like $1,200.50 or 1200.00
    """
    # Look for currency symbols followed by numbers
    matches = re.findall(r'\$\s?([0-9,]+\.?[0-9]*)', text)
    if not matches:
        # Look for "Price: 1200" pattern
        matches = re.findall(r'Price.*?([0-9,]+\.?[0-9]*)', text, re.IGNORECASE)
    
    if matches:
        # Clean up commas and convert to float
        prices = []
        for m in matches:
            try:
                clean = float(m.replace(',', ''))
                prices.append(clean)
            except:
                continue
        
        # Filter out unrealistic low prices (accessories) and huge outliers
        prices = [p for p in prices if p > 10 and p < 50000]
        
        if prices:
            return min(prices) # Return the lowest valid price found
    return 0.0

# --- PROMPTS ---

def get_analysis_payload(text):
    return {
        "contents": [{
            "parts": [{
                "text": (
                    f"Return raw JSON with: 'price' (number), 'description' (string), 'specs' (object). "
                    f"Text: {text[:10000]}"
                )
            }]
        }]
    }

# --- PROCESSOR ---

async def process_product(browser, row):
    url = row['url']
    pid = row['product_id']
    print(f"🔎 Checking {url}...")

    try:
        page = await browser.new_page(user_agent=random.choice(USER_AGENTS))
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await asyncio.sleep(3)
        body_text = await page.inner_text('body')

        update_data = {"updated_at": "now()"}
        current_price = 0.0
        
        # 1. Try AI First
        ai_resp = call_gemini(get_analysis_payload(body_text))
        ai_success = False
        
        if ai_resp and 'candidates' in ai_resp:
            try:
                raw = ai_resp['candidates'][0]['content']['parts'][0]['text']
                clean = raw.replace('```json', '').replace('```', '').strip()
                data = json.loads(clean)
                
                current_price = float(data.get('price', 0))
                
                # Only update details if they are missing
                if not row.get('products', {}).get('description'):
                    update_data['description'] = data.get('description')
                    update_data['specs'] = data.get('specs')
                    # Grab image if new
                    try:
                        img = await page.locator('meta[property="og:image"]').get_attribute('content')
                        if img: update_data['image_url'] = img
                    except: pass
                
                if current_price > 0:
                    ai_success = True
                    print(f"   🧠 AI Found Price: ${current_price}")
            except Exception as e:
                print(f"   ⚠️ AI Parse Error: {e}")

        # 2. Fallback to Regex if AI failed
        if not ai_success or current_price == 0:
            print("   ⚠️ AI failed. Switching to Regex Fallback...")
            current_price = regex_fallback(body_text)
            if current_price > 0:
                print(f"   🔢 Regex Found Price: ${current_price}")

        # 3. Save
        if current_price > 0:
            update_data['price'] = current_price
            
            # Update Main Product
            supabase.table("products").update(update_data).eq("id", pid).execute()
            
            # Update Source
            supabase.table("product_sources").update({
                "last_price": current_price,
                "last_checked": "now()"
            }).eq("id", row['id']).execute()
            
            # History
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

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for row in sources:
            await process_product(browser, row)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
