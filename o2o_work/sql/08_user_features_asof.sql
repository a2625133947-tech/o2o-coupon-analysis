-- Historical features use only event dates strictly before the fixed as-of date.
CREATE TABLE historical_events AS
SELECT record_id,user_id,received_date AS event_date,'receipt' AS event_type
FROM source.fact_offline_records
WHERE is_coupon_receipt AND received_date >= (SELECT history_start FROM stage3_config)
 AND received_date < (SELECT asof_date FROM stage3_config)
UNION ALL
SELECT record_id,user_id,consumed_date AS event_date,'consumption' AS event_type
FROM source.fact_offline_records
WHERE consumed_date >= (SELECT history_start FROM stage3_config)
 AND consumed_date < (SELECT asof_date FROM stage3_config);
CREATE TABLE historical_user_features AS
WITH activity AS (
 SELECT user_id,count(DISTINCT event_date) AS history_active_days,
 count(DISTINCT event_date) FILTER (WHERE event_type='receipt') AS history_receipt_active_days,
 count(DISTINCT event_date) FILTER (WHERE event_type='consumption') AS history_consumption_active_days,
 max(event_date) AS latest_history_event_date
 FROM historical_events GROUP BY user_id
), consumption AS (
 SELECT user_id,count(*) AS history_consumption_records,max(event_date) AS last_consumption_date
 FROM historical_events WHERE event_type='consumption' GROUP BY user_id
), receipts AS (
 SELECT user_id,count(*) AS history_receipt_records,
 count(*) FILTER (WHERE received_date+INTERVAL 15 DAY < (SELECT asof_date FROM stage3_config)) AS history_mature_receipt_records,
 count(*) FILTER (WHERE received_date+INTERVAL 15 DAY >= (SELECT asof_date FROM stage3_config)) AS history_immature_receipt_records,
 count(*) FILTER (WHERE received_date+INTERVAL 15 DAY < (SELECT asof_date FROM stage3_config)
 AND consumed_date < (SELECT asof_date FROM stage3_config)
 AND date_diff('day',received_date,consumed_date) BETWEEN 0 AND 15) AS history_redeemed15_records
 FROM source.fact_offline_records
 WHERE is_coupon_receipt AND received_date >= (SELECT history_start FROM stage3_config)
 AND received_date < (SELECT asof_date FROM stage3_config) GROUP BY user_id
)
SELECT a.*, (SELECT asof_date FROM stage3_config) AS asof_date,
 coalesce(c.history_consumption_records,0) AS history_consumption_records,c.last_consumption_date,
 date_diff('day',c.last_consumption_date,(SELECT asof_date FROM stage3_config)) AS recency_days,
 coalesce(r.history_receipt_records,0) AS history_receipt_records,
 coalesce(r.history_mature_receipt_records,0) AS history_mature_receipt_records,
 coalesce(r.history_immature_receipt_records,0) AS history_immature_receipt_records,
 coalesce(r.history_redeemed15_records,0) AS history_redeemed15_records,
 r.history_redeemed15_records::DOUBLE/nullif(r.history_mature_receipt_records,0) AS history_redemption_rate15
FROM activity a LEFT JOIN consumption c USING(user_id) LEFT JOIN receipts r USING(user_id);
-- Universe is historical users plus later evaluation receipt users; May membership is not a feature.
CREATE TABLE segmentation_universe AS
SELECT user_id FROM historical_user_features
UNION SELECT DISTINCT user_id FROM source.fact_coupon_receipts
WHERE received_date BETWEEN (SELECT evaluation_start FROM stage3_config) AND (SELECT evaluation_end FROM stage3_config);
CREATE TABLE user_features_asof AS
SELECT u.user_id,coalesce(h.asof_date,(SELECT asof_date FROM stage3_config)) AS asof_date,
 h.user_id IS NOT NULL AS has_observed_history,
 coalesce(h.history_active_days,0) AS history_active_days,
 coalesce(h.history_receipt_active_days,0) AS history_receipt_active_days,
 coalesce(h.history_consumption_active_days,0) AS history_consumption_active_days,
 h.latest_history_event_date,
 coalesce(h.history_consumption_records,0) AS history_consumption_records,
 h.last_consumption_date,h.recency_days,
 coalesce(h.history_receipt_records,0) AS history_receipt_records,
 coalesce(h.history_mature_receipt_records,0) AS history_mature_receipt_records,
 coalesce(h.history_immature_receipt_records,0) AS history_immature_receipt_records,
 coalesce(h.history_redeemed15_records,0) AS history_redeemed15_records,
 h.history_redemption_rate15
FROM segmentation_universe u LEFT JOIN historical_user_features h USING(user_id);
