-- May is strictly evaluation-only. No May fields are used to define historical segments.
CREATE TABLE evaluation_receipts AS
SELECT r.record_id,r.source_file,r.source_row_number,r.user_id,r.merchant_id,
 r.received_date,r.consumed_date,r.discount_rate,r.distance_group,
 date_diff('day',r.received_date,r.consumed_date) AS evaluation_lag_days,
 r.received_date+INTERVAL 15 DAY <= (SELECT evaluation_observation_end FROM stage3_config) AS evaluation_mature15,
 coalesce(r.consumed_date <= (SELECT evaluation_observation_end FROM stage3_config)
 AND date_diff('day',r.received_date,r.consumed_date) BETWEEN 0 AND 15,FALSE) AS evaluation_redeemed15,
 s.segment_id
FROM source.fact_coupon_receipts r LEFT JOIN user_segments s USING(user_id)
WHERE r.received_date BETWEEN (SELECT evaluation_start FROM stage3_config) AND (SELECT evaluation_end FROM stage3_config);
CREATE TABLE evaluation_user_outcomes AS
SELECT user_id,asof_date,segment_id,count(*) AS receipt_records,
 count(*) FILTER (WHERE evaluation_mature15) AS mature_receipt_records,
 count(*) FILTER (WHERE evaluation_mature15 AND evaluation_redeemed15) AS redeemed15_records,
 bool_or(evaluation_mature15 AND evaluation_redeemed15) AS redeemed15_any,
 count(*) FILTER (WHERE evaluation_mature15 AND evaluation_redeemed15)::DOUBLE /
 nullif(count(*) FILTER (WHERE evaluation_mature15),0) AS within_user_redemption_rate15
FROM evaluation_receipts JOIN (SELECT user_id,asof_date FROM user_segments) USING(user_id)
GROUP BY user_id,asof_date,segment_id;
CREATE TABLE segment_evaluation AS
SELECT c.segment_id,c.universe_users,
 count(r.record_id) AS evaluation_receipt_records,
 count(*) FILTER (WHERE r.evaluation_mature15) AS mature_receipt_records,
 count(*) FILTER (WHERE r.evaluation_mature15 AND r.evaluation_redeemed15) AS redeemed15_records,
 count(*) FILTER (WHERE r.evaluation_mature15 AND r.evaluation_redeemed15)::DOUBLE/
 nullif(count(*) FILTER (WHERE r.evaluation_mature15),0) AS record_redemption_rate15,
 count(DISTINCT r.user_id) AS evaluation_receipt_users,
 count(DISTINCT r.user_id) FILTER (WHERE r.evaluation_mature15) AS mature_receipt_users,
 count(DISTINCT r.user_id) FILTER (WHERE r.evaluation_mature15 AND r.evaluation_redeemed15) AS redeemed15_users,
 count(DISTINCT r.user_id) FILTER (WHERE r.evaluation_mature15 AND r.evaluation_redeemed15)::DOUBLE/
 nullif(count(DISTINCT r.user_id) FILTER (WHERE r.evaluation_mature15),0) AS redemption_user_share15,
 count(DISTINCT r.user_id)::DOUBLE/nullif(c.universe_users,0) AS may_receipt_user_coverage,
 count(*) FILTER (WHERE r.evaluation_mature15)<100 AS small_receipt_sample
FROM segment_coverage c LEFT JOIN evaluation_receipts r USING(segment_id)
GROUP BY c.segment_id,c.universe_users ORDER BY c.segment_id;
CREATE TABLE segment_user_evaluation AS
SELECT c.segment_id,count(u.user_id) AS evaluation_users,
 coalesce(sum(u.mature_receipt_records),0) AS mature_receipt_records,
 coalesce(sum(u.redeemed15_records),0) AS redeemed15_records,
 count(u.user_id) FILTER (WHERE u.redeemed15_any) AS redeemed15_users,
 avg(u.within_user_redemption_rate15) AS mean_within_user_redemption_rate15,
 sum(u.within_user_redemption_rate15) AS sum_within_user_redemption_rate15,
 sum(u.mature_receipt_records)::DOUBLE/nullif(count(u.user_id),0) AS receipts_per_user,
 sum(u.redeemed15_records)::DOUBLE/nullif(sum(u.mature_receipt_records),0) AS record_redemption_rate15,
 count(u.user_id) FILTER (WHERE u.redeemed15_any)::DOUBLE/nullif(count(u.user_id),0) AS redemption_user_share15
FROM segment_coverage c LEFT JOIN evaluation_user_outcomes u USING(segment_id)
GROUP BY c.segment_id ORDER BY c.segment_id;
