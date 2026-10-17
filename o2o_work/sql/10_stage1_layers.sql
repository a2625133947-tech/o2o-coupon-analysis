-- raw_offline is imported separately with all seven original fields as VARCHAR.
-- Exact duplicates use all seven RAW values, never business-key approximations.
CREATE TABLE offline_duplicate_map AS
SELECT record_id,
 row_number() OVER (PARTITION BY User_id, Merchant_id, Coupon_id, Discount_rate,
 Distance, Date_received, Date ORDER BY source_row_number) AS exact_duplicate_rank,
 count(*) OVER (PARTITION BY User_id, Merchant_id, Coupon_id, Discount_rate,
 Distance, Date_received, Date) AS exact_duplicate_group_size,
 min(source_row_number) OVER (PARTITION BY User_id, Merchant_id, Coupon_id, Discount_rate,
 Distance, Date_received, Date) AS first_source_row_number
FROM raw_offline;

CREATE TABLE fact_offline_records AS
WITH normalized AS (
 SELECT record_id, source_file, source_row_number,
 CASE WHEN User_id IN ('null', '') THEN NULL ELSE User_id END AS user_id,
 CASE WHEN Merchant_id IN ('null', '') THEN NULL ELSE Merchant_id END AS merchant_id,
 CASE WHEN Coupon_id IN ('null', '') THEN NULL ELSE Coupon_id END AS coupon_id,
 CASE WHEN Discount_rate IN ('null', '') THEN NULL ELSE Discount_rate END AS discount_rate,
 CASE WHEN Distance IN ('null', '') THEN NULL ELSE Distance END AS distance_code,
 CASE WHEN Date_received IN ('null', '') THEN NULL ELSE Date_received END AS received_text,
 CASE WHEN Date IN ('null', '') THEN NULL ELSE Date END AS consumed_text
 FROM raw_offline
), parsed AS (
 SELECT *,
 CASE WHEN regexp_full_match(received_text, '[0-9]{8}')
      THEN try_strptime(received_text, '%Y%m%d')::DATE END AS received_date,
 CASE WHEN regexp_full_match(consumed_text, '[0-9]{8}')
      THEN try_strptime(consumed_text, '%Y%m%d')::DATE END AS consumed_date,
 CASE WHEN regexp_full_match(discount_rate, '[0-9]+(\.[0-9]+)?:[0-9]+(\.[0-9]+)?')
      THEN try_cast(split_part(discount_rate, ':', 1) AS DOUBLE) END AS threshold_candidate,
 CASE WHEN regexp_full_match(discount_rate, '[0-9]+(\.[0-9]+)?:[0-9]+(\.[0-9]+)?')
      THEN try_cast(split_part(discount_rate, ':', 2) AS DOUBLE) END AS reduction_candidate,
 CASE WHEN regexp_full_match(discount_rate, '[0-9]+(\.[0-9]+)?')
      THEN try_cast(discount_rate AS DOUBLE) END AS direct_candidate
 FROM normalized
), coupon_parsed AS (
 SELECT *,
 CASE WHEN discount_rate IS NULL THEN 'no_discount'
 WHEN isfinite(threshold_candidate) AND isfinite(reduction_candidate)
  AND threshold_candidate > 0 AND reduction_candidate > 0
  AND reduction_candidate <= threshold_candidate THEN 'threshold_reduction'
 WHEN isfinite(direct_candidate) AND direct_candidate > 0 AND direct_candidate <= 1 THEN 'direct_discount'
 ELSE 'invalid_discount' END AS coupon_type,
 date_diff('day', received_date, consumed_date) AS lag_days,
 received_text IS NOT NULL AND received_date IS NULL AS invalid_received_date,
 consumed_text IS NOT NULL AND consumed_date IS NULL AS invalid_consumed_date,
 (coupon_id IS NOT NULL) <> (received_text IS NOT NULL) AS coupon_receipt_mismatch,
 coalesce(
  (received_text IS NOT NULL AND received_date IS NULL)
  OR (consumed_text IS NOT NULL AND consumed_date IS NULL)
  OR coalesce(date_diff('day', received_date, consumed_date) < 0, FALSE)
  OR ((coupon_id IS NOT NULL) <> (received_text IS NOT NULL))
  OR user_id IS NULL
  OR merchant_id IS NULL
  OR (user_id IS NOT NULL AND NOT regexp_full_match(user_id, '[0-9]+'))
  OR (merchant_id IS NOT NULL AND NOT regexp_full_match(merchant_id, '[0-9]+'))
  OR (coupon_id IS NOT NULL AND NOT regexp_full_match(coupon_id, '[0-9]+'))
  OR (coupon_id IS NOT NULL AND received_date IS NOT NULL AND discount_rate IS NULL)
  OR (coupon_id IS NULL AND discount_rate IS NOT NULL)
  OR coupon_type = 'invalid_discount'
  OR (distance_code IS NOT NULL AND NOT regexp_full_match(distance_code, '(0|[1-9]|10)'))
  OR (received_date IS NOT NULL AND (received_date < (SELECT history_start FROM analysis_config) OR received_date > (SELECT training_observation_end FROM analysis_config)))
  OR (consumed_date IS NOT NULL AND (consumed_date < (SELECT history_start FROM analysis_config) OR consumed_date > (SELECT training_observation_end FROM analysis_config)))
  OR (received_date IS NULL AND consumed_date IS NULL),
  FALSE
 ) AS has_anomaly
 FROM parsed
), classified AS (
 SELECT *,
 NOT has_anomaly AND coupon_id IS NOT NULL AND received_date IS NOT NULL AS is_coupon_receipt,
 CASE WHEN coupon_type='threshold_reduction' THEN threshold_candidate END AS threshold,
 CASE WHEN coupon_type='threshold_reduction' THEN reduction_candidate END AS reduction,
 CASE WHEN coupon_type='direct_discount' THEN direct_candidate END AS direct_payment_fraction,
 CASE WHEN coupon_type='threshold_reduction' THEN reduction_candidate / threshold_candidate
      WHEN coupon_type='direct_discount' THEN 1-direct_candidate END AS nominal_discount_fraction,
 strftime(received_date, '%Y-%m') AS receipt_month,
 date_part('isodow', received_date)::INTEGER AS receipt_weekday,
 coalesce(distance_code, 'unknown') AS distance_group,
 is_coupon_receipt AND received_date + INTERVAL 15 DAY <=
   (SELECT training_observation_end FROM analysis_config) AS mature15,
 CASE WHEN invalid_received_date OR invalid_consumed_date OR coupon_receipt_mismatch
      OR lag_days < 0 OR has_anomaly THEN 'invalid_event'
 WHEN coupon_id IS NULL AND received_date IS NULL AND consumed_date IS NOT NULL THEN 'ordinary_consumption'
 WHEN is_coupon_receipt AND consumed_date IS NULL THEN 'receipt_without_consumption'
 WHEN is_coupon_receipt AND lag_days BETWEEN 0 AND 15 THEN 'redeemed_within15'
 WHEN is_coupon_receipt AND lag_days > 15 THEN 'consumed_after15'
 ELSE 'unclassified' END AS record_state,
 coalesce(is_coupon_receipt AND lag_days BETWEEN 0 AND 15, FALSE) AS redeemed_within15
 FROM coupon_parsed
)
SELECT c.* EXCLUDE (threshold_candidate, reduction_candidate, direct_candidate, has_anomaly),
 c.mature15 AND c.redeemed_within15 AS redeemed15_mature,
 d.exact_duplicate_rank, d.exact_duplicate_group_size, d.first_source_row_number,
 d.exact_duplicate_rank > 1 AS is_exact_duplicate_excess
FROM classified c JOIN offline_duplicate_map d USING (record_id);

CREATE TABLE fact_coupon_receipts AS
SELECT * FROM fact_offline_records WHERE is_coupon_receipt;
CREATE VIEW fact_offline_records_dedup AS
SELECT * FROM fact_offline_records WHERE exact_duplicate_rank=1;
CREATE VIEW fact_coupon_receipts_dedup AS
SELECT * FROM fact_coupon_receipts WHERE exact_duplicate_rank=1;

-- One row per anomaly reason per record; normal missing Date and unknown Distance aren't errors.
CREATE TABLE offline_anomalies AS
SELECT f.record_id, f.source_file, f.source_row_number, x.reason,
 f.user_id, f.merchant_id, f.coupon_id, f.received_text, f.consumed_text,
 f.discount_rate, f.distance_code
FROM fact_offline_records f
CROSS JOIN LATERAL (
 SELECT reason FROM (VALUES
 ('invalid_received_date', f.invalid_received_date),
 ('invalid_consumed_date', f.invalid_consumed_date),
 ('negative_lag', coalesce(f.lag_days<0, FALSE)),
 ('coupon_receipt_mismatch', f.coupon_receipt_mismatch),
 ('missing_user_id', f.user_id IS NULL),
 ('missing_merchant_id', f.merchant_id IS NULL),
 ('invalid_user_id', f.user_id IS NOT NULL AND NOT regexp_full_match(f.user_id, '[0-9]+')),
 ('invalid_merchant_id', f.merchant_id IS NOT NULL AND NOT regexp_full_match(f.merchant_id, '[0-9]+')),
 ('invalid_coupon_id', f.coupon_id IS NOT NULL AND NOT regexp_full_match(f.coupon_id, '[0-9]+')),
 ('missing_receipt_discount', f.coupon_id IS NOT NULL AND f.received_date IS NOT NULL AND f.discount_rate IS NULL),
 ('unexpected_discount_without_coupon', f.coupon_id IS NULL AND f.discount_rate IS NOT NULL),
 ('invalid_discount', f.coupon_type='invalid_discount'),
 ('invalid_distance_code', f.distance_code IS NOT NULL AND NOT regexp_full_match(f.distance_code, '(0|[1-9]|10)')),
 ('received_outside_training', f.received_date IS NOT NULL AND (f.received_date <
     (SELECT history_start FROM analysis_config) OR f.received_date >
     (SELECT training_observation_end FROM analysis_config))),
 ('consumed_outside_training', f.consumed_date IS NOT NULL AND (f.consumed_date <
     (SELECT history_start FROM analysis_config) OR f.consumed_date >
     (SELECT training_observation_end FROM analysis_config))),
 ('unclassified_event', (f.received_date IS NULL AND f.consumed_date IS NULL AND NOT f.invalid_received_date AND NOT f.invalid_consumed_date) OR f.record_state='unclassified')
 ) AS checks(reason, failed) WHERE failed
) x;
