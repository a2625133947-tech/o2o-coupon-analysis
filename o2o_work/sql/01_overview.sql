-- Entire mature offline receipt population; source-record and distinct-user metrics separated.
CREATE TABLE overview AS
SELECT * FROM (
 SELECT count(*) AS source_records,
 count(*) FILTER (WHERE record_state='ordinary_consumption') AS ordinary_consumption_records,
 count(*) FILTER (WHERE consumed_date IS NOT NULL) AS consumption_records,
 count(*) FILTER (WHERE is_coupon_receipt AND consumed_date IS NOT NULL) AS coupon_consumption_records,
 count(*) FILTER (WHERE is_coupon_receipt AND consumed_date IS NOT NULL)::DOUBLE /
 nullif(count(*) FILTER (WHERE consumed_date IS NOT NULL),0) AS coupon_consumption_record_share
 FROM source.fact_offline_records
) CROSS JOIN (
 SELECT count(*) AS receipt_records,
 count(DISTINCT user_id) AS receipt_users,
 sum(redeemed15_mature::BIGINT) AS redeemed15_records,
 count(DISTINCT user_id) FILTER (WHERE redeemed15_mature) AS redeemed15_users,
 sum(redeemed15_mature::BIGINT)::DOUBLE / nullif(count(*),0) AS redemption_rate15,
 count(DISTINCT user_id) FILTER (WHERE redeemed15_mature)::DOUBLE /
 nullif(count(DISTINCT user_id),0) AS redemption_user_share15,
 count(*) < (SELECT small_sample_receipts FROM stage2_config) AS small_sample, min(received_date) AS receipt_start, max(received_date) AS receipt_end
 FROM receipts
);
