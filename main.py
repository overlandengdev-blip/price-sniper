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

CURRENT_MODEL = "gemini-1.5-flash"

def get_price_from_gemini_direct(text_content):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{CURRENT_MODEL}:generateContent?key={GEMINI_API_KEY}"
    headers = {'Content-Type': 'application/json'}
    
    payload = {
        "contents": [{
            "parts": [{
                "text": (
                    f"Analyze this product text and find the current selling price. "
                    f"Return ONLY the number as a float (e.g. 2450.00). "
                    f"Ignore currency symbols. If multiple exist, choose the lowest 'Sale' price. "
                    f"If no price is found, return 0.\n\n"
                    f"TEXT CONTENT:\n{text_content[:6000]}"
                )
            }]
        }]
    }

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
                except (KeyError, IndexError, AttributeError):
                    pass
                return 0.0
            elif response.status_code == 429:
                print(f"⏳ Hit Rate Limit (429). Waiting 10s...")
                time.sleep(10)
            else:
                return 0.0
        except Exception as e:
            print(f"Request Connection Failed: {e}")
    return 0.0

async def get_product_image(page):
    """Finds the main product image using standard Meta Tags."""
    try:
        # Priority 1: Open Graph Image (Standard for social sharing)
        img_url = await page.locator('meta[property="og:image"]').get_attribute('content')
        if img_url: return img_url
        
        # Priority 2: Twitter Image
        img_url = await page.locator('meta[name="twitter:image"]').get_attribute('content')
        if img_url: return img_url
        
        # Priority 3: First large image on page (Fallback)
        img_url = await page.locator('img[width="500"]').first.get_attribute('src')
        return img_url
    except:
        return None

async def process_product(browser, row):
    url = row['url']
    print(f"Checking: {url}...")
    try:
        page = await browser.new_page(user_agent=random.choice(USER_AGENTS))
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await asyncio.sleep(5)
        
        # 1. Get Text for Price
        body_text = await page.inner_text('body')
        price = get_price_from_gemini_direct(body_text)
        
        # 2. Get Image URL (NEW)
        image_url = await get_product_image(page)
        
        if price > 0:
            print(f"✅ Found Price: ${price}")
            
            # Prepare update data
            update_data = {
                "price": price,
                "updated_at": "now()"
            }
            
            # Only update image if we found a new one
            if image_url:
                print(f"📸 Found Image: {image_url[:30]}...")
                update_data["image_url"] = image_url

            # Save to Database
            supabase.table("products").update(update_data).eq("id", row['product_id']).execute()
            
            supabase.table("product_sources").update({
                "last_price": price, 
                "last_checked": "now()"
            }).eq("id", row['id']).execute()
            
            supabase.table("price_history").insert({
                "product_id": row['product_id'], 
                "price": price
            }).execute()
        else:
            print("No price found.")
        
        await page.close()
    except Exception as e:
        print(f"Failed processing {url}: {e}")

async def main():
    sources = supabase.table("product_sources").select("*").execute().data
    if not sources: return
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for row in sources:
            await process_product(browser, row)
            await asyncio.sleep(5)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
