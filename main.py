import asyncio
import os
import random
import re
import time
import json
import requests
from playwright.async_api import async_playwright
from supabase import create_client, Client
from bs4 import BeautifulSoup

# --- CONFIGURATION ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not all([SUPABASE_URL, SUPABASE_KEY, GEMINI_API_KEY]):
    print("❌ Critical Error: Missing Secrets.")
    exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
]

# --- 1. THE "CLICKER" ENGINE (Reveals Hidden Specs) ---
async def expand_hidden_content(page):
    """Hunts for 'Specs', 'Dimensions', 'Features' buttons and clicks them."""
    # Common keywords for dropdowns
    keywords = ["spec", "dimension", "weight", "feature", "detail", "fitment", "compat"]
    
    # Try to find buttons or tabs containing these words
    for key in keywords:
        try:
            # Look for buttons/summaries/headers with the keyword
            # We use a broad locator to find anything clickable with that text
            locator = page.locator(f"button:text-is('{key}'), summary:text-is('{key}'), h3:has-text('{key}'), div[role='button']:has-text('{key}')")
            count = await locator.count()
            
            for i in range(count):
                try:
                    elem = locator.nth(i)
                    if await elem.is_visible():
                        await elem.click(timeout=1000)
                        await asyncio.sleep(0.5) # Wait for animation
                        print(f"   👉 Clicked '{key}' section...")
                except: pass
        except: pass

# --- 2. THE "SPEC HUNTER" (Regex instead of AI) ---
def extract_deep_specs(text):
    specs = {}
    
    # Clean text for easier searching
    clean = text.lower().replace('\n', ' ')
    
    # A. Find WEIGHT (e.g., 25kg, 4.5 kg)
    weight = re.search(r'(\d+(\.\d+)?\s?kg)', clean)
    if weight: specs['weight'] = weight.group(1).replace(' ', '')
    
    # B. Find DIMENSIONS (e.g., 1200x1400mm, 50mm x 50mm)
    dims = re.search(r'(\d+\s?[xX]\s?\d+(\s?[xX]\s?\d+)?\s?(mm|cm|m))', clean)
    if dims: specs['dimensions'] = dims.group(1).replace(' ', '')
    
    # C. Find COMPATIBILITY (Years + Makes)
    # Look for year ranges like "2015+", "2012-2020" near vehicle names
    vehicles = []
    makes = ["toyota", "ford", "nissan", "mitsubishi", "isuzu", "mazda", "jeep", "land rover", "ram", "chevrolet"]
    
    for make in makes:
        if make in clean:
            # Find year pattern near the make
            # Matches: "Toyota Hilux 2015+" or "2012 Ford Ranger"
            pattern = re.search(fr'({make}.*?20\d\d[-+]?)|(20\d\d[-+]?.*?{make})', clean)
            if pattern:
                # Clean up the match to look nice
                raw_match = pattern.group(0).strip()
                vehicles.append(raw_match.title())
    
    if vehicles:
        # Deduplicate and take top 5
        specs['compatibility'] = ", ".join(list(set(vehicles))[:5])
        
    return specs

# --- 3. DESCRIPTION HARVESTER ---
def get_best_description(soup, meta_desc):
    """If Meta Description sucks, find the best paragraph on the page."""
    
    # If meta desc is good (long enough and not generic), use it
    if meta_desc and len(meta_desc) > 50 and "loading" not in meta_desc.lower():
        return meta_desc
        
    # Otherwise, hunt for the main product text
    # Strategy: Find the <p> tag with the most text inside the main content area
    paragraphs = soup.find_all('p')
    best_p = ""
    for p in paragraphs:
        text = p.get_text().strip()
        # Filter out junk like "Copyright", "Cart", "cookies"
        if len(text) > len(best_p) and len(text) < 1000:
            if "cookie" not in text.lower() and "rights reserved" not in text.lower():
                best_p = text
                
    if len(best_p) > 30:
        print("   📝 Found description from page content.")
        return best_p
        
    return "No description available."

# --- 4. PRICE LOGIC ---
def get_price_strategies(soup, text):
    # Strategy A: Meta Tags (Best)
    candidates = [
        soup.find("meta", property="og:price:amount"),
        soup.find("meta", property="product:price:amount"),
        soup.find("span", itemprop="price"),
        soup.find("meta", {"name": "twitter:data1"})
    ]
    for tag in candidates:
        if tag:
            val = tag.get("content") or tag.get_text()
            if val:
                try:
                    p = float(re.sub(r'[^0-9.]', '', val))
                    if p > 5: return p
                except: continue
                
    # Strategy B: Regex Max (Fallback)
    matches = re.findall(r'\$\s?([0-9,]+\.?[0-9]*)', text)
    prices = []
    for m in matches:
        try:
            val = float(m.replace(',', ''))
            if 15 < val < 50000: prices.append(val)
        except: pass
    if prices: return max(prices)
    
    return None

# --- PROCESSOR ---
async def process_product(browser, row):
    url = row['url']
    pid = row['product_id']
    source_id = row['id']
    
    print(f"🔎 Processing: {url}...")

    # Auto-Linker
    if pid is None or pid == "None":
        print("   🛠️ Orphan link. Creating placeholder...")
        try:
            # Note: Category is now optional in DB
            new_prod = supabase.table("products").insert({"name": "Scanning...", "is_approved": False}).execute()
            pid = new_prod.data[0]['id']
            supabase.table("product_sources").update({"product_id": pid}).eq("id", source_id).execute()
        except Exception as e:
            print(f"   ❌ DB Error: {e}. Skipping.")
            return

    try:
        page = await browser.new_page(user_agent=random.choice(USER_AGENTS))
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await asyncio.sleep(4)
        
        # --- NEW: EXPAND DROPDOWNS ---
        await expand_hidden_content(page)
        
        # Get content
        html = await page.content()
        body_text = await page.inner_text("body")
        soup = BeautifulSoup(html, 'html.parser')
        
        # 1. Get Details
        title_tag = soup.find("meta", property="og:title")
        name = title_tag.get("content") if title_tag else soup.find("title").get_text()
        
        desc_tag = soup.find("meta", property="og:description")
        meta_desc = desc_tag.get("content") if desc_tag else ""
        description = get_best_description(soup, meta_desc)
        
        img_tag = soup.find("meta", property="og:image")
        image_url = img_tag.get("content") if img_tag else None
        
        # 2. Get Specs (The new Hunter)
        specs = extract_deep_specs(body_text)
        if specs:
            print(f"   ⚙️ Found Specs: {specs}")
            
        # 3. Get Price
        price = get_price_strategies(soup, body_text)
        
        # 4. Save
        update_data = {
            "name": name.strip()[:255], 
            "description": description.strip(),
            "specs": specs,
            "updated_at": "now()"
        }
        if image_url: update_data['image_url'] = image_url
        if price: update_data['price'] = price
        
        # To DB
        supabase.table("products").update(update_data).eq("id", pid).execute()
        
        if price:
            print(f"   💰 Saved Price: ${price}")
            supabase.table("product_sources").update({"last_price": price, "last_checked": "now()"}).eq("id", source_id).execute()
            supabase.table("price_history").insert({"product_id": pid, "price": price}).execute()

        await page.close()

    except Exception as e:
        print(f"   ⚠️ Scrape Error: {e}")

async def main():
    print("🚀 Starting Deep Patrol...")
    response = supabase.table("product_sources").select("*, products(*)").execute()
    sources = response.data
    
    if not sources:
        print("💤 No products.")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for row in sources:
            await process_product(browser, row)
            time.sleep(3) 
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
