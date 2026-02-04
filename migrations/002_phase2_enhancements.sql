-- Product Specification Extractor v3.0 - Phase 2 Migration
-- Enhanced price history, anomaly detection, confidence scoring
-- Run this AFTER 001_product_specs.sql
-- ============================================================================

-- ============================================================================
-- ENHANCED PRICE HISTORY TABLE
-- Stores every price check with full metadata - NEVER delete this data
-- ============================================================================

-- Drop the simple price_history if it exists and recreate with full schema
-- Note: If you have existing data, backup first!

-- First, check if columns exist and add them if they don't
ALTER TABLE price_history ADD COLUMN IF NOT EXISTS regular_price FLOAT;
ALTER TABLE price_history ADD COLUMN IF NOT EXISTS sale_price FLOAT;
ALTER TABLE price_history ADD COLUMN IF NOT EXISTS confidence FLOAT DEFAULT 0;
ALTER TABLE price_history ADD COLUMN IF NOT EXISTS extraction_method TEXT;
ALTER TABLE price_history ADD COLUMN IF NOT EXISTS validation_status TEXT DEFAULT 'valid';
ALTER TABLE price_history ADD COLUMN IF NOT EXISTS is_sale BOOLEAN DEFAULT FALSE;
ALTER TABLE price_history ADD COLUMN IF NOT EXISTS discount_percentage FLOAT;
ALTER TABLE price_history ADD COLUMN IF NOT EXISTS availability TEXT;
ALTER TABLE price_history ADD COLUMN IF NOT EXISTS source_url TEXT;
ALTER TABLE price_history ADD COLUMN IF NOT EXISTS checked_at TIMESTAMP DEFAULT NOW();

-- Ensure we have the timestamp column
ALTER TABLE price_history RENAME COLUMN timestamp TO checked_at;
-- If that fails, the column might already be named checked_at

-- ============================================================================
-- PRODUCTS TABLE - Phase 2 Additions
-- ============================================================================

-- Multiple images support
ALTER TABLE products ADD COLUMN IF NOT EXISTS image_urls TEXT[];

-- Product identifiers
ALTER TABLE products ADD COLUMN IF NOT EXISTS upc TEXT;  -- 12-digit Universal Product Code
ALTER TABLE products ADD COLUMN IF NOT EXISTS ean TEXT;  -- 13-digit European Article Number
ALTER TABLE products ADD COLUMN IF NOT EXISTS mpn TEXT;  -- Manufacturer Part Number

-- Reviews and ratings
ALTER TABLE products ADD COLUMN IF NOT EXISTS rating FLOAT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS review_count INTEGER;

-- Sale tracking
ALTER TABLE products ADD COLUMN IF NOT EXISTS is_on_sale BOOLEAN DEFAULT FALSE;
ALTER TABLE products ADD COLUMN IF NOT EXISTS discount_percentage FLOAT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS historical_high FLOAT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS historical_low FLOAT;

-- Variants
ALTER TABLE products ADD COLUMN IF NOT EXISTS has_variants BOOLEAN DEFAULT FALSE;
ALTER TABLE products ADD COLUMN IF NOT EXISTS variants JSONB;

-- Confidence scoring
ALTER TABLE products ADD COLUMN IF NOT EXISTS overall_confidence FLOAT DEFAULT 0;
ALTER TABLE products ADD COLUMN IF NOT EXISTS price_confidence FLOAT DEFAULT 0;
ALTER TABLE products ADD COLUMN IF NOT EXISTS price_validation_status TEXT DEFAULT 'valid';

-- ============================================================================
-- INDEXES FOR PERFORMANCE
-- ============================================================================

-- Price history indexes (critical for anomaly detection queries)
CREATE INDEX IF NOT EXISTS idx_price_history_product_checked
ON price_history (product_id, checked_at DESC);

CREATE INDEX IF NOT EXISTS idx_price_history_validation
ON price_history (validation_status);

CREATE INDEX IF NOT EXISTS idx_price_history_is_sale
ON price_history (is_sale) WHERE is_sale = TRUE;

-- Products indexes for filtering
CREATE INDEX IF NOT EXISTS idx_products_is_on_sale
ON products (is_on_sale) WHERE is_on_sale = TRUE;

CREATE INDEX IF NOT EXISTS idx_products_rating
ON products (rating) WHERE rating IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_products_confidence
ON products (overall_confidence);

CREATE INDEX IF NOT EXISTS idx_products_upc
ON products (upc) WHERE upc IS NOT NULL;

-- ============================================================================
-- VIEWS FOR COMMON QUERIES
-- ============================================================================

-- Products currently on sale
CREATE OR REPLACE VIEW products_on_sale AS
SELECT
    id,
    name,
    brand,
    category,
    price,
    regular_price,
    sale_price,
    discount_percentage,
    historical_high,
    availability,
    image_url,
    updated_at
FROM products
WHERE is_on_sale = TRUE
  AND is_approved = TRUE
ORDER BY discount_percentage DESC;

-- Price anomalies needing review
CREATE OR REPLACE VIEW price_anomalies AS
SELECT
    p.id,
    p.name,
    p.price,
    ph.price as recorded_price,
    ph.validation_status,
    ph.confidence,
    ph.checked_at,
    ps.url
FROM products p
JOIN price_history ph ON p.id = ph.product_id
JOIN product_sources ps ON p.id = ps.product_id
WHERE ph.validation_status IN ('anomaly_detected', 'dramatic_drop', 'recheck_failed')
  AND ph.checked_at > NOW() - INTERVAL '7 days'
ORDER BY ph.checked_at DESC;

-- Products with low confidence scores
CREATE OR REPLACE VIEW low_confidence_products AS
SELECT
    id,
    name,
    overall_confidence,
    price_confidence,
    category,
    updated_at
FROM products
WHERE overall_confidence < 0.5
  AND is_approved = FALSE
ORDER BY overall_confidence ASC;

-- ============================================================================
-- FUNCTIONS FOR PRICE ANALYTICS
-- ============================================================================

-- Get price statistics for a product
CREATE OR REPLACE FUNCTION get_price_stats(p_product_id INTEGER, p_days INTEGER DEFAULT 30)
RETURNS TABLE (
    high_price FLOAT,
    low_price FLOAT,
    avg_price FLOAT,
    price_count BIGINT,
    current_price FLOAT,
    price_change_pct FLOAT
) AS $$
DECLARE
    v_current FLOAT;
    v_previous FLOAT;
BEGIN
    -- Get current price
    SELECT price INTO v_current FROM products WHERE id = p_product_id;

    -- Get previous recorded price
    SELECT price INTO v_previous
    FROM price_history
    WHERE product_id = p_product_id
    ORDER BY checked_at DESC
    OFFSET 1 LIMIT 1;

    RETURN QUERY
    SELECT
        MAX(ph.price)::FLOAT as high_price,
        MIN(ph.price)::FLOAT as low_price,
        AVG(ph.price)::FLOAT as avg_price,
        COUNT(*)::BIGINT as price_count,
        v_current as current_price,
        CASE
            WHEN v_previous > 0 THEN ((v_current - v_previous) / v_previous * 100)::FLOAT
            ELSE 0::FLOAT
        END as price_change_pct
    FROM price_history ph
    WHERE ph.product_id = p_product_id
      AND ph.checked_at > NOW() - (p_days || ' days')::INTERVAL;
END;
$$ LANGUAGE plpgsql;

-- Detect if a price change is anomalous
CREATE OR REPLACE FUNCTION is_price_anomaly(
    p_product_id INTEGER,
    p_new_price FLOAT,
    p_threshold FLOAT DEFAULT 0.30
) RETURNS BOOLEAN AS $$
DECLARE
    v_latest_price FLOAT;
    v_change_pct FLOAT;
BEGIN
    -- Get most recent price
    SELECT price INTO v_latest_price
    FROM price_history
    WHERE product_id = p_product_id
    ORDER BY checked_at DESC
    LIMIT 1;

    IF v_latest_price IS NULL OR v_latest_price = 0 THEN
        RETURN FALSE;
    END IF;

    v_change_pct := ABS(p_new_price - v_latest_price) / v_latest_price;

    RETURN v_change_pct >= p_threshold;
END;
$$ LANGUAGE plpgsql;

-- Update historical high/low for a product
CREATE OR REPLACE FUNCTION update_price_extremes(p_product_id INTEGER)
RETURNS VOID AS $$
DECLARE
    v_high FLOAT;
    v_low FLOAT;
BEGIN
    SELECT MAX(price), MIN(price)
    INTO v_high, v_low
    FROM price_history
    WHERE product_id = p_product_id
      AND price > 0;

    UPDATE products
    SET historical_high = v_high,
        historical_low = v_low
    WHERE id = p_product_id;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- TRIGGER: Auto-update price extremes on new price history
-- ============================================================================

CREATE OR REPLACE FUNCTION trigger_update_price_extremes()
RETURNS TRIGGER AS $$
BEGIN
    PERFORM update_price_extremes(NEW.product_id);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_price_history_update_extremes ON price_history;
CREATE TRIGGER trg_price_history_update_extremes
AFTER INSERT ON price_history
FOR EACH ROW
EXECUTE FUNCTION trigger_update_price_extremes();

-- ============================================================================
-- TRIGGER: Auto-detect sales
-- ============================================================================

CREATE OR REPLACE FUNCTION trigger_detect_sale()
RETURNS TRIGGER AS $$
DECLARE
    v_high FLOAT;
    v_discount FLOAT;
BEGIN
    -- Get historical high
    SELECT historical_high INTO v_high FROM products WHERE id = NEW.product_id;

    IF v_high IS NOT NULL AND v_high > NEW.price THEN
        v_discount := ((v_high - NEW.price) / v_high) * 100;

        IF v_discount >= 5 THEN
            UPDATE products
            SET is_on_sale = TRUE,
                discount_percentage = ROUND(v_discount::NUMERIC, 1)
            WHERE id = NEW.product_id;
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_price_history_detect_sale ON price_history;
CREATE TRIGGER trg_price_history_detect_sale
AFTER INSERT ON price_history
FOR EACH ROW
EXECUTE FUNCTION trigger_detect_sale();

-- ============================================================================
-- VERIFICATION
-- ============================================================================

SELECT 'Phase 2 Migration Complete!' as status;

-- Check new columns
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'price_history'
AND column_name IN ('confidence', 'extraction_method', 'validation_status', 'is_sale', 'discount_percentage')
ORDER BY column_name;

SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'products'
AND column_name IN ('upc', 'ean', 'mpn', 'rating', 'review_count', 'is_on_sale', 'overall_confidence', 'variants')
ORDER BY column_name;
