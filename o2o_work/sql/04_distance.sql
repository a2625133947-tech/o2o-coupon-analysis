-- Distance remains a raw code. Unknown is retained, never mapped to zero or exact km.
CREATE TABLE distance_metrics AS SELECT distance_group,
 count(*) AS receipt_records,
 count(DISTINCT user_id) AS receipt_users,
 sum(redeemed15_mature::BIGINT) AS redeemed15_records,
 count(DISTINCT user_id) FILTER (WHERE redeemed15_mature) AS redeemed15_users,
 sum(redeemed15_mature::BIGINT)::DOUBLE / nullif(count(*),0) AS redemption_rate15,
 count(DISTINCT user_id) FILTER (WHERE redeemed15_mature)::DOUBLE /
 nullif(count(DISTINCT user_id),0) AS redemption_user_share15,
 count(*) < (SELECT small_sample_receipts FROM stage2_config) AS small_sample FROM receipts GROUP BY distance_group
ORDER BY CASE WHEN distance_group='unknown' THEN 99 ELSE cast(distance_group AS INTEGER) END;
CREATE TABLE distance_month_metrics AS SELECT receipt_month, distance_group,
 count(*) AS receipt_records,
 count(DISTINCT user_id) AS receipt_users,
 sum(redeemed15_mature::BIGINT) AS redeemed15_records,
 count(DISTINCT user_id) FILTER (WHERE redeemed15_mature) AS redeemed15_users,
 sum(redeemed15_mature::BIGINT)::DOUBLE / nullif(count(*),0) AS redemption_rate15,
 count(DISTINCT user_id) FILTER (WHERE redeemed15_mature)::DOUBLE /
 nullif(count(DISTINCT user_id),0) AS redemption_user_share15,
 count(*) < (SELECT small_sample_receipts FROM stage2_config) AS small_sample FROM receipts GROUP BY receipt_month, distance_group ORDER BY receipt_month, distance_group;
