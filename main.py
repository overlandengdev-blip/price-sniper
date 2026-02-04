"""
Product Specification Extractor v3.0
=====================================
A robust, production-ready scraping system for 4WD/camping product data.
Features: Price anomaly detection, confidence scoring, comprehensive history.

Database Schema Requirements (Supabase) - See migrations/002_phase2.sql
"""

import asyncio
import os
import random
import re
import json
import logging
import hashlib
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass, field, asdict
from enum import Enum
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from supabase import create_client, Client
from bs4 import BeautifulSoup
import requests

# --- CONFIGURATION ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # Optional: for instant notifications

CONCURRENCY = 5  # Increased for better throughput
MAX_RETRIES = 3
ANOMALY_THRESHOLD = 0.30  # 30% price change triggers recheck
DRAMATIC_DROP_THRESHOLD = 0.70  # 70% drop is suspicious

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

if not all([SUPABASE_URL, SUPABASE_KEY]):
    logger.error("Missing Supabase credentials.")
    exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
]


# --- ENUMS & CONSTANTS ---

class ExtractionMethod(Enum):
    JSON_LD = "json_ld"
    META_TAGS = "meta_tags"
    HTML_SELECTORS = "html_selectors"
    REGEX = "regex"
    LLM = "llm"
    FALLBACK = "fallback"


class PriceValidation(Enum):
    VALID = "valid"
    ANOMALY_DETECTED = "anomaly_detected"
    DRAMATIC_DROP = "dramatic_drop"
    RECHECK_PASSED = "recheck_passed"
    RECHECK_FAILED = "recheck_failed"


# Trust scores for different extraction methods
TRUST_SCORES = {
    ExtractionMethod.JSON_LD: 10,
    ExtractionMethod.META_TAGS: 8,
    ExtractionMethod.HTML_SELECTORS: 6,
    ExtractionMethod.REGEX: 4,
    ExtractionMethod.LLM: 5,
    ExtractionMethod.FALLBACK: 2,
}


# --- DATA CLASSES ---

@dataclass
class PriceRecord:
    """Enhanced price record with full metadata."""
    price: float
    confidence: float  # 0.0 to 1.0
    extraction_method: str
    source_selector: Optional[str] = None
    validation_status: str = "valid"
    is_sale: bool = False
    discount_percentage: Optional[float] = None
    historical_high: Optional[float] = None
    historical_low: Optional[float] = None
    price_trend: Optional[str] = None  # "rising", "falling", "stable"
    checked_at: Optional[str] = None
    raw_text: Optional[str] = None


@dataclass
class ProductSpecs:
    """Complete product specification container with confidence tracking."""
    # Core
    name: Optional[str] = None
    name_confidence: float = 0.0
    description: Optional[str] = None
    description_confidence: float = 0.0
    image_url: Optional[str] = None
    image_urls: List[str] = field(default_factory=list)  # Multiple images
    brand: Optional[str] = None

    # Pricing (Enhanced)
    regular_price: Optional[float] = None
    sale_price: Optional[float] = None
    current_price: Optional[float] = None
    price_confidence: float = 0.0
    price_extraction_method: str = ""
    price_validation: str = "valid"
    is_on_sale: bool = False
    discount_percentage: Optional[float] = None
    historical_high: Optional[float] = None
    historical_low: Optional[float] = None

    # Engineering (Critical for GVM)
    weight_kg: Optional[float] = None
    weight_raw: Optional[str] = None
    weight_confidence: float = 0.0
    dimensions_l: Optional[float] = None
    dimensions_w: Optional[float] = None
    dimensions_h: Optional[float] = None
    dimensions_raw: Optional[str] = None
    dimensions_confidence: float = 0.0

    # Compatibility
    vehicle_compatibility: List[str] = field(default_factory=list)
    is_universal: bool = False

    # Catalog
    category: Optional[str] = None
    sku: Optional[str] = None
    upc: Optional[str] = None  # NEW: Universal Product Code
    ean: Optional[str] = None  # NEW: European Article Number
    mpn: Optional[str] = None  # NEW: Manufacturer Part Number
    availability: str = "unknown"

    # Reviews (NEW)
    rating: Optional[float] = None
    review_count: Optional[int] = None

    # Variants (NEW)
    variants: List[Dict] = field(default_factory=list)
    has_variants: bool = False

    # Metadata
    extraction_sources: Dict[str, str] = field(default_factory=dict)
    overall_confidence: float = 0.0
    scrape_duration_ms: int = 0


# --- RETRY & RATE LIMITING ---

class RateLimitDetector:
    """Detect and handle rate limiting from websites."""

    RATE_LIMIT_INDICATORS = [
        "rate limit", "too many requests", "429", "please try again",
        "access denied", "blocked", "captcha", "verify you are human",
        "unusual traffic", "automated access"
    ]

    @staticmethod
    def is_rate_limited(html: str, status_code: int = 200) -> bool:
        if status_code == 429:
            return True
        if status_code == 403:
            return True

        html_lower = html.lower()
        return any(indicator in html_lower for indicator in RateLimitDetector.RATE_LIMIT_INDICATORS)

    @staticmethod
    def get_retry_delay(attempt: int) -> float:
        """Exponential backoff: 2s, 4s, 8s, 16s..."""
        return min(2 ** (attempt + 1), 60)  # Cap at 60 seconds


async def retry_with_backoff(func, max_retries: int = MAX_RETRIES, *args, **kwargs):
    """Execute function with exponential backoff retry."""
    last_exception = None

    for attempt in range(max_retries):
        try:
            return await func(*args, **kwargs)
        except (PlaywrightTimeout, Exception) as e:
            last_exception = e
            delay = RateLimitDetector.get_retry_delay(attempt)
            logger.warning(f"   Attempt {attempt + 1}/{max_retries} failed: {str(e)[:50]}. Retrying in {delay}s...")
            await asyncio.sleep(delay)

    raise last_exception


# --- PRICE ANOMALY DETECTION ---

class PriceAnomalyDetector:
    """Detect suspicious price changes and validate."""

    @staticmethod
    async def get_price_history(product_id: int, days: int = 30) -> List[Dict]:
        """Fetch recent price history for a product."""
        try:
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            response = supabase.table("price_history") \
                .select("*") \
                .eq("product_id", product_id) \
                .gte("checked_at", cutoff) \
                .order("checked_at", desc=True) \
                .limit(100) \
                .execute()
            return response.data or []
        except:
            return []

    @staticmethod
    def calculate_statistics(history: List[Dict]) -> Dict:
        """Calculate price statistics from history."""
        if not history:
            return {}

        prices = [h['price'] for h in history if h.get('price')]
        if not prices:
            return {}

        return {
            'high': max(prices),
            'low': min(prices),
            'average': sum(prices) / len(prices),
            'latest': prices[0] if prices else None,
            'count': len(prices)
        }

    @staticmethod
    def detect_anomaly(new_price: float, history: List[Dict]) -> Tuple[PriceValidation, Dict]:
        """
        Detect if new price is anomalous compared to history.
        Returns: (validation_status, metadata)
        """
        if not history:
            return PriceValidation.VALID, {'reason': 'no_history'}

        stats = PriceAnomalyDetector.calculate_statistics(history)
        if not stats:
            return PriceValidation.VALID, {'reason': 'no_stats'}

        latest = stats['latest']
        avg = stats['average']

        if latest and latest > 0:
            change_pct = abs(new_price - latest) / latest

            # Dramatic drop (e.g., $200 -> $30)
            if new_price < latest and change_pct >= DRAMATIC_DROP_THRESHOLD:
                return PriceValidation.DRAMATIC_DROP, {
                    'reason': 'dramatic_drop',
                    'previous': latest,
                    'change_percent': round(change_pct * 100, 1),
                    'threshold': DRAMATIC_DROP_THRESHOLD * 100
                }

            # General anomaly
            if change_pct >= ANOMALY_THRESHOLD:
                return PriceValidation.ANOMALY_DETECTED, {
                    'reason': 'significant_change',
                    'previous': latest,
                    'change_percent': round(change_pct * 100, 1),
                    'threshold': ANOMALY_THRESHOLD * 100
                }

        return PriceValidation.VALID, {
            'reason': 'within_threshold',
            'previous': latest,
            'average': round(avg, 2)
        }

    @staticmethod
    def calculate_sale_info(current_price: float, stats: Dict) -> Tuple[bool, Optional[float]]:
        """Determine if item is on sale based on historical high."""
        if not stats or 'high' not in stats:
            return False, None

        historical_high = stats['high']
        if historical_high and historical_high > current_price:
            discount = ((historical_high - current_price) / historical_high) * 100
            if discount >= 5:  # At least 5% off to count as sale
                return True, round(discount, 1)

        return False, None


# --- CONFIDENCE SCORING ---

class ConfidenceCalculator:
    """Calculate confidence scores for extracted data."""

    @staticmethod
    def calculate_price_confidence(candidates: List[Dict]) -> Tuple[float, str]:
        """
        Calculate confidence score for price extraction.
        Returns: (confidence 0-1, winning method)
        """
        if not candidates:
            return 0.0, "none"

        # Group by price value
        price_votes = {}
        for c in candidates:
            price = c['val']
            if price not in price_votes:
                price_votes[price] = {'total_trust': 0, 'sources': [], 'methods': []}
            price_votes[price]['total_trust'] += c['trust']
            price_votes[price]['sources'].append(c['src'])
            price_votes[price]['methods'].append(c.get('method', 'unknown'))

        if not price_votes:
            return 0.0, "none"

        # Find winner
        winner_price = max(price_votes, key=lambda p: price_votes[p]['total_trust'])
        winner_data = price_votes[winner_price]

        # Calculate confidence
        max_possible_trust = TRUST_SCORES[ExtractionMethod.JSON_LD] * 3  # Multiple JSON sources
        confidence = min(winner_data['total_trust'] / max_possible_trust, 1.0)

        # Boost confidence if multiple sources agree
        if len(winner_data['sources']) >= 2:
            confidence = min(confidence * 1.2, 1.0)

        # Primary method
        primary_method = winner_data['methods'][0] if winner_data['methods'] else "unknown"

        return round(confidence, 2), primary_method

    @staticmethod
    def calculate_overall_confidence(specs: ProductSpecs) -> float:
        """Calculate overall extraction confidence."""
        weights = {
            'price': 0.3,
            'name': 0.2,
            'description': 0.15,
            'weight': 0.15,
            'dimensions': 0.1,
            'sku': 0.1
        }

        scores = {
            'price': specs.price_confidence if specs.current_price else 0,
            'name': specs.name_confidence if specs.name else 0,
            'description': specs.description_confidence if specs.description else 0,
            'weight': specs.weight_confidence if specs.weight_kg else 0,
            'dimensions': specs.dimensions_confidence if specs.dimensions_raw else 0,
            'sku': 0.8 if specs.sku else 0  # Fixed score for SKU presence
        }

        total = sum(scores[k] * weights[k] for k in weights)
        return round(total, 2)


# --- ENHANCED EXTRACTORS ---

def extract_product_identifiers(soup: BeautifulSoup, body_text: str, json_data: Dict) -> Dict[str, Optional[str]]:
    """Extract UPC, EAN, MPN, and other product identifiers."""
    identifiers = {
        'sku': None,
        'upc': None,
        'ean': None,
        'mpn': None,
        'gtin': None
    }

    # From JSON-LD
    for key in ['sku', 'mpn', 'gtin', 'gtin13', 'gtin8', 'gtin14', 'productID']:
        if key in json_data:
            value = str(json_data[key])
            if key.startswith('gtin'):
                if len(value) == 12:
                    identifiers['upc'] = value
                elif len(value) == 13:
                    identifiers['ean'] = value
                identifiers['gtin'] = value
            elif key == 'mpn':
                identifiers['mpn'] = value
            elif key == 'sku':
                identifiers['sku'] = value

    # From meta tags
    meta_patterns = [
        ('upc', r'product:upc'),
        ('ean', r'product:ean'),
        ('sku', r'product:sku'),
        ('mpn', r'product:mpn'),
    ]

    for id_type, pattern in meta_patterns:
        if not identifiers[id_type]:
            meta = soup.find('meta', property=re.compile(pattern, re.I))
            if meta and meta.get('content'):
                identifiers[id_type] = meta.get('content')[:50]

    # From HTML
    id_selectors = {
        'sku': ['.sku', '[itemprop="sku"]', '[data-sku]', '.product-sku'],
        'upc': ['.upc', '[itemprop="gtin12"]', '[data-upc]'],
        'mpn': ['.mpn', '[itemprop="mpn"]', '[data-mpn]'],
    }

    for id_type, selectors in id_selectors.items():
        if not identifiers[id_type]:
            for selector in selectors:
                elem = soup.select_one(selector)
                if elem:
                    text = elem.get_text(strip=True)
                    if text and len(text) < 50:
                        identifiers[id_type] = text
                        break

    # Regex patterns for UPC/EAN in text
    if not identifiers['upc']:
        upc_match = re.search(r'\b(\d{12})\b', body_text)
        if upc_match:
            identifiers['upc'] = upc_match.group(1)

    if not identifiers['ean']:
        ean_match = re.search(r'\b(\d{13})\b', body_text)
        if ean_match:
            identifiers['ean'] = ean_match.group(1)

    return identifiers


def extract_reviews(soup: BeautifulSoup, json_data: Dict) -> Tuple[Optional[float], Optional[int]]:
    """Extract rating and review count."""
    rating = None
    review_count = None

    # From JSON-LD
    if 'aggregateRating' in json_data:
        agg = json_data['aggregateRating']
        if isinstance(agg, dict):
            try:
                rating = float(agg.get('ratingValue', 0))
                review_count = int(agg.get('reviewCount', agg.get('ratingCount', 0)))
            except:
                pass

    # From HTML
    if not rating:
        rating_elem = soup.select_one('[itemprop="ratingValue"], .rating-value, .star-rating')
        if rating_elem:
            text = rating_elem.get_text(strip=True)
            match = re.search(r'(\d+\.?\d*)', text)
            if match:
                try:
                    rating = float(match.group(1))
                    if rating > 5:  # Likely percentage
                        rating = rating / 20  # Convert to 5-star scale
                except:
                    pass

    if not review_count:
        count_elem = soup.select_one('[itemprop="reviewCount"], .review-count, .reviews-count')
        if count_elem:
            text = count_elem.get_text(strip=True)
            match = re.search(r'(\d+)', text.replace(',', ''))
            if match:
                try:
                    review_count = int(match.group(1))
                except:
                    pass

    return rating, review_count


def extract_multiple_images(soup: BeautifulSoup, json_data: Dict) -> List[str]:
    """Extract all product images."""
    images = set()

    # From JSON-LD
    if 'image' in json_data:
        img_data = json_data['image']
        if isinstance(img_data, list):
            for img in img_data:
                if isinstance(img, dict):
                    images.add(img.get('url', ''))
                else:
                    images.add(str(img))
        elif isinstance(img_data, dict):
            images.add(img_data.get('url', ''))
        else:
            images.add(str(img_data))

    # From meta tags
    og_images = soup.find_all('meta', property='og:image')
    for meta in og_images:
        if meta.get('content'):
            images.add(meta['content'])

    # From HTML gallery
    gallery_selectors = [
        '.product-gallery img',
        '.product-images img',
        '[data-gallery] img',
        '.thumbnail-images img',
        '.product-thumbnails img',
        '.woocommerce-product-gallery img'
    ]

    for selector in gallery_selectors:
        for img in soup.select(selector)[:10]:  # Limit to 10
            src = img.get('src') or img.get('data-src') or img.get('data-lazy-src')
            if src and not src.startswith('data:'):
                images.add(src)

    # Clean and filter
    valid_images = []
    for img in images:
        if img and isinstance(img, str) and len(img) > 10:
            if img.startswith('//'):
                img = 'https:' + img
            if img.startswith('http'):
                valid_images.append(img)

    return valid_images[:10]  # Return max 10 images


def extract_variants(soup: BeautifulSoup, json_data: Dict, body_text: str) -> List[Dict]:
    """Extract product variants (colors, sizes, etc.)."""
    variants = []

    # From JSON-LD offers
    offers = json_data.get('offers', [])
    if isinstance(offers, list) and len(offers) > 1:
        for offer in offers[:20]:  # Limit to 20 variants
            if isinstance(offer, dict):
                variant = {
                    'name': offer.get('name'),
                    'price': None,
                    'sku': offer.get('sku'),
                    'availability': offer.get('availability', '').split('/')[-1]
                }
                try:
                    variant['price'] = float(offer.get('price', 0))
                except:
                    pass
                if variant['price'] or variant['name']:
                    variants.append(variant)

    # From HTML variant selectors
    if not variants:
        variant_selectors = [
            'select[name*="variant"] option',
            'select[name*="size"] option',
            'select[name*="color"] option',
            '.variant-option',
            '.product-variant',
            '[data-variant]'
        ]

        for selector in variant_selectors:
            for elem in soup.select(selector)[:20]:
                text = elem.get_text(strip=True)
                if text and text.lower() not in ['select', 'choose', '--']:
                    price = None
                    price_match = re.search(r'\$(\d+\.?\d*)', text)
                    if price_match:
                        try:
                            price = float(price_match.group(1))
                        except:
                            pass
                    variants.append({
                        'name': re.sub(r'\s*\$[\d.]+', '', text).strip(),
                        'price': price,
                        'sku': elem.get('data-sku') or elem.get('value'),
                        'availability': 'unknown'
                    })
            if variants:
                break

    return variants


# --- CATEGORY INFERENCE (Enhanced) ---

CATEGORY_KEYWORDS = {
    "Suspension": [
        "leaf spring", "coil spring", "shock absorber", "strut", "lift kit",
        "suspension", "sway bar", "control arm", "panhard rod", "castor kit",
        "airbag", "coilover", "shock", "spring"
    ],
    "Bullbars & Protection": [
        "bullbar", "bull bar", "nudge bar", "bash plate", "skid plate",
        "rock slider", "side step", "rear bar", "recovery point", "bumper",
        "grille guard", "brush guard"
    ],
    "Roof Racks & Storage": [
        "roof rack", "roof bar", "roof basket", "cargo barrier", "drawer",
        "cargo box", "roof pod", "awning", "roof top tent", "rtt", "canopy",
        "roof tray", "platform"
    ],
    "Lighting": [
        "led bar", "light bar", "driving light", "spot light", "flood light",
        "work light", "headlight", "tail light", "beacon", "lumen", "led pod"
    ],
    "Recovery Gear": [
        "recovery kit", "snatch strap", "kinetic rope", "shackle", "winch",
        "recovery board", "maxtrax", "sand track", "hi-lift", "high lift",
        "recovery point", "bow shackle", "snatch block"
    ],
    "Electrical": [
        "battery", "dual battery", "dc-dc", "inverter", "solar panel",
        "fuse box", "wiring harness", "anderson plug", "usb charger",
        "bcdc", "charger", "isolator"
    ],
    "Fuel & Water": [
        "fuel tank", "jerry can", "water tank", "aux tank", "long range tank",
        "water filter", "pump", "bladder", "auxiliary tank"
    ],
    "Camping & Touring": [
        "swag", "tent", "sleeping bag", "camp chair", "table", "fridge",
        "cooler", "stove", "camp kitchen", "shower", "toilet", "porta potti",
        "annexe", "mattress"
    ],
    "Tyres & Wheels": [
        "tyre", "tire", "wheel", "rim", "alloy", "steel wheel", "beadlock",
        "spare wheel", "tyre carrier", "all terrain", "mud terrain"
    ],
    "Exhaust & Performance": [
        "exhaust", "snorkel", "air intake", "turbo", "intercooler",
        "catch can", "egr", "dpf", "tuner", "chip", "cold air intake"
    ],
    "Towing": [
        "tow bar", "towbar", "tow ball", "trailer plug", "trailer connector",
        "weight distribution", "wdh", "anti-sway", "caravan mirror"
    ],
    "Communication": [
        "uhf", "cb radio", "antenna", "gps", "satellite", "spot messenger",
        "two way radio", "gmrs"
    ]
}

def infer_category(name: str, description: str = "") -> Tuple[str, float]:
    """Infer product category with confidence score."""
    if not name:
        return "Uncategorized", 0.0

    text = f"{name} {description}".lower()
    scores = {}

    for category, keywords in CATEGORY_KEYWORDS.items():
        score = 0
        for kw in keywords:
            if kw in text:
                # Boost score if keyword is in title
                if kw in name.lower():
                    score += 2
                else:
                    score += 1
        if score > 0:
            scores[category] = score

    if scores:
        best_category = max(scores, key=scores.get)
        # Confidence based on score relative to max possible
        confidence = min(scores[best_category] / 5, 1.0)
        return best_category, round(confidence, 2)

    return "Uncategorized", 0.0


# --- VEHICLE COMPATIBILITY (from Phase 1) ---

VEHICLE_PATTERNS = [
    r"(toyota\s+)?(hilux|hiace|landcruiser|land\s*cruiser|prado|fortuner|4runner)\s*(n\d+|[0-9]{2,3}\s*series)?",
    r"(lc|landcruiser)\s*(70|76|78|79|80|100|105|200|300)\s*series?",
    r"n70|n80|gun125|gun126|kun26|ln106",
    r"(nissan\s+)?(patrol|navara|pathfinder|x-trail|y61|y62|gu|gq|d22|d23|d40|np300)",
    r"(ford\s+)?(ranger|everest|f[0-9]{3}|px1|px2|px3|pxii|pxiii)",
    r"(mazda\s+)?(bt-50|bt50|cx-[0-9])",
    r"(mitsubishi\s+)?(triton|pajero|challenger|outlander|mq|mr|ml|mn)",
    r"(isuzu\s+)?(d-?max|dmax|mu-?x|mux)",
    r"(holden\s+)?(colorado|rodeo|trailblazer|rg|ra)",
    r"(jeep\s+)?(wrangler|gladiator|cherokee|grand\s*cherokee|jk|jl|jt|wj|wk)",
    r"(land\s*rover\s+)?(defender|discovery|range\s*rover)",
    r"(suzuki\s+)?(jimny|vitara|grand\s*vitara)",
    r"(vw|volkswagen)\s*(amarok)",
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

    for indicator in UNIVERSAL_INDICATORS:
        if indicator in text_to_search:
            is_universal = True
            break

    compat_selectors = [
        "div[class*='compatib']", "div[class*='vehicle']", "div[class*='fitment']",
        "div[class*='fit-guide']", "section[class*='vehicle']", ".vehicle-list",
        "#vehicle-compatibility", ".product-fitment", "[data-vehicle]"
    ]

    for selector in compat_selectors:
        for elem in soup.select(selector):
            text_to_search += " " + elem.get_text(" ", strip=True).lower()

    for pattern in VEHICLE_PATTERNS:
        matches = re.findall(pattern, text_to_search, re.IGNORECASE)
        for match in matches:
            if isinstance(match, tuple):
                vehicle = " ".join(m for m in match if m).strip()
            else:
                vehicle = match.strip()
            if vehicle and len(vehicle) > 2:
                vehicle = re.sub(r'\s+', ' ', vehicle).title()
                vehicles.add(vehicle)

    if not vehicles and not is_universal:
        camping_keywords = ["camp", "tent", "swag", "chair", "table", "fridge", "cooler", "stove"]
        if any(kw in text_to_search for kw in camping_keywords):
            is_universal = True

    return list(vehicles)[:20], is_universal


# --- WEIGHT EXTRACTION (Enhanced) ---

WEIGHT_PATTERNS = [
    (r"(?:weight|wt\.?|net\s*weight|gross\s*weight|shipping\s*weight|product\s*weight)[\s:]*([0-9]+(?:\.[0-9]+)?)\s*(kg|kgs|kilograms?|lbs?|pounds?|g|grams?|oz|ounces?)", ExtractionMethod.HTML_SELECTORS),
    (r"(?:^|\||\n)\s*(?:weight|mass)[\s:|\-]+([0-9]+(?:\.[0-9]+)?)\s*(kg|kgs|lbs?|g)", ExtractionMethod.REGEX),
    (r"([0-9]+(?:\.[0-9]+)?)\s*(kg|kgs|kilograms?)\b", ExtractionMethod.REGEX),
    (r"([0-9]+(?:\.[0-9]+)?)\s*(lbs?|pounds?)\b", ExtractionMethod.REGEX),
    (r"\(([0-9]+(?:\.[0-9]+)?)\s*(kg|lbs?)\)", ExtractionMethod.REGEX),
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
    return round(value, 2)

def extract_weight(soup: BeautifulSoup, body_text: str, json_data: Dict) -> Tuple[Optional[float], Optional[str], float]:
    """Extract weight with confidence score."""

    # Priority 1: JSON-LD (highest confidence)
    if 'weight' in json_data:
        weight_data = json_data['weight']
        if isinstance(weight_data, dict):
            value = weight_data.get('value')
            unit = weight_data.get('unitCode', weight_data.get('unitText', 'kg'))
            if value:
                try:
                    kg = normalize_weight_to_kg(float(value), unit)
                    return kg, f"{value} {unit}", 0.95
                except:
                    pass

    # Priority 2: Spec tables
    spec_selectors = [
        "table.specs", "table.specifications", ".product-specs table",
        "dl.specs", ".spec-list", "[class*='specification']", ".tech-specs"
    ]

    spec_text = ""
    for selector in spec_selectors:
        for elem in soup.select(selector):
            spec_text += " " + elem.get_text(" ", strip=True)

    search_text = f"{spec_text} {body_text}".lower()

    best_match = None
    best_confidence = 0.0

    for i, (pattern, method) in enumerate(WEIGHT_PATTERNS):
        matches = re.findall(pattern, search_text, re.IGNORECASE | re.MULTILINE)
        for match in matches:
            if isinstance(match, tuple) and len(match) >= 2:
                try:
                    value = float(match[0])
                    unit = match[1]
                    kg = normalize_weight_to_kg(value, unit)

                    if 0.1 <= kg <= 500:
                        # Confidence based on pattern priority
                        confidence = max(0.8 - (i * 0.1), 0.4)
                        if confidence > best_confidence:
                            best_confidence = confidence
                            best_match = (kg, f"{match[0]} {unit}")
                except:
                    continue

    if best_match:
        return best_match[0], best_match[1], best_confidence

    return None, None, 0.0


# --- DIMENSIONS EXTRACTION (Enhanced) ---

DIMENSION_PATTERNS = [
    r"(?:dimensions?|size|measurements?)[\s:]*([0-9]+(?:\.[0-9]+)?)\s*[xX\*]\s*([0-9]+(?:\.[0-9]+)?)\s*[xX\*]\s*([0-9]+(?:\.[0-9]+)?)\s*(mm|cm|m|inches?|in|\")?",
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
    if value > 100:
        return round(value / 10, 1)
    return round(value, 1)

def extract_dimensions(soup: BeautifulSoup, body_text: str, json_data: Dict) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[str], float]:
    """Extract dimensions with confidence score."""

    # Check JSON-LD first
    for dim_key in ['depth', 'width', 'height']:
        if dim_key in json_data:
            l = json_data.get('depth', json_data.get('length'))
            w = json_data.get('width')
            h = json_data.get('height')
            try:
                if isinstance(l, dict): l = float(l.get('value', 0))
                if isinstance(w, dict): w = float(w.get('value', 0))
                if isinstance(h, dict): h = float(h.get('value', 0))
                if l and w and h:
                    return float(l), float(w), float(h), f"{l} x {w} x {h}", 0.95
            except:
                pass
            break

    spec_text = ""
    for selector in ["table", ".specs", ".specifications", "[class*='dimension']"]:
        for elem in soup.select(selector):
            spec_text += " " + elem.get_text(" ", strip=True)

    search_text = f"{spec_text} {body_text}".lower()

    for pattern in DIMENSION_PATTERNS:
        matches = re.findall(pattern, search_text, re.IGNORECASE)
        for match in matches:
            if len(match) >= 3:
                try:
                    l, w, h = float(match[0]), float(match[1]), float(match[2])
                    unit = match[3] if len(match) > 3 else 'mm'
                    l_cm = normalize_dimension_to_cm(l, unit)
                    w_cm = normalize_dimension_to_cm(w, unit)
                    h_cm = normalize_dimension_to_cm(h, unit)
                    if all(1 <= d <= 500 for d in [l_cm, w_cm, h_cm]):
                        raw = f"{match[0]} x {match[1]} x {match[2]} {unit or 'mm'}"
                        return l_cm, w_cm, h_cm, raw, 0.75
                except:
                    continue

    return None, None, None, None, 0.0


# --- AVAILABILITY EXTRACTION ---

AVAILABILITY_MAP = {
    "in_stock": ["in stock", "in-stock", "instock", "available", "ready to ship", "ships today", "ships within", "add to cart", "buy now", "available now"],
    "pre_order": ["pre-order", "preorder", "pre order", "coming soon", "available soon", "backorder", "back-order", "back order", "notify me", "arriving"],
    "sold_out": ["sold out", "out of stock", "outofstock", "out-of-stock", "unavailable", "currently unavailable", "no longer available", "discontinued"]
}

def extract_availability(soup: BeautifulSoup, body_text: str, json_data: Dict) -> str:
    """Determine product availability status."""
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

    avail_selectors = [".availability", ".stock-status", "[class*='availability']", "[class*='stock']", ".product-availability", "[itemprop='availability']"]
    avail_text = ""
    for selector in avail_selectors:
        elem = soup.select_one(selector)
        if elem:
            avail_text += " " + elem.get_text(strip=True).lower()

    search_text = f"{avail_text} {body_text[:2000]}".lower()
    for status, keywords in AVAILABILITY_MAP.items():
        for kw in keywords:
            if kw in search_text:
                return status

    add_to_cart = soup.select_one("button[class*='cart'], input[value*='cart'], .add-to-cart")
    if add_to_cart:
        return "in_stock"

    return "unknown"


# --- ENHANCED PRICE EXTRACTION ---

def extract_prices_enhanced(soup: BeautifulSoup, body_text: str, json_data: Dict) -> Tuple[Optional[float], Optional[float], Optional[float], float, str, List[Dict]]:
    """
    Enhanced price extraction with confidence scoring.
    Returns: (regular_price, sale_price, current_price, confidence, method, all_candidates)
    """
    candidates = []

    # Source 1: JSON-LD (Highest trust)
    offers = json_data.get('offers', {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}

    if offers:
        if 'price' in offers:
            try:
                candidates.append({
                    "src": "JSON-LD", "val": float(offers['price']),
                    "trust": TRUST_SCORES[ExtractionMethod.JSON_LD],
                    "type": "current", "method": ExtractionMethod.JSON_LD.value
                })
            except: pass
        if 'lowPrice' in offers:
            try:
                candidates.append({
                    "src": "JSON-LD-Low", "val": float(offers['lowPrice']),
                    "trust": TRUST_SCORES[ExtractionMethod.JSON_LD],
                    "type": "sale", "method": ExtractionMethod.JSON_LD.value
                })
            except: pass
        if 'highPrice' in offers:
            try:
                candidates.append({
                    "src": "JSON-LD-High", "val": float(offers['highPrice']),
                    "trust": TRUST_SCORES[ExtractionMethod.JSON_LD] - 1,
                    "type": "regular", "method": ExtractionMethod.JSON_LD.value
                })
            except: pass

    # Source 2: Meta Tags
    meta_mappings = [
        ("og:price:amount", "current"),
        ("product:price:amount", "current"),
        ("product:sale_price:amount", "sale"),
        ("product:original_price:amount", "regular"),
    ]

    for prop, ptype in meta_mappings:
        meta = soup.find("meta", property=prop)
        if meta:
            try:
                val = float(meta.get("content"))
                candidates.append({
                    "src": f"Meta-{prop}", "val": val,
                    "trust": TRUST_SCORES[ExtractionMethod.META_TAGS],
                    "type": ptype, "method": ExtractionMethod.META_TAGS.value
                })
            except: pass

    # Source 3: HTML elements
    price_selectors = {
        "sale": [".sale-price", ".special-price", ".discount-price", "[class*='sale-price']", ".price--sale"],
        "regular": [".regular-price", ".original-price", ".was-price", ".rrp", "[class*='was']", ".price--compare", "s", "del"],
        "current": [".price", ".product-price", "[itemprop='price']", ".current-price"]
    }

    for ptype, selectors in price_selectors.items():
        for selector in selectors:
            for elem in soup.select(selector)[:3]:
                text = elem.get_text(strip=True)
                match = re.search(r'\$?\s?([0-9,]+\.?[0-9]*)', text)
                if match:
                    try:
                        val = float(match.group(1).replace(',', ''))
                        if 1 < val < 50000:
                            candidates.append({
                                "src": f"HTML-{selector}", "val": val,
                                "trust": TRUST_SCORES[ExtractionMethod.HTML_SELECTORS],
                                "type": ptype, "method": ExtractionMethod.HTML_SELECTORS.value,
                                "raw_text": text[:50]
                            })
                    except: pass

    # Source 4: Regex
    sale_pattern = r'(?:sale|now|special|save|was\s*\$[0-9,]+\.?[0-9]*\s*now)\s*\$?\s?([0-9,]+\.?[0-9]*)'
    regular_pattern = r'(?:was|rrp|regular|original|msrp)\s*\$?\s?([0-9,]+\.?[0-9]*)'

    for match in re.findall(sale_pattern, body_text, re.IGNORECASE)[:5]:
        try:
            val = float(match.replace(',', ''))
            if 1 < val < 50000:
                candidates.append({
                    "src": "Regex-Sale", "val": val,
                    "trust": TRUST_SCORES[ExtractionMethod.REGEX],
                    "type": "sale", "method": ExtractionMethod.REGEX.value
                })
        except: pass

    for match in re.findall(regular_pattern, body_text, re.IGNORECASE)[:5]:
        try:
            val = float(match.replace(',', ''))
            if 1 < val < 50000:
                candidates.append({
                    "src": "Regex-Regular", "val": val,
                    "trust": TRUST_SCORES[ExtractionMethod.REGEX],
                    "type": "regular", "method": ExtractionMethod.REGEX.value
                })
        except: pass

    # General price fallback
    general_prices = re.findall(r'\$\s?([0-9,]+\.[0-9]{2})', body_text)
    for match in general_prices[:10]:
        try:
            val = float(match.replace(',', ''))
            if 10 < val < 50000:
                candidates.append({
                    "src": "Visual", "val": val,
                    "trust": TRUST_SCORES[ExtractionMethod.FALLBACK],
                    "type": "current", "method": ExtractionMethod.FALLBACK.value
                })
        except: pass

    # --- VERDICT ---
    if not candidates:
        return None, None, None, 0.0, "none", []

    # Score by type
    type_scores = {"regular": {}, "sale": {}, "current": {}}
    for c in candidates:
        ptype, val = c['type'], c['val']
        type_scores[ptype][val] = type_scores[ptype].get(val, 0) + c['trust']

    def pick_winner(scores):
        return max(scores, key=scores.get) if scores else None

    regular_price = pick_winner(type_scores["regular"])
    sale_price = pick_winner(type_scores["sale"])
    current_price = pick_winner(type_scores["current"])

    # Logic
    if sale_price and regular_price and sale_price < regular_price:
        current_price = sale_price
    elif sale_price and not regular_price:
        current_price = sale_price
    elif current_price is None:
        current_price = sale_price or regular_price

    # Calculate confidence
    confidence, method = ConfidenceCalculator.calculate_price_confidence(candidates)

    return regular_price, sale_price, current_price, confidence, method, candidates


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

            for item in items:
                if '@graph' in item:
                    items.extend(item['@graph'])

            for item in items:
                itype = item.get('@type')
                if isinstance(itype, list):
                    itype = itype[0]

                if itype in ['Product', 'ItemPage', 'IndividualProduct']:
                    for key in ['name', 'description', 'sku', 'mpn', 'gtin', 'gtin13', 'gtin8']:
                        if key in item:
                            data[key] = item[key]

                    if 'brand' in item:
                        brand = item['brand']
                        data['brand'] = brand.get('name') if isinstance(brand, dict) else brand

                    if 'image' in item:
                        data['image'] = item['image']

                    if 'offers' in item:
                        data['offers'] = item['offers']

                    if 'aggregateRating' in item:
                        data['aggregateRating'] = item['aggregateRating']

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

def get_best_description(soup: BeautifulSoup, title: str, json_desc: Optional[str]) -> Tuple[str, float]:
    """Get best description with confidence score."""

    if validate_description(json_desc, title):
        return json_desc[:2000], 0.9

    meta = soup.find("meta", property="og:description")
    if meta:
        val = validate_description(meta.get("content"), title)
        if val:
            return val[:2000], 0.8

    selectors = [
        ".product-description", "#product-description", ".description",
        "#description", ".product-details", ".product-info",
        "[itemprop='description']", ".tab-content"
    ]

    for sel in selectors:
        elem = soup.select_one(sel)
        if elem:
            val = validate_description(elem.get_text(" ", strip=True), title)
            if val:
                return val[:2000], 0.7

    best_p = ""
    for p in soup.find_all('p'):
        text = p.get_text().strip()
        if len(text) > len(best_p) and len(text) < 2000:
            if validate_description(text, title):
                best_p = text

    if best_p:
        return best_p, 0.5

    return "Description unavailable.", 0.0


# --- LLM FALLBACK ---

async def llm_extract_specs(body_text: str, name: str) -> Dict:
    """Use Gemini to extract specs when structured data is unavailable."""
    if not GEMINI_API_KEY:
        return {}

    text_sample = body_text[:8000]
    prompt = f"""Analyze this product page and extract specifications. Product: "{name}"

Text:
{text_sample}

Return ONLY valid JSON:
{{
  "weight_kg": <number or null>,
  "weight_raw": "<original text or null>",
  "dimensions_raw": "<like '120 x 80 x 45cm' or null>",
  "vehicle_compatibility": ["<vehicle 1>"] or [],
  "sku": "<part number or null>",
  "upc": "<12-digit code or null>",
  "availability": "<in_stock|pre_order|sold_out|unknown>"
}}"""

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
            json_match = re.search(r'\{[\s\S]*\}', text)
            if json_match:
                return json.loads(json_match.group())
    except Exception as e:
        logger.warning(f"   LLM extraction failed: {e}")

    return {}


# --- WEBHOOK NOTIFICATION ---

async def notify_webhook(event: str, data: Dict):
    """Send webhook notification for real-time updates."""
    if not WEBHOOK_URL:
        return

    try:
        payload = {
            "event": event,
            "timestamp": datetime.now().isoformat(),
            "data": data
        }
        requests.post(WEBHOOK_URL, json=payload, timeout=5)
    except:
        pass  # Non-critical, don't fail on webhook errors


# --- CORE WORKER ---

async def process_product(sem: asyncio.Semaphore, browser, row: Dict, is_daily_update: bool = False):
    """Process a single product URL with enhanced extraction and validation."""
    async with sem:
        start_time = datetime.now()
        url = row['url']
        pid = row['product_id']
        source_id = row['id']
        product_data = row.get('products', {}) or {}
        specs_locked = product_data.get('specs_locked', False)

        logger.info(f"Processing: {url[:60]}...")

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
                logger.info(f"   Created product ID: {pid}")

                # Notify webhook of new product
                await notify_webhook("product_created", {"product_id": pid, "url": url})
            except Exception as e:
                logger.error(f"   DB error creating product: {e}")
                return

        # Get price history for anomaly detection
        price_history = await PriceAnomalyDetector.get_price_history(pid)
        price_stats = PriceAnomalyDetector.calculate_statistics(price_history)

        retry_count = 0
        max_retries = MAX_RETRIES

        while retry_count < max_retries:
            try:
                page = await browser.new_page(user_agent=random.choice(USER_AGENTS))

                try:
                    await page.goto(url, timeout=60000, wait_until="domcontentloaded")
                except PlaywrightTimeout:
                    logger.warning(f"   Timeout on initial load, retrying...")
                    retry_count += 1
                    await page.close()
                    await asyncio.sleep(RateLimitDetector.get_retry_delay(retry_count))
                    continue

                await asyncio.sleep(random.uniform(2, 4))

                html = await page.content()

                # Check for rate limiting
                if RateLimitDetector.is_rate_limited(html):
                    logger.warning(f"   Rate limited detected, backing off...")
                    retry_count += 1
                    await page.close()
                    await asyncio.sleep(RateLimitDetector.get_retry_delay(retry_count) * 2)
                    continue

                # Click expanders
                expander_patterns = [
                    "text=/spec/i", "text=/dimension/i", "text=/description/i",
                    "text=/detail/i", "text=/more info/i", "text=/show more/i",
                    "text=/features/i", "text=/technical/i", "text=/compatibility/i"
                ]

                for pattern in expander_patterns:
                    try:
                        await page.locator(pattern).first.click(timeout=500)
                        await asyncio.sleep(0.3)
                    except:
                        pass

                html = await page.content()
                body_text = await page.inner_text("body")
                soup = BeautifulSoup(html, 'html.parser')
                await page.close()

                # --- EXTRACTION ---
                specs = ProductSpecs()
                json_data = extract_json_ld(soup)

                # Name
                specs.name = json_data.get('name')
                specs.name_confidence = 0.95 if specs.name else 0.0

                if not specs.name:
                    og_title = soup.find("meta", property="og:title")
                    if og_title:
                        specs.name = og_title.get("content")
                        specs.name_confidence = 0.85

                if not specs.name and soup.title:
                    specs.name = soup.title.string
                    specs.name_confidence = 0.6

                # Prices (ALWAYS extract)
                regular, sale, current, confidence, method, candidates = extract_prices_enhanced(soup, body_text, json_data)
                specs.regular_price = regular
                specs.sale_price = sale
                specs.current_price = current
                specs.price_confidence = confidence
                specs.price_extraction_method = method

                # Price anomaly detection
                if specs.current_price and price_history:
                    validation_status, validation_meta = PriceAnomalyDetector.detect_anomaly(
                        specs.current_price, price_history
                    )
                    specs.price_validation = validation_status.value

                    if validation_status in [PriceValidation.DRAMATIC_DROP, PriceValidation.ANOMALY_DETECTED]:
                        logger.warning(f"   PRICE ANOMALY: {validation_meta}")

                        # Recheck with fresh page load
                        if retry_count < max_retries - 1:
                            logger.info(f"   Rechecking price...")
                            retry_count += 1
                            await asyncio.sleep(RateLimitDetector.get_retry_delay(retry_count))
                            continue
                        else:
                            specs.price_validation = PriceValidation.RECHECK_FAILED.value
                            logger.warning(f"   Price recheck exhausted, flagging for manual review")

                # Sale detection based on historical high
                if specs.current_price and price_stats:
                    specs.historical_high = price_stats.get('high')
                    specs.historical_low = price_stats.get('low')
                    is_sale, discount = PriceAnomalyDetector.calculate_sale_info(
                        specs.current_price, price_stats
                    )
                    specs.is_on_sale = is_sale
                    specs.discount_percentage = discount

                # Availability
                specs.availability = extract_availability(soup, body_text, json_data)

                # Log price verdict
                price_parts = []
                if specs.regular_price: price_parts.append(f"RRP: ${specs.regular_price}")
                if specs.sale_price: price_parts.append(f"Sale: ${specs.sale_price}")
                if specs.current_price: price_parts.append(f"Current: ${specs.current_price}")
                price_parts.append(f"Conf: {specs.price_confidence}")
                if specs.is_on_sale: price_parts.append(f"SALE -{specs.discount_percentage}%")
                logger.info(f"   Price: {' | '.join(price_parts)}")

                # --- FULL EXTRACTION ---
                if not is_daily_update or not specs_locked:
                    # Description
                    specs.description, specs.description_confidence = get_best_description(
                        soup, specs.name, json_data.get('description')
                    )

                    # Images
                    specs.image_urls = extract_multiple_images(soup, json_data)
                    specs.image_url = specs.image_urls[0] if specs.image_urls else None

                    # Brand
                    specs.brand = json_data.get('brand')
                    if not specs.brand:
                        brand_elem = soup.select_one("[itemprop='brand'], .brand, .product-brand")
                        if brand_elem:
                            specs.brand = brand_elem.get_text(strip=True)

                    # Product identifiers
                    identifiers = extract_product_identifiers(soup, body_text, json_data)
                    specs.sku = identifiers['sku']
                    specs.upc = identifiers['upc']
                    specs.ean = identifiers['ean']
                    specs.mpn = identifiers['mpn']

                    # Reviews
                    specs.rating, specs.review_count = extract_reviews(soup, json_data)

                    # Variants
                    specs.variants = extract_variants(soup, json_data, body_text)
                    specs.has_variants = len(specs.variants) > 1

                    # Weight
                    specs.weight_kg, specs.weight_raw, specs.weight_confidence = extract_weight(soup, body_text, json_data)
                    if specs.weight_kg:
                        logger.info(f"   Weight: {specs.weight_kg}kg (conf: {specs.weight_confidence})")

                    # Dimensions
                    specs.dimensions_l, specs.dimensions_w, specs.dimensions_h, specs.dimensions_raw, specs.dimensions_confidence = extract_dimensions(soup, body_text, json_data)
                    if specs.dimensions_raw:
                        logger.info(f"   Dimensions: {specs.dimensions_raw}")

                    # Vehicle Compatibility
                    specs.vehicle_compatibility, specs.is_universal = extract_vehicle_compatibility(soup, body_text, specs.name or "")
                    if specs.vehicle_compatibility:
                        logger.info(f"   Vehicles: {len(specs.vehicle_compatibility)} found")
                    elif specs.is_universal:
                        logger.info(f"   Vehicle: Universal fit")

                    # Category
                    specs.category, cat_confidence = infer_category(specs.name or "", specs.description or "")
                    logger.info(f"   Category: {specs.category}")

                    # LLM fallback for missing critical data
                    if not specs.weight_kg or not specs.sku:
                        logger.info("   Trying LLM extraction...")
                        llm_data = await llm_extract_specs(body_text, specs.name or "")

                        if not specs.weight_kg and llm_data.get('weight_kg'):
                            specs.weight_kg = llm_data['weight_kg']
                            specs.weight_raw = llm_data.get('weight_raw')
                            specs.weight_confidence = 0.6

                        if not specs.sku and llm_data.get('sku'):
                            specs.sku = llm_data['sku']

                        if not specs.upc and llm_data.get('upc'):
                            specs.upc = llm_data['upc']

                # Calculate overall confidence
                specs.overall_confidence = ConfidenceCalculator.calculate_overall_confidence(specs)
                specs.scrape_duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)

                # --- SAVE TO DATABASE ---
                now = datetime.now().isoformat()

                # Build update data
                if is_daily_update and specs_locked:
                    update_data = {"updated_at": now}
                    if specs.current_price:
                        update_data["price"] = specs.current_price
                        if specs.regular_price:
                            update_data["regular_price"] = specs.regular_price
                        if specs.sale_price:
                            update_data["sale_price"] = specs.sale_price
                    update_data["availability"] = specs.availability
                    if specs.is_on_sale:
                        update_data["is_on_sale"] = True
                        update_data["discount_percentage"] = specs.discount_percentage
                    logger.info(f"   Daily update: ${specs.current_price} | {specs.availability}")
                else:
                    update_data = {
                        "name": (specs.name or "Unknown")[:255],
                        "description": specs.description,
                        "category": specs.category,
                        "updated_at": now,
                        "last_full_scrape": now,
                        "availability": specs.availability,
                        "overall_confidence": specs.overall_confidence
                    }

                    if specs.image_url:
                        update_data["image_url"] = specs.image_url
                    if specs.image_urls:
                        update_data["image_urls"] = specs.image_urls
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
                    if specs.upc:
                        update_data["upc"] = specs.upc
                    if specs.ean:
                        update_data["ean"] = specs.ean
                    if specs.mpn:
                        update_data["mpn"] = specs.mpn
                    if specs.brand:
                        update_data["brand"] = specs.brand
                    if specs.rating:
                        update_data["rating"] = specs.rating
                    if specs.review_count:
                        update_data["review_count"] = specs.review_count
                    if specs.is_on_sale:
                        update_data["is_on_sale"] = specs.is_on_sale
                        update_data["discount_percentage"] = specs.discount_percentage
                    if specs.has_variants:
                        update_data["has_variants"] = specs.has_variants
                        update_data["variants"] = specs.variants

                supabase.table("products").update(update_data).eq("id", pid).execute()

                # Enhanced price history with metadata
                if specs.current_price:
                    supabase.table("product_sources").update({
                        "last_price": specs.current_price,
                        "last_checked": "now()"
                    }).eq("id", source_id).execute()

                    # Comprehensive price history record
                    history_record = {
                        "product_id": pid,
                        "price": specs.current_price,
                        "regular_price": specs.regular_price,
                        "sale_price": specs.sale_price,
                        "confidence": specs.price_confidence,
                        "extraction_method": specs.price_extraction_method,
                        "validation_status": specs.price_validation,
                        "is_sale": specs.is_on_sale,
                        "discount_percentage": specs.discount_percentage,
                        "availability": specs.availability,
                        "source_url": url
                    }
                    supabase.table("price_history").insert(history_record).execute()

                # Notify webhook of update
                await notify_webhook("product_updated", {
                    "product_id": pid,
                    "price": specs.current_price,
                    "is_sale": specs.is_on_sale,
                    "availability": specs.availability
                })

                logger.info(f"   Saved (conf: {specs.overall_confidence}, {specs.scrape_duration_ms}ms)")
                return  # Success

            except Exception as e:
                logger.error(f"   Scrape error: {e}")
                retry_count += 1
                if retry_count < max_retries:
                    await asyncio.sleep(RateLimitDetector.get_retry_delay(retry_count))
                continue

        logger.error(f"   Failed after {max_retries} retries")


async def main():
    """Main entry point."""
    import sys

    is_daily_update = "--daily" in sys.argv or os.environ.get("DAILY_UPDATE") == "true"
    single_url = None

    # Check for single URL mode
    for arg in sys.argv:
        if arg.startswith("--url="):
            single_url = arg.split("=", 1)[1]

    if is_daily_update:
        logger.info("=" * 60)
        logger.info("DAILY UPDATE MODE - Price & availability only")
        logger.info("=" * 60)
    else:
        logger.info("=" * 60)
        logger.info("FULL EXTRACTION MODE - All product specifications")
        logger.info("=" * 60)

    # Fetch sources
    if single_url:
        logger.info(f"Single URL mode: {single_url}")
        response = supabase.table("product_sources").select("*, products(*)").eq("url", single_url).execute()
    else:
        response = supabase.table("product_sources").select("*, products(*)").execute()

    sources = response.data

    if not sources:
        logger.info("No products to process")
        return

    logger.info(f"Processing {len(sources)} products with {CONCURRENCY} workers...")
    logger.info("-" * 60)

    sem = asyncio.Semaphore(CONCURRENCY)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        tasks = [process_product(sem, browser, row, is_daily_update) for row in sources]
        await asyncio.gather(*tasks)
        await browser.close()

    logger.info("=" * 60)
    logger.info("COMPLETE!")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
