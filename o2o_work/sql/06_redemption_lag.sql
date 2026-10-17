-- Daily/cumulative rates use all mature receipts; success distribution uses successes only.
CREATE TABLE redemption_lag AS
WITH daily AS (
 SELECT lag_days, count(*) AS daily_redeemed15_records FROM receipts
 WHERE redeemed15_mature GROUP BY lag_days
), calendar AS (SELECT range AS lag_day FROM range(0,16)), counts AS (
 SELECT lag_day, coalesce(daily_redeemed15_records,0) AS daily_redeemed15_records
 FROM calendar LEFT JOIN daily ON lag_day=lag_days
), cumulative AS (
 SELECT *, sum(daily_redeemed15_records) OVER (ORDER BY lag_day) AS cumulative_redeemed15_records FROM counts
), denominators AS (
 SELECT count(*) AS mature_receipt_records, sum(redeemed15_mature::BIGINT) AS total_redeemed15_records FROM receipts
)
SELECT *, daily_redeemed15_records::DOUBLE/nullif(mature_receipt_records,0) AS daily_redemption_rate15,
 cumulative_redeemed15_records::DOUBLE/nullif(mature_receipt_records,0) AS cumulative_redemption_rate15,
 daily_redeemed15_records::DOUBLE/nullif(total_redeemed15_records,0) AS share_of_successful_redemptions,
 cumulative_redeemed15_records::DOUBLE/nullif(total_redeemed15_records,0) AS cumulative_share_of_successes
FROM cumulative CROSS JOIN denominators ORDER BY lag_day;
CREATE TABLE redemption_lag_summary AS
SELECT count(*) AS successful_receipt_records,
 avg(lag_days) AS mean_wait_days, median(lag_days) AS median_wait_days,
 quantile_cont(lag_days,0.75) AS q75_wait_days, quantile_cont(lag_days,0.9) AS q90_wait_days
FROM receipts WHERE redeemed15_mature;
CREATE TABLE redemption_lag_by_coupon_type AS
WITH types AS (SELECT DISTINCT coupon_type FROM receipts), daily AS (
 SELECT coupon_type, lag_days, count(*) AS daily_redeemed15_records
 FROM receipts WHERE redeemed15_mature GROUP BY coupon_type, lag_days
), denominator AS (
 SELECT coupon_type, count(*) AS mature_receipt_records,
 sum(redeemed15_mature::BIGINT) AS total_redeemed15_records FROM receipts GROUP BY coupon_type
), counts AS (
 SELECT t.coupon_type, r.range AS lag_day, coalesce(d.daily_redeemed15_records,0) AS daily_redeemed15_records
 FROM types t CROSS JOIN range(0,16) r LEFT JOIN daily d ON t.coupon_type=d.coupon_type AND r.range=d.lag_days
)
SELECT c.*, sum(daily_redeemed15_records) OVER (PARTITION BY c.coupon_type ORDER BY lag_day) AS cumulative_redeemed15_records,
 mature_receipt_records, total_redeemed15_records,
 daily_redeemed15_records::DOUBLE/nullif(mature_receipt_records,0) AS daily_redemption_rate15,
 daily_redeemed15_records::DOUBLE/nullif(total_redeemed15_records,0) AS share_of_successful_redemptions
FROM counts c JOIN denominator USING (coupon_type) ORDER BY coupon_type, lag_day;
