-- Product Specification Extractor v2.0 - Database Migration
-- Run this in your Supabase SQL Editor to add the new engineering fields
-- ============================================================================

-- Add pricing fields (Regular vs Sale price)
ALTER TABLE products ADD COLUMN IF NOT EXISTS regular_price FLOAT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS sale_price FLOAT;

-- Add weight fields (Critical for GVM compliance)
ALTER TABLE products ADD COLUMN IF NOT EXISTS weight_kg FLOAT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS weight_raw TEXT;  -- Original text like "25kg" or "55 lbs"

-- Add dimension fields (L x W x H in centimeters)
ALTER TABLE products ADD COLUMN IF NOT EXISTS dimensions_l FLOAT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS dimensions_w FLOAT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS dimensions_h FLOAT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS dimensions_raw TEXT;  -- Original text like "120 x 80 x 45 cm"

-- Add vehicle compatibility (array of vehicle names)
ALTER TABLE products ADD COLUMN IF NOT EXISTS vehicle_compatibility TEXT[];
ALTER TABLE products ADD COLUMN IF NOT EXISTS is_universal BOOLEAN DEFAULT FALSE;

-- Add catalog/inventory fields
ALTER TABLE products ADD COLUMN IF NOT EXISTS availability TEXT DEFAULT 'unknown';
ALTER TABLE products ADD COLUMN IF NOT EXISTS sku TEXT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS brand TEXT;

-- Add lock flag to prevent overwriting manual edits
-- Set this to TRUE on products you've manually edited in the app
ALTER TABLE products ADD COLUMN IF NOT EXISTS specs_locked BOOLEAN DEFAULT FALSE;

-- Track when full specs were last scraped
ALTER TABLE products ADD COLUMN IF NOT EXISTS last_full_scrape TIMESTAMP;

-- Create index for faster vehicle compatibility searches
CREATE INDEX IF NOT EXISTS idx_products_vehicle_compatibility
ON products USING GIN (vehicle_compatibility);

-- Create index for availability filtering
CREATE INDEX IF NOT EXISTS idx_products_availability
ON products (availability);

-- Create index for category filtering
CREATE INDEX IF NOT EXISTS idx_products_category
ON products (category);

-- Create index for weight (useful for GVM calculations)
CREATE INDEX IF NOT EXISTS idx_products_weight
ON products (weight_kg);

-- ============================================================================
-- OPTIONAL: Create a view for GVM-relevant products with weight data
-- ============================================================================

CREATE OR REPLACE VIEW products_with_weight AS
SELECT
    id,
    name,
    category,
    brand,
    weight_kg,
    weight_raw,
    dimensions_l,
    dimensions_w,
    dimensions_h,
    dimensions_raw,
    vehicle_compatibility,
    is_universal,
    price,
    regular_price,
    sale_price,
    availability,
    sku,
    image_url,
    updated_at
FROM products
WHERE weight_kg IS NOT NULL
ORDER BY weight_kg DESC;

-- ============================================================================
-- OPTIONAL: Create a function to search products by vehicle
-- ============================================================================

CREATE OR REPLACE FUNCTION search_products_by_vehicle(search_vehicle TEXT)
RETURNS SETOF products AS $$
BEGIN
    RETURN QUERY
    SELECT *
    FROM products
    WHERE
        is_universal = TRUE
        OR EXISTS (
            SELECT 1 FROM unnest(vehicle_compatibility) AS v
            WHERE v ILIKE '%' || search_vehicle || '%'
        )
    ORDER BY name;
END;
$$ LANGUAGE plpgsql;

-- Example usage: SELECT * FROM search_products_by_vehicle('Hilux');

-- ============================================================================
-- VERIFICATION: Check the new columns were added
-- ============================================================================

SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'products'
AND column_name IN (
    'regular_price', 'sale_price',
    'weight_kg', 'weight_raw',
    'dimensions_l', 'dimensions_w', 'dimensions_h', 'dimensions_raw',
    'vehicle_compatibility', 'is_universal',
    'availability', 'sku', 'brand',
    'specs_locked', 'last_full_scrape'
)
ORDER BY column_name;
