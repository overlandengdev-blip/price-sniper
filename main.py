"""
Product Specification Extractor v2.0
=====================================
A robust scraping system for 4WD/camping product data extraction.
Designed for GVM compliance apps - extracts weight, dimensions, compatibility.

Database Schema Requirements (Supabase):
-----------------------------------------
ALTER TABLE products ADD COLUMN IF NOT EXISTS regular_price FLOAT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS sale_price FLOAT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS weight_kg FLOAT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS weight_raw TEXT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS dimensions_l FLOAT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS dimensions_w FLOAT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS dimensions_h FLOAT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS dimensions_raw TEXT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS vehicle_compatibility TEXT[];
ALTER TABLE products ADD COLUMN IF NOT EXISTS availability TEXT DEFAULT 'unknown';
ALTER TABLE products ADD COLUMN IF NOT EXISTS sku TEXT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS brand TEXT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS is_universal BOOLEAN DEFAULT FALSE;
ALTER TABLE products ADD COLUMN IF NOT EXISTS specs_locked BOOLEAN DEFAULT FALSE;
ALTER TABLE products ADD COLUMN IF NOT EXISTS last_full_scrape TIMESTAMP;
"""

import asyncio
import os
import random
import re
import json
import logging
from datetime import datetime
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass, field, asdict
from playwright.async_api import async_playwright
from supabase import create_client, Client
from bs4 import BeautifulSoup
import requests

# --- CONFIGURATION ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

CONCURRENCY = 3

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

if not all([SUPABASE_URL, SUPABASE_KEY]):
    logger.error("Missing Supabase credentials.")
    exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0"
]

# --- DATA CLASSES ---

@dataclass
class ProductSpecs:
    """Complete product specification container."""
    # Core
    name: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    brand: Optional[str] = None

    # Pricing
    regular_price: Optional[float] = None
    sale_price: Optional[float] = None
    current_price: Optional[float] = None  # The effective price to pay

    # Engineering (Critical for GVM)
    weight_kg: Optional[float] = None
    weight_raw: Optional[str] = None  # Original text: "25kg" or "55 lbs"
    dimensions_l: Optional[float] = None  # Length in cm
    dimensions_w: Optional[float] = None  # Width in cm
    dimensions_h: Optional[float] = None  # Height in cm
    dimensions_raw: Optional[str] = None  # Original: "120 x 80 x 45 cm"

    # Compatibility
    vehicle_compatibility: List[str] = field(default_factory=list)
    is_universal: bool = False

    # Catalog
    category: Optional[str] = None
    sku: Optional[str] = None
    availability: str = "unknown"  # in_stock, pre_order, sold_out, unknown

    # Confidence tracking
    extraction_sources: Dict[str, str] = field(default_factory=dict)


# --- CATEGORY INFERENCE ---

CATEGORY_KEYWORDS = {
    "Suspension": [
        "leaf spring", "coil spring", "shock absorber", "strut", "lift kit",
        "suspension", "sway bar", "control arm", "panhard rod", "castor kit"
    ],
    "Bullbars & Protection": [
        "bullbar", "bull bar", "nudge bar", "bash plate", "skid plate",
        "rock slider", "side step", "rear bar", "recovery point"
    ],
    "Roof Racks & Storage": [
        "roof rack", "roof bar", "roof basket", "cargo barrier", "drawer",
        "cargo box", "roof pod", "awning", "roof top tent", "rtt"
    ],
    "Lighting": [
        "led bar", "light bar", "driving light", "spot light", "flood light",
        "work light", "headlight", "tail light", "beacon", "lumen"
    ],
    "Recovery Gear": [
        "recovery kit", "snatch strap", "kinetic rope", "shackle", "winch",
        "recovery board", "maxtrax", "sand track", "hi-lift", "high lift"
    ],
    "Electrical": [
        "battery", "dual battery", "dc-dc", "inverter", "solar panel",
        "fuse box", "wiring harness", "anderson plug", "usb charger"
    ],
    "Fuel & Water": [
        "fuel tank", "jerry can", "water tank", "aux tank", "long range tank",
        "water filter", "pump", "bladder"
    ],
    "Camping & Touring": [
        "swag", "tent", "sleeping bag", "camp chair", "table", "fridge",
        "cooler", "stove", "camp kitchen", "shower", "toilet", "porta potti"
    ],
    "Tyres & Wheels": [
        "tyre", "tire", "wheel", "rim", "alloy", "steel wheel", "beadlock",
        "spare wheel", "tyre carrier"
    ],
    "Exhaust & Performance": [
        "exhaust", "snorkel", "air intake", "turbo", "intercooler",
        "catch can", "egr", "dpf", "tuner", "chip"
    ],
    "Towing": [
        "tow bar", "towbar", "tow ball", "trailer plug", "trailer connector",
        "weight distribution", "wdh", "anti-sway"
    ],
    "Communication": [
        "uhf", "cb radio", "antenna", "gps", "satellite", "spot messenger"
    ]
}

def infer_category(name: str, description: str = "") -> str:
    """Infer product category from name and description."""
    if not name:
        return "Uncategorized"

    text = f"{name} {description}".lower()

    scores = {}
    for category, keywords in CATEGORY_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > 0:
            scores[category] = score

    if scores:
        return max(scores, key=scores.get)

    return "Uncategorized"


# --- VEHICLE COMPATIBILITY ---

VEHICLE_PATTERNS = [
    # Toyota
    r"(toyota\s+)?(hilux|hiace|landcruiser|land\s*cruiser|prado|fortuner|4runner)\s*(n\d+|[0-9]{2,3}\s*series)?",
    r"(lc|landcruiser)\s*(70|76|78|79|80|100|105|200|300)\s*series?",
    r"n70|n80|gun125|gun126|kun26|ln106",
    # Nissan
    r"(nissan\s+)?(patrol|navara|pathfinder|x-trail|y61|y62|gu|gq|d22|d23|d40|np300)",
    # Ford
    r"(ford\s+)?(ranger|everest|f[0-9]{3}|px1|px2|px3|pxii|pxiii)",
    # Mazda
    r"(mazda\s+)?(bt-50|bt50|cx-[0-9])",
    # Mitsubishi
    r"(mitsubishi\s+)?(triton|pajero|challenger|outlander|mq|mr|ml|mn)",
    # Isuzu
    r"(isuzu\s+)?(d-?max|dmax|mu-?x|mux)",
    # Holden/Colorado
    r"(holden\s+)?(colorado|rodeo|trailblazer|rg|ra)",
    # Jeep
    r"(jeep\s+)?(wrangler|gladiator|cherokee|grand\s*cherokee|jk|jl|jt|wj|wk)",
    # Land Rover
    r"(land\s*rover\s+)?(defender|discovery|range\s*rover)",
    # Suzuki
    r"(suzuki\s+)?(jimny|vitara|grand\s*vitara)",
    # VW
    r"(vw|volkswagen)\s*(amarok)",
    # Generic patterns
    r"(suits?|fits?|compatible\s*with|for)\s+([a-z0-9\s\-]+(?:series|model)?)",
]

UNIVERSAL_INDICATORS = [
    "universal", "fits all", "one size fits all", "multi-fit",
    "universal fit", "suits most", "fits most vehicles", "all vehicles"
]

def extract_vehicle_compatibility(soup: BeautifulSoup, body_text: str, name: str) -> Tuple[List[str], bool]:
    """Extract vehicle compatibility from page content."""
    vehicles = set()
    is_universal = False

    text_to_search = f"{name} {body_text}".lower()

    # Check for universal fit indicators
    for indicator in UNIVERSAL_INDICATORS:
        if indicator in text_to_search:
            is_universal = True
            break

    # Look for structured compatibility sections
    compat_selectors = [
        "div[class*='compatib']", "div[class*='vehicle']", "div[class*='fitment']",
        "div[class*='fit-guide']", "section[class*='vehicle']", ".vehicle-list",
        "#vehicle-compatibility", ".product-fitment", "[data-vehicle]"
    ]

    for selector in compat_selectors:
        elems = soup.select(selector)
        for elem in elems:
            text_to_search += " " + elem.get_text(" ", strip=True).lower()

    # Extract using patterns
    for pattern in VEHICLE_PATTERNS:
        matches = re.findall(pattern, text_to_search, re.IGNORECASE)
        for match in matches:
            if isinstance(match, tuple):
                vehicle = " ".join(m for m in match if m).strip()
            else:
                vehicle = match.strip()

            if vehicle and len(vehicle) > 2:
                # Normalize
                vehicle = re.sub(r'\s+', ' ', vehicle).title()
                vehicles.add(vehicle)

    # If no specific vehicles found but it's clearly an accessory, mark as universal
    if not vehicles and not is_universal:
        camping_keywords = ["camp", "tent", "swag", "chair", "table", "fridge", "cooler", "stove"]
        if any(kw in text_to_search for kw in camping_keywords):
            is_universal = True

    return list(vehicles)[:20], is_universal  # Limit to 20 vehicles


# --- WEIGHT EXTRACTION ---

WEIGHT_PATTERNS = [
    # Explicit weight labels
    r"(?:weight|wt\.?|net\s*weight|gross\s*weight|shipping\s*weight|product\s*weight)[\s:]*([0-9]+(?:\.[0-9]+)?)\s*(kg|kgs|kilograms?|lbs?|pounds?|g|grams?|oz|ounces?)",
    # Weight in specs tables
    r"(?:^|\||\n)\s*(?:weight|mass)[\s:|\-]+([0-9]+(?:\.[0-9]+)?)\s*(kg|kgs|lbs?|g)",
    # Standalone weight with unit
    r"([0-9]+(?:\.[0-9]+)?)\s*(kg|kgs|kilograms?)\b",
    r"([0-9]+(?:\.[0-9]+)?)\s*(lbs?|pounds?)\b",
    # Weight in parentheses
    r"\(([0-9]+(?:\.[0-9]+)?)\s*(kg|lbs?)\)",
]

def normalize_weight_to_kg(value: float, unit: str) -> float:
    """Convert weight to kilograms."""
    unit = unit.lower().strip()

    if unit in ['kg', 'kgs', 'kilogram', 'kilograms']:
        return round(value, 2)
    elif unit in ['lb', 'lbs', 'pound', 'pounds']:
        return round(value * 0.453592, 2)
    elif unit in ['g', 'gram', 'grams']:
        return round(value / 1000, 2)
    elif unit in ['oz', 'ounce', 'ounces']:
        return round(value * 0.0283495, 2)

    return round(value, 2)  # Assume kg if unknown

def extract_weight(soup: BeautifulSoup, body_text: str, json_data: Dict) -> Tuple[Optional[float], Optional[str]]:
    """Extract and normalize weight from page content."""

    # Priority 1: JSON-LD structured data
    if 'weight' in json_data:
        weight_data = json_data['weight']
        if isinstance(weight_data, dict):
            value = weight_data.get('value')
            unit = weight_data.get('unitCode', weight_data.get('unitText', 'kg'))
            if value:
                try:
                    kg = normalize_weight_to_kg(float(value), unit)
                    return kg, f"{value} {unit}"
                except:
                    pass

    # Priority 2: Look in spec tables
    spec_selectors = [
        "table.specs", "table.specifications", ".product-specs table",
        "dl.specs", ".spec-list", "[class*='specification']", ".tech-specs"
    ]

    spec_text = ""
    for selector in spec_selectors:
        elems = soup.select(selector)
        for elem in elems:
            spec_text += " " + elem.get_text(" ", strip=True)

    search_text = f"{spec_text} {body_text}".lower()

    # Priority 3: Regex patterns
    best_match = None
    best_priority = 999

    for i, pattern in enumerate(WEIGHT_PATTERNS):
        matches = re.findall(pattern, search_text, re.IGNORECASE | re.MULTILINE)
        for match in matches:
            if isinstance(match, tuple) and len(match) >= 2:
                try:
                    value = float(match[0])
                    unit = match[1]

                    # Sanity check: weight should be reasonable (0.1kg to 500kg)
                    kg = normalize_weight_to_kg(value, unit)
                    if 0.1 <= kg <= 500:
                        if i < best_priority:
                            best_priority = i
                            best_match = (kg, f"{match[0]} {unit}")
                except:
                    continue

    if best_match:
        return best_match

    return None, None


# --- DIMENSIONS EXTRACTION ---

DIMENSION_PATTERNS = [
    # L x W x H format
    r"(?:dimensions?|size|measurements?)[\s:]*([0-9]+(?:\.[0-9]+)?)\s*[xX\*]\s*([0-9]+(?:\.[0-9]+)?)\s*[xX\*]\s*([0-9]+(?:\.[0-9]+)?)\s*(mm|cm|m|inches?|in|\")?",
    # Individual L/W/H
    r"(?:length|long)[\s:]*([0-9]+(?:\.[0-9]+)?)\s*(mm|cm|m|inches?|in)?",
    r"(?:width|wide)[\s:]*([0-9]+(?:\.[0-9]+)?)\s*(mm|cm|m|inches?|in)?",
    r"(?:height|high|tall|depth|deep)[\s:]*([0-9]+(?:\.[0-9]+)?)\s*(mm|cm|m|inches?|in)?",
    # Compact format: 1200x800x450mm
    r"\b([0-9]{2,4})\s*[xX\*]\s*([0-9]{2,4})\s*[xX\*]\s*([0-9]{2,4})\s*(mm|cm)\b",
]

def normalize_dimension_to_cm(value: float, unit: str) -> float:
    """Convert dimension to centimeters."""
    unit = unit.lower().strip() if unit else 'cm'

    if unit in ['mm', 'millimeter', 'millimeters']:
        return round(value / 10, 1)
    elif unit in ['cm', 'centimeter', 'centimeters']:
        return round(value, 1)
    elif unit in ['m', 'meter', 'meters']:
        return round(value * 100, 1)
    elif unit in ['in', 'inch', 'inches', '"']:
        return round(value * 2.54, 1)

    # Heuristic: if value > 100, probably mm
    if value > 100:
        return round(value / 10, 1)

    return round(value, 1)

def extract_dimensions(soup: BeautifulSoup, body_text: str, json_data: Dict) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[str]]:
    """Extract L x W x H dimensions."""

    # Check JSON-LD for dimensions
    for dim_key in ['depth', 'width', 'height']:
        if dim_key in json_data:
            # Has structured dimensions
            l = json_data.get('depth', json_data.get('length'))
            w = json_data.get('width')
            h = json_data.get('height')

            try:
                if isinstance(l, dict):
                    l = float(l.get('value', 0))
                if isinstance(w, dict):
                    w = float(w.get('value', 0))
                if isinstance(h, dict):
                    h = float(h.get('value', 0))

                if l and w and h:
                    return float(l), float(w), float(h), f"{l} x {w} x {h}"
            except:
                pass
            break

    # Search in spec tables first
    spec_text = ""
    for selector in ["table", ".specs", ".specifications", "[class*='dimension']"]:
        for elem in soup.select(selector):
            spec_text += " " + elem.get_text(" ", strip=True)

    search_text = f"{spec_text} {body_text}".lower()

    # Try L x W x H patterns
    for pattern in DIMENSION_PATTERNS[:2] + DIMENSION_PATTERNS[4:]:
        matches = re.findall(pattern, search_text, re.IGNORECASE)
        for match in matches:
            if len(match) >= 3:
                try:
                    l = float(match[0])
                    w = float(match[1])
                    h = float(match[2])
                    unit = match[3] if len(match) > 3 else 'mm'

                    l_cm = normalize_dimension_to_cm(l, unit)
                    w_cm = normalize_dimension_to_cm(w, unit)
                    h_cm = normalize_dimension_to_cm(h, unit)

                    # Sanity check: reasonable furniture/part dimensions
                    if all(1 <= d <= 500 for d in [l_cm, w_cm, h_cm]):
                        raw = f"{match[0]} x {match[1]} x {match[2]} {unit or 'mm'}"
                        return l_cm, w_cm, h_cm, raw
                except:
                    continue

    return None, None, None, None


# --- SKU / PART NUMBER EXTRACTION ---

SKU_PATTERNS = [
    r"(?:sku|part\s*(?:no|number|#)|item\s*(?:no|number|#)|product\s*(?:code|id)|model\s*(?:no|number)?|article\s*(?:no|number))[\s:#\-]*([A-Z0-9][A-Z0-9\-_]{3,20})",
    r"(?:^|\s)([A-Z]{2,5}[\-_]?[0-9]{3,8}[A-Z]?)(?:\s|$)",  # ARB-style: ARB-4450010
    r"(?:^|\s)([0-9]{5,10})(?:\s|$)",  # Numeric SKU
]

def extract_sku(soup: BeautifulSoup, body_text: str, json_data: Dict) -> Optional[str]:
    """Extract SKU or part number."""

    # Priority 1: JSON-LD
    for key in ['sku', 'productID', 'mpn', 'gtin', 'gtin13', 'gtin8', 'identifier']:
        if key in json_data:
            return str(json_data[key])[:50]

    # Priority 2: Meta tags
    sku_metas = soup.find_all("meta", attrs={"property": re.compile(r"product:.*sku|product:.*id", re.I)})
    for meta in sku_metas:
        content = meta.get("content")
        if content:
            return content[:50]

    # Priority 3: Specific HTML elements
    sku_selectors = [
        ".sku", ".product-sku", "[class*='sku']", "[itemprop='sku']",
        ".part-number", "[class*='part-number']", "[data-sku]"
    ]

    for selector in sku_selectors:
        elem = soup.select_one(selector)
        if elem:
            text = elem.get_text(strip=True)
            if text and len(text) < 50:
                return text

    # Priority 4: Regex on page text
    for pattern in SKU_PATTERNS:
        matches = re.findall(pattern, body_text, re.IGNORECASE)
        for match in matches:
            if match and 4 <= len(match) <= 30:
                return match.upper()

    return None


# --- AVAILABILITY EXTRACTION ---

AVAILABILITY_MAP = {
    "in_stock": [
        "in stock", "in-stock", "instock", "available", "ready to ship",
        "ships today", "ships within", "add to cart", "buy now", "available now"
    ],
    "pre_order": [
        "pre-order", "preorder", "pre order", "coming soon", "available soon",
        "backorder", "back-order", "back order", "notify me", "arriving"
    ],
    "sold_out": [
        "sold out", "out of stock", "outofstock", "out-of-stock", "unavailable",
        "currently unavailable", "no longer available", "discontinued"
    ]
}

def extract_availability(soup: BeautifulSoup, body_text: str, json_data: Dict) -> str:
    """Determine product availability status."""

    # Priority 1: JSON-LD
    offers = json_data.get('offers', {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}

    availability = offers.get('availability', '')
    if availability:
        availability_lower = availability.lower()
        if 'instock' in availability_lower or 'instoreonly' in availability_lower:
            return "in_stock"
        elif 'preorder' in availability_lower or 'backorder' in availability_lower:
            return "pre_order"
        elif 'outofstock' in availability_lower or 'discontinued' in availability_lower:
            return "sold_out"

    # Priority 2: Availability-specific elements
    avail_selectors = [
        ".availability", ".stock-status", "[class*='availability']",
        "[class*='stock']", ".product-availability", "[itemprop='availability']"
    ]

    avail_text = ""
    for selector in avail_selectors:
        elem = soup.select_one(selector)
        if elem:
            avail_text += " " + elem.get_text(strip=True).lower()

    search_text = f"{avail_text} {body_text[:2000]}".lower()

    # Check patterns
    for status, keywords in AVAILABILITY_MAP.items():
        for kw in keywords:
            if kw in search_text:
                return status

    # Default: if we found a price and add to cart, probably in stock
    add_to_cart = soup.select_one("button[class*='cart'], input[value*='cart'], .add-to-cart")
    if add_to_cart:
        return "in_stock"

    return "unknown"


# --- ENHANCED PRICE EXTRACTION ---

def extract_prices(soup: BeautifulSoup, body_text: str, json_data: Dict) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Extract regular price, sale price, and current (effective) price.
    Returns: (regular_price, sale_price, current_price)
    """
    regular_price = None
    sale_price = None
    candidates = []

    # Source 1: JSON-LD (Highest trust)
    offers = json_data.get('offers', {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}

    if offers:
        if 'price' in offers:
            try:
                candidates.append({"src": "JSON", "val": float(offers['price']), "trust": 10, "type": "current"})
            except: pass

        if 'lowPrice' in offers:
            try:
                candidates.append({"src": "JSON-Low", "val": float(offers['lowPrice']), "trust": 10, "type": "sale"})
            except: pass

        if 'highPrice' in offers:
            try:
                candidates.append({"src": "JSON-High", "val": float(offers['highPrice']), "trust": 9, "type": "regular"})
            except: pass

    # Source 2: Meta Tags
    meta_mappings = [
        ("og:price:amount", 8, "current"),
        ("product:price:amount", 8, "current"),
        ("product:sale_price:amount", 9, "sale"),
        ("product:original_price:amount", 9, "regular"),
    ]

    for prop, trust, ptype in meta_mappings:
        meta = soup.find("meta", property=prop)
        if meta:
            try:
                val = float(meta.get("content"))
                candidates.append({"src": f"Meta-{prop}", "val": val, "trust": trust, "type": ptype})
            except: pass

    # Source 3: Price-specific HTML elements
    price_selectors = {
        "sale": [".sale-price", ".special-price", ".discount-price", "[class*='sale-price']", ".price--sale", ".price-sale"],
        "regular": [".regular-price", ".original-price", ".was-price", ".rrp", "[class*='was']", ".price--compare", "s", "del"],
        "current": [".price", ".product-price", "[itemprop='price']", ".current-price", "[class*='price']:not([class*='was']):not([class*='regular'])"]
    }

    for ptype, selectors in price_selectors.items():
        for selector in selectors:
            elems = soup.select(selector)
            for elem in elems:
                text = elem.get_text(strip=True)
                match = re.search(r'\$?\s?([0-9,]+\.?[0-9]*)', text)
                if match:
                    try:
                        val = float(match.group(1).replace(',', ''))
                        if 1 < val < 50000:
                            candidates.append({"src": f"HTML-{selector}", "val": val, "trust": 6, "type": ptype})
                    except: pass

    # Source 4: Regex patterns
    # Look for sale indicators near prices
    sale_pattern = r'(?:sale|now|special|save|was\s*\$[0-9,]+\.?[0-9]*\s*now)\s*\$?\s?([0-9,]+\.?[0-9]*)'
    regular_pattern = r'(?:was|rrp|regular|original|msrp)\s*\$?\s?([0-9,]+\.?[0-9]*)'

    for match in re.findall(sale_pattern, body_text, re.IGNORECASE):
        try:
            val = float(match.replace(',', ''))
            if 1 < val < 50000:
                candidates.append({"src": "Regex-Sale", "val": val, "trust": 4, "type": "sale"})
        except: pass

    for match in re.findall(regular_pattern, body_text, re.IGNORECASE):
        try:
            val = float(match.replace(',', ''))
            if 1 < val < 50000:
                candidates.append({"src": "Regex-Regular", "val": val, "trust": 4, "type": "regular"})
        except: pass

    # General price fallback
    general_prices = re.findall(r'\$\s?([0-9,]+\.[0-9]{2})', body_text)
    for match in general_prices[:10]:  # Limit to first 10
        try:
            val = float(match.replace(',', ''))
            if 10 < val < 50000:
                candidates.append({"src": "Visual", "val": val, "trust": 3, "type": "current"})
        except: pass

    # --- PRICE VERDICT ---
    if not candidates:
        return None, None, None

    # Separate by type and score
    type_scores = {"regular": {}, "sale": {}, "current": {}}

    for c in candidates:
        ptype = c['type']
        val = c['val']
        type_scores[ptype][val] = type_scores[ptype].get(val, 0) + c['trust']

    # Pick winners for each type
    def pick_winner(scores):
        if not scores:
            return None
        return max(scores, key=scores.get)

    regular_price = pick_winner(type_scores["regular"])
    sale_price = pick_winner(type_scores["sale"])
    current_price = pick_winner(type_scores["current"])

    # Logic: if we have both regular and sale, current should be sale
    if sale_price and regular_price and sale_price < regular_price:
        current_price = sale_price
    elif sale_price and not regular_price:
        current_price = sale_price
    elif current_price is None:
        current_price = sale_price or regular_price

    # Log verdict
    log_parts = []
    if regular_price: log_parts.append(f"Regular: ${regular_price}")
    if sale_price: log_parts.append(f"Sale: ${sale_price}")
    if current_price: log_parts.append(f"Current: ${current_price}")
    logger.info(f"   Price Verdict: {' | '.join(log_parts) or 'No price found'}")

    return regular_price, sale_price, current_price


# --- JSON-LD EXTRACTION ---

def extract_json_ld(soup: BeautifulSoup) -> Dict:
    """Extract structured data from JSON-LD scripts."""
    data = {}
    scripts = soup.find_all('script', type='application/ld+json')

    for script in scripts:
        try:
            content = script.string
            if not content:
                continue

            js = json.loads(content)
            items = js if isinstance(js, list) else [js]

            # Handle @graph structure
            for item in items:
                if '@graph' in item:
                    items.extend(item['@graph'])

            for item in items:
                itype = item.get('@type')
                if isinstance(itype, list):
                    itype = itype[0]

                if itype in ['Product', 'ItemPage', 'IndividualProduct']:
                    # Basic info
                    if 'name' in item:
                        data['name'] = item['name']
                    if 'description' in item:
                        data['description'] = item['description']
                    if 'brand' in item:
                        brand = item['brand']
                        data['brand'] = brand.get('name') if isinstance(brand, dict) else brand
                    if 'image' in item:
                        img = item['image']
                        if isinstance(img, list):
                            img = img[0]
                        data['image_url'] = img.get('url') if isinstance(img, dict) else img
                    if 'sku' in item:
                        data['sku'] = item['sku']
                    if 'mpn' in item:
                        data['mpn'] = item['mpn']

                    # Offers
                    if 'offers' in item:
                        data['offers'] = item['offers']

                    # Weight & Dimensions
                    if 'weight' in item:
                        data['weight'] = item['weight']
                    for dim in ['depth', 'width', 'height']:
                        if dim in item:
                            data[dim] = item[dim]
        except:
            continue

    return data


# --- DESCRIPTION EXTRACTION ---

BANNED_PHRASES = [
    "login", "password", "cart", "checkout", "loading", "rights reserved",
    "privacy policy", "terms", "newsletter", "click here", "sign up",
    "subscribe", "cookie", "accept all"
]

def clean_text(text: str) -> Optional[str]:
    if not text:
        return None
    return re.sub(r'\s+', ' ', text).strip()

def validate_description(text: str, title: str) -> Optional[str]:
    if not text:
        return None

    clean = clean_text(text)
    low = clean.lower()

    if len(clean) < 50:
        return None
    if any(b in low for b in BANNED_PHRASES):
        return None
    if title and title.lower() in low and len(clean) < len(title) + 30:
        return None

    return clean

def get_best_description(soup: BeautifulSoup, title: str, json_desc: Optional[str]) -> str:
    # 1. JSON-LD
    if validate_description(json_desc, title):
        return json_desc[:2000]

    # 2. Meta tag
    meta = soup.find("meta", property="og:description")
    if meta:
        val = validate_description(meta.get("content"), title)
        if val:
            return val[:2000]

    # 3. Product description selectors
    selectors = [
        ".product-description", "#product-description",
        ".description", "#description",
        ".product-details", ".product-info",
        "[itemprop='description']", ".tab-content",
        ".woocommerce-product-details__short-description"
    ]

    for sel in selectors:
        elem = soup.select_one(sel)
        if elem:
            val = validate_description(elem.get_text(" ", strip=True), title)
            if val:
                return val[:2000]

    # 4. Best paragraph fallback
    best_p = ""
    for p in soup.find_all('p'):
        text = p.get_text().strip()
        if len(text) > len(best_p) and len(text) < 2000:
            if validate_description(text, title):
                best_p = text

    return best_p if best_p else "Description unavailable."


# --- LLM FALLBACK (Gemini) ---

async def llm_extract_specs(body_text: str, name: str) -> Dict:
    """Use Gemini to extract specs when structured data is unavailable."""
    if not GEMINI_API_KEY:
        return {}

    # Truncate body text to avoid token limits
    text_sample = body_text[:8000]

    prompt = f"""Analyze this product page text and extract specifications. Product: "{name}"

Text:
{text_sample}

Return ONLY valid JSON with these fields (use null if not found):
{{
  "weight_kg": <number or null>,
  "weight_raw": "<original text like '25kg' or null>",
  "dimensions_raw": "<like '120 x 80 x 45cm' or null>",
  "vehicle_compatibility": ["<vehicle 1>", "<vehicle 2>"] or [],
  "sku": "<part number or null>",
  "availability": "<in_stock|pre_order|sold_out|unknown>"
}}

Only return the JSON object, nothing else."""

    try:
        response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent?key={GEMINI_API_KEY}",
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.1, "maxOutputTokens": 500}
            },
            timeout=30
        )

        if response.status_code == 200:
            result = response.json()
            text = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '')

            # Parse JSON from response
            json_match = re.search(r'\{[\s\S]*\}', text)
            if json_match:
                return json.loads(json_match.group())
    except Exception as e:
        logger.warning(f"   LLM extraction failed: {e}")

    return {}


# --- CORE WORKER ---

async def process_product(sem: asyncio.Semaphore, browser, row: Dict, is_daily_update: bool = False):
    """Process a single product URL."""
    async with sem:
        url = row['url']
        pid = row['product_id']
        source_id = row['id']
        product_data = row.get('products', {}) or {}
        specs_locked = product_data.get('specs_locked', False)

        logger.info(f"Checking: {url}")

        # Auto-create product if needed
        if pid is None:
            try:
                new_prod = supabase.table("products").insert({
                    "name": "Scanning...",
                    "is_approved": False,
                    "category": "Uncategorized"
                }).execute()
                pid = new_prod.data[0]['id']
                supabase.table("product_sources").update({"product_id": pid}).eq("id", source_id).execute()
                logger.info(f"   Created new product: {pid}")
            except Exception as e:
                logger.error(f"   DB error: {e}")
                return

        try:
            page = await browser.new_page(user_agent=random.choice(USER_AGENTS))
            await page.goto(url, timeout=60000, wait_until="domcontentloaded")
            await asyncio.sleep(3)

            # Click expanders to reveal hidden content
            expander_patterns = [
                "text=/spec/i", "text=/dimension/i", "text=/description/i",
                "text=/detail/i", "text=/more info/i", "text=/show more/i",
                "text=/features/i", "text=/technical/i", "text=/compatibility/i"
            ]

            for pattern in expander_patterns:
                try:
                    await page.locator(pattern).first.click(timeout=300)
                    await asyncio.sleep(0.3)
                except:
                    pass

            html = await page.content()
            body_text = await page.inner_text("body")
            soup = BeautifulSoup(html, 'html.parser')

            # --- EXTRACTION ---
            specs = ProductSpecs()
            json_data = extract_json_ld(soup)

            # Name
            specs.name = json_data.get('name')
            if not specs.name:
                og_title = soup.find("meta", property="og:title")
                specs.name = og_title.get("content") if og_title else None
            if not specs.name and soup.title:
                specs.name = soup.title.string

            # Prices (ALWAYS extract for daily updates)
            specs.regular_price, specs.sale_price, specs.current_price = extract_prices(soup, body_text, json_data)

            # Availability (ALWAYS extract for daily updates)
            specs.availability = extract_availability(soup, body_text, json_data)

            # --- FULL EXTRACTION (Only on initial scrape or if not locked) ---
            if not is_daily_update or not specs_locked:
                # Description
                specs.description = get_best_description(soup, specs.name, json_data.get('description'))

                # Image
                specs.image_url = json_data.get('image_url')
                if not specs.image_url:
                    og_img = soup.find("meta", property="og:image")
                    specs.image_url = og_img.get("content") if og_img else None

                # Brand
                specs.brand = json_data.get('brand')
                if not specs.brand:
                    brand_elem = soup.select_one("[itemprop='brand'], .brand, .product-brand")
                    if brand_elem:
                        specs.brand = brand_elem.get_text(strip=True)

                # Weight (Critical for GVM)
                specs.weight_kg, specs.weight_raw = extract_weight(soup, body_text, json_data)
                if specs.weight_kg:
                    logger.info(f"   Weight: {specs.weight_kg}kg ({specs.weight_raw})")

                # Dimensions
                specs.dimensions_l, specs.dimensions_w, specs.dimensions_h, specs.dimensions_raw = extract_dimensions(soup, body_text, json_data)
                if specs.dimensions_raw:
                    logger.info(f"   Dimensions: {specs.dimensions_raw}")

                # Vehicle Compatibility
                specs.vehicle_compatibility, specs.is_universal = extract_vehicle_compatibility(soup, body_text, specs.name or "")
                if specs.vehicle_compatibility:
                    logger.info(f"   Vehicles: {', '.join(specs.vehicle_compatibility[:5])}...")
                elif specs.is_universal:
                    logger.info(f"   Vehicle: Universal fit")

                # SKU
                specs.sku = extract_sku(soup, body_text, json_data)
                if specs.sku:
                    logger.info(f"   SKU: {specs.sku}")

                # Category inference
                specs.category = infer_category(specs.name or "", specs.description or "")
                logger.info(f"   Category: {specs.category}")

                # LLM fallback for missing critical data
                if not specs.weight_kg or not specs.dimensions_raw:
                    logger.info("   Trying LLM extraction for missing specs...")
                    llm_data = await llm_extract_specs(body_text, specs.name or "")

                    if not specs.weight_kg and llm_data.get('weight_kg'):
                        specs.weight_kg = llm_data['weight_kg']
                        specs.weight_raw = llm_data.get('weight_raw')
                        logger.info(f"   LLM Weight: {specs.weight_kg}kg")

                    if not specs.dimensions_raw and llm_data.get('dimensions_raw'):
                        specs.dimensions_raw = llm_data['dimensions_raw']
                        logger.info(f"   LLM Dimensions: {specs.dimensions_raw}")

                    if not specs.sku and llm_data.get('sku'):
                        specs.sku = llm_data['sku']

                    if not specs.vehicle_compatibility and llm_data.get('vehicle_compatibility'):
                        specs.vehicle_compatibility = llm_data['vehicle_compatibility']

            await page.close()

            # --- SAVE TO DATABASE ---
            now = datetime.now().isoformat()

            if is_daily_update and specs_locked:
                # Daily update: Only update price and availability
                update_data = {
                    "updated_at": now
                }
                if specs.current_price:
                    update_data["price"] = specs.current_price
                    if specs.regular_price:
                        update_data["regular_price"] = specs.regular_price
                    if specs.sale_price:
                        update_data["sale_price"] = specs.sale_price
                update_data["availability"] = specs.availability

                logger.info(f"   Daily update: price=${specs.current_price}, availability={specs.availability}")
            else:
                # Full update
                update_data = {
                    "name": (specs.name or "Unknown")[:255],
                    "description": specs.description,
                    "category": specs.category,
                    "updated_at": now,
                    "last_full_scrape": now,
                    "availability": specs.availability
                }

                if specs.image_url:
                    update_data["image_url"] = specs.image_url
                if specs.current_price:
                    update_data["price"] = specs.current_price
                if specs.regular_price:
                    update_data["regular_price"] = specs.regular_price
                if specs.sale_price:
                    update_data["sale_price"] = specs.sale_price
                if specs.weight_kg:
                    update_data["weight_kg"] = specs.weight_kg
                if specs.weight_raw:
                    update_data["weight_raw"] = specs.weight_raw
                if specs.dimensions_l:
                    update_data["dimensions_l"] = specs.dimensions_l
                if specs.dimensions_w:
                    update_data["dimensions_w"] = specs.dimensions_w
                if specs.dimensions_h:
                    update_data["dimensions_h"] = specs.dimensions_h
                if specs.dimensions_raw:
                    update_data["dimensions_raw"] = specs.dimensions_raw
                if specs.vehicle_compatibility:
                    update_data["vehicle_compatibility"] = specs.vehicle_compatibility
                if specs.is_universal:
                    update_data["is_universal"] = specs.is_universal
                if specs.sku:
                    update_data["sku"] = specs.sku
                if specs.brand:
                    update_data["brand"] = specs.brand

            supabase.table("products").update(update_data).eq("id", pid).execute()

            # Update source tracking
            if specs.current_price:
                supabase.table("product_sources").update({
                    "last_price": specs.current_price,
                    "last_checked": "now()"
                }).eq("id", source_id).execute()

                # Price history
                supabase.table("price_history").insert({
                    "product_id": pid,
                    "price": specs.current_price
                }).execute()

            logger.info(f"   Saved successfully")

        except Exception as e:
            logger.error(f"   Scrape error: {e}")


async def main():
    """Main entry point."""
    import sys

    # Check for daily update mode
    is_daily_update = "--daily" in sys.argv or os.environ.get("DAILY_UPDATE") == "true"

    if is_daily_update:
        logger.info("DAILY UPDATE MODE - Only updating prices and availability")
    else:
        logger.info("FULL EXTRACTION MODE - Extracting all product specs")

    logger.info("Fetching product sources...")
    response = supabase.table("product_sources").select("*, products(*)").execute()
    sources = response.data

    if not sources:
        logger.info("No products to process")
        return

    logger.info(f"Processing {len(sources)} products...")

    sem = asyncio.Semaphore(CONCURRENCY)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        tasks = [process_product(sem, browser, row, is_daily_update) for row in sources]
        await asyncio.gather(*tasks)
        await browser.close()

    logger.info("Complete!")


if __name__ == "__main__":
    asyncio.run(main())
