import asyncio
import os
import random
import re
from playwright.async_api import async_playwright
from supabase import create_client, Client
from google import genai  # <--- NEW LIBRARY IMPORT

# --- CONFIGURATION ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not all([SUPABASE_URL, SUPABASE_KEY, GEMINI_API_KEY]):
    print("Error: Missing API Keys in Secrets.")
    exit(1)

# --- SETUP CLIENTS ---
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
# New Google Client Setup
ai_client = genai.Client(api_key=GEMINI_API_KEY)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
]

async def get_price_from_gemini(text_content):
    """Asks AI to extract the price from raw text."""
    try:
        prompt = (
            f"Analyze this product text and find the current selling price. "
            f"Return ONLY the number as a float (e.g. 2450.00). "
            f"Ignore currency symbols. If multiple exist, choose the lowest 'Sale' price. "
            f"If no price is found, return 0.\n\n"
            f"TEXT CONTENT:\n{text_content[:8000]}"
        )
        
        # NEW CODE SYNTAX FOR GEMINI 1.5
        response = ai_client.models.generate_content(
            model='gemini-1.5-flash',
            contents=prompt
        )
        
        price_str = response.text.strip().replace('$', '').replace(',', '')
        match = re.search(r"(\d+\.?\d*)", price_str)
        if match:
            return float(match.group(1))
        return 0.0
    except Exception as e:
        print(f"AI Extraction Failed: {e}")
        return 0.0

async def process_product(browser, row):
    url = row['url']
    product_id = row['product_id']
    print(f"Checking: {url}...")

    try:
        page = await browser.new_page(user_agent=random.choice(USER_AGENTS))
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await asyncio.sleep(random.randint(5, 10))
        
        body_text = await page.inner_text('body')
        price = await get_price_from_gemini(body_text)
        
        if price > 0:
            print(f"Found Price: ${price}")
            supabase.table("products").update({"price": price}).eq("id", product_id).execute()
            supabase.table("product_sources").update({
                "last_price": price, 
                "last_checked": "now()"
            }).eq("id", row['id']).execute()
            supabase.table("price_history").insert({
                "product_id": product_id, 
                "price": price
            }).execute()
        else:
            print("No price found by AI.")
        
        await page.close()
    except Exception as e:
        print(f"Failed to scrape {url}: {e}")

async def main():
    response = supabase.table("product_sources").select("*").execute()
    sources = response.data
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
