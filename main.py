import asyncio
import os
import random
import re
import time
import json
from playwright.async_api import async_playwright
from supabase import create_client, Client
import google.generativeai as genai
from google.api_core import exceptions as google_exceptions

# --- CONFIGURATION ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not all([SUPABASE_URL, SUPABASE_KEY, GEMINI_API_KEY]):
    print("❌ Error: Missing API Keys in Secrets.")
    exit(1)

# Initialize Clients
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
genai.configure(api_key=GEMINI_API_KEY)

# Configuration for Gemini
GENERATION_CONFIG = {
    "temperature": 0.5,
    "top_p": 0.95,
    "top_k": 64,
    "max_output_tokens": 8192,
    "response_mime_type": "application/json",
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
]

# --- GEMINI AI FUNCTION (OFFICIAL LIBRARY) ---

def call_gemini(prompt_text):
    """Uses the official Google Library to call Gemini with fallback models."""
    # List of models to try (Newest to Oldest)
    models_to_try = ["gemini-1.5-flash", "gemini-1.5-flash-latest", "gemini-pro"]
    
    for model_name in models_to_try:
        try:
            model = genai.GenerativeModel(
                model_name=model_name,
                generation_config=GENERATION_CONFIG
            )
            
            response = model.generate_content(prompt_text)
            
            # Check if response is valid
            if response.text:
                return response.text
                
        except google_exceptions.NotFound:
            print(f"   ⚠️ Model '{model_name}' not found. Trying next...")
            continue
        except Exception as e:
            print(f"   ⚠️ Error with {model_name}: {e}")
            time.sleep(2)
            continue
            
    print("   ❌ All AI models failed.")
    return None

# --- PROMPTS ---

def get_full_analysis_prompt(text):
    return (
        f"Analyze this product page text and extract detailed data. "
        f"Return ONLY a raw JSON object with these keys:\n"
        f"- 'price': (float) The current price (0 if not found).\n"
        f"- 'description': (string) A short, punchy marketing summary (max 200 chars).\n"
        f"- 'weight': (string) The weight if found (e.g. '25kg'), else 'N/A'.\n"
        f"- 'compatibility': (string) Vehicle fitment details (e.g. 'Fits Ford Ranger 2012+'), else 'Universal'.\n"
        f"- 'specs': (object) A dictionary of other key specs found.\n\n"
        f"TEXT CONTENT:\n{text[:15000]}"
    )

def get_price_only_prompt(text):
    return (
        f"Find the price. Return a JSON object with one key: 'price' (float). "
        f"Ignore currency symbols. If multiple prices exist, use the lowest sale price. "
        f"If no price is found, return 0.\n"
        f"TEXT:\n{text[:8000]}"
    )

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

            # Call AI
            json_text = call_gemini(get_full_analysis_prompt(body_text))
            
            if json_text:
                try:
                    # Clean markdown if present (e.g., ```json ... ```)
                    clean_json = json_text.replace('```json', '').replace('```', '').strip()
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
            print("   ⚡ Known Product. Checking Price only...")
            json_text = call_gemini(get_price_only_prompt(body_text))
            
            if json_text:
                try:
                    clean_json = json_text.replace('```json', '').replace('```', '').strip()
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
