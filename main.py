import asyncio
import os
import random
import re
from playwright.async_api import async_playwright
from supabase import create_client, Client
import google.generativeai as genai # <--- Classic Import

# --- CONFIGURATION ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not all([SUPABASE_URL, SUPABASE_KEY, GEMINI_API_KEY]):
    print("Error: Missing API Keys in Secrets.")
    exit(1)

# --- SETUP CLIENTS ---
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
genai.configure(api_key=GEMINI_API_KEY) # <--- Classic Setup

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
]

async def get_price_from_gemini(text_content):
    try:
        # Use the GenerativeModel class (Stable)
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        prompt = (
            f"Find the price in this text. Return ONLY the number (e.g. 2450.00). "
            f"If multiple, pick the lowest sale price. Ignore currency symbols. "
            f"Content:\n{text_content[:8000]}"
        )
        
        response = model.generate_content(prompt)
        
        if response.text:
            price_str = response.text.strip().replace('$', '').replace(',', '')
            match = re.search(r"(\d+\.?\d*)", price_str)
            if match:
                return float(match.group(1))
        return 0.0
    except Exception as e:
        print(f"AI Error: {e}")
        return 0.0

async def process_product(browser, row):
    url = row['url']
    print(f"Checking: {url}...")
    try:
        page = await browser.new_page(user_agent=random.choice(USER_AGENTS))
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await asyncio.sleep(5)
        
        body_text = await page.inner_text('body')
        price = await get_price_from_gemini(body_text)
        
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
    if not sources:
        print("No products to track.")
        return
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for row in sources: await process_product(browser, row)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
