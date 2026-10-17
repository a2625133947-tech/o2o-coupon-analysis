-- A single receipt window; rates are numerator/denominator, never merchant-rate averages.
CREATE TABLE merchant_metrics AS SELECT merchant_id,
 count(*) AS receipt_records,
 count(DISTINCT user_id) AS receipt_users,
 sum(redeemed15_mature::BIGINT) AS redeemed15_records,
 count(DISTINCT user_id) FILTER (WHERE redeemed15_mature) AS redeemed15_users,
 sum(redeemed15_mature::BIGINT)::DOUBLE / nullif(count(*),0) AS redemption_rate15,
 count(DISTINCT user_id) FILTER (WHERE redeemed15_mature)::DOUBLE /
 nullif(count(DISTINCT user_id),0) AS redemption_user_share15,
 count(*) < (SELECT small_sample_receipts FROM stage2_config) AS small_sample FROM receipts GROUP BY merchant_id ORDER BY receipt_records DESC, merchant_id;
-- Disjoint composition cells for candidate diagnostics and comparable-range checks.
CREATE TABLE merchant_structure AS SELECT merchant_id, receipt_month, coupon_type,
 threshold, distance_group, count(*) AS receipt_records,
 sum(redeemed15_mature::BIGINT) AS redeemed15_records,
 sum(redeemed15_mature::BIGINT)::DOUBLE/nullif(count(*),0) AS redemption_rate15,
 count(*) < (SELECT small_sample_receipts FROM stage2_config) AS small_sample
FROM receipts GROUP BY merchant_id, receipt_month, coupon_type, threshold, distance_group
ORDER BY merchant_id, receipt_month, coupon_type, threshold, distance_group;
CREATE TABLE merchant_distribution AS
SELECT count(*) AS merchant_count, min(receipt_records) AS min_receipts,
 quantile_cont(receipt_records,0.25) AS q25_receipts,
 quantile_cont(receipt_records,0.5) AS median_receipts,
 quantile_cont(receipt_records,0.75) AS q75_receipts,
 quantile_cont(receipt_records,0.9) AS q90_receipts,
 max(receipt_records) AS max_receipts,
 count(*) FILTER (WHERE small_sample) AS small_sample_merchants
FROM merchant_metrics;
