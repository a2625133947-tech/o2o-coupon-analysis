-- Four non-overlapping groupings at separate grains; never sum user counts across groups.
CREATE TABLE coupon_type_metrics AS SELECT coupon_type,
count(*) AS receipt_records,
 count(DISTINCT user_id) AS receipt_users,
 sum(redeemed15_mature::BIGINT) AS redeemed15_records,
 count(DISTINCT user_id) FILTER (WHERE redeemed15_mature) AS redeemed15_users,
 sum(redeemed15_mature::BIGINT)::DOUBLE / nullif(count(*),0) AS redemption_rate15,
 count(DISTINCT user_id) FILTER (WHERE redeemed15_mature)::DOUBLE /
 nullif(count(DISTINCT user_id),0) AS redemption_user_share15,
 count(*) < (SELECT small_sample_receipts FROM stage2_config) AS small_sample
FROM receipts GROUP BY coupon_type ORDER BY coupon_type;
CREATE TABLE coupon_threshold_metrics AS SELECT coupon_type, threshold,
count(*) AS receipt_records,
 count(DISTINCT user_id) AS receipt_users,
 sum(redeemed15_mature::BIGINT) AS redeemed15_records,
 count(DISTINCT user_id) FILTER (WHERE redeemed15_mature) AS redeemed15_users,
 sum(redeemed15_mature::BIGINT)::DOUBLE / nullif(count(*),0) AS redemption_rate15,
 count(DISTINCT user_id) FILTER (WHERE redeemed15_mature)::DOUBLE /
 nullif(count(DISTINCT user_id),0) AS redemption_user_share15,
 count(*) < (SELECT small_sample_receipts FROM stage2_config) AS small_sample
FROM receipts GROUP BY coupon_type, threshold ORDER BY coupon_type, threshold;
CREATE TABLE coupon_discount_band_metrics AS SELECT coupon_type, nominal_discount_band,
count(*) AS receipt_records,
 count(DISTINCT user_id) AS receipt_users,
 sum(redeemed15_mature::BIGINT) AS redeemed15_records,
 count(DISTINCT user_id) FILTER (WHERE redeemed15_mature) AS redeemed15_users,
 sum(redeemed15_mature::BIGINT)::DOUBLE / nullif(count(*),0) AS redemption_rate15,
 count(DISTINCT user_id) FILTER (WHERE redeemed15_mature)::DOUBLE /
 nullif(count(DISTINCT user_id),0) AS redemption_user_share15,
 count(*) < (SELECT small_sample_receipts FROM stage2_config) AS small_sample
FROM receipts GROUP BY coupon_type, nominal_discount_band ORDER BY coupon_type, nominal_discount_band;
CREATE TABLE coupon_scheme_metrics AS SELECT coupon_type, discount_rate, threshold, reduction, nominal_discount_fraction,
count(*) AS receipt_records,
 count(DISTINCT user_id) AS receipt_users,
 sum(redeemed15_mature::BIGINT) AS redeemed15_records,
 count(DISTINCT user_id) FILTER (WHERE redeemed15_mature) AS redeemed15_users,
 sum(redeemed15_mature::BIGINT)::DOUBLE / nullif(count(*),0) AS redemption_rate15,
 count(DISTINCT user_id) FILTER (WHERE redeemed15_mature)::DOUBLE /
 nullif(count(DISTINCT user_id),0) AS redemption_user_share15,
 count(*) < (SELECT small_sample_receipts FROM stage2_config) AS small_sample
FROM receipts GROUP BY coupon_type, discount_rate, threshold, reduction, nominal_discount_fraction ORDER BY coupon_type, discount_rate, threshold, reduction, nominal_discount_fraction;
CREATE TABLE coupon_type_month_metrics AS SELECT receipt_month, coupon_type,
count(*) AS receipt_records,
 count(DISTINCT user_id) AS receipt_users,
 sum(redeemed15_mature::BIGINT) AS redeemed15_records,
 count(DISTINCT user_id) FILTER (WHERE redeemed15_mature) AS redeemed15_users,
 sum(redeemed15_mature::BIGINT)::DOUBLE / nullif(count(*),0) AS redemption_rate15,
 count(DISTINCT user_id) FILTER (WHERE redeemed15_mature)::DOUBLE /
 nullif(count(DISTINCT user_id),0) AS redemption_user_share15,
 count(*) < (SELECT small_sample_receipts FROM stage2_config) AS small_sample
FROM receipts GROUP BY receipt_month, coupon_type ORDER BY receipt_month, coupon_type;
