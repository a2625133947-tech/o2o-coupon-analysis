-- Frozen rules selected from the HISTORICAL distribution, before evaluating May outcomes.
CREATE TABLE user_segments AS
SELECT *,
 CASE WHEN NOT has_observed_history THEN 'unknown_history'
 WHEN history_mature_receipt_records >= (SELECT high_receipt_min FROM segmentation_rules)
  AND history_redeemed15_records=0 THEN 'high_receipt_zero_redemption'
 WHEN history_consumption_records=0 THEN 'no_observed_consumption'
 WHEN recency_days <= (SELECT recent_days_max FROM segmentation_rules)
  AND history_consumption_records >= (SELECT high_consumption_min FROM segmentation_rules) THEN 'recent_high_frequency'
 WHEN recency_days <= (SELECT recent_days_max FROM segmentation_rules) THEN 'recent_low_frequency'
 ELSE 'not_recent_consumption' END AS segment_id
FROM user_features_asof;
CREATE TABLE segment_coverage AS
SELECT segment_id,count(*) AS universe_users,
 count(*) FILTER (WHERE has_observed_history) AS historical_users,
 sum(history_consumption_records) AS history_consumption_records,
 sum(history_receipt_records) AS history_receipt_records,
 sum(history_mature_receipt_records) AS history_mature_receipt_records,
 sum(history_redeemed15_records) AS history_redeemed15_records,
 sum(history_redeemed15_records)::DOUBLE/nullif(sum(history_mature_receipt_records),0) AS historical_record_redemption_rate15,
 median(recency_days) AS median_recency_days,
 median(history_consumption_records) AS median_consumption_records
FROM user_segments GROUP BY segment_id ORDER BY segment_id;
