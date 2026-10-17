-- Full source-record reconciliation, not stage2 business findings.
SELECT count(*) AS rows,
 count(*) FILTER (WHERE is_coupon_receipt) AS receipt_records,
 count(*) FILTER (WHERE is_coupon_receipt AND consumed_date IS NOT NULL) AS receipt_with_consumption,
 count(*) FILTER (WHERE redeemed_within15) AS redeemed_within15,
 count(*) FILTER (WHERE is_coupon_receipt AND consumed_date IS NULL) AS receipt_without_consumption,
 count(*) FILTER (WHERE record_state='ordinary_consumption') AS consumption_without_receipt,
 count(*) FILTER (WHERE received_date IS NULL AND consumed_date IS NULL) AS neither_date,
 count(*) FILTER (WHERE lag_days<0) AS negative_lag,
 count(*) FILTER (WHERE coupon_receipt_mismatch) AS coupon_receipt_mismatch,
 count(*) FILTER (WHERE is_coupon_receipt AND lag_days>15) AS lag_over15,
 count(*) FILTER (WHERE mature15) AS mature_receipts,
 count(*) FILTER (WHERE redeemed15_mature) AS mature_redeemed15,
 count(*) FILTER (WHERE is_coupon_receipt AND NOT mature15) AS immature_receipts,
 count(*) FILTER (WHERE is_exact_duplicate_excess) AS exact_duplicate_excess,
 count(*) FILTER (WHERE invalid_received_date) AS invalid_received_date,
 count(*) FILTER (WHERE invalid_consumed_date) AS invalid_consumed_date,
 count(DISTINCT record_id) AS distinct_record_ids,
 count(DISTINCT user_id) AS users,
 min(received_date) AS received_min, max(received_date) AS received_max,
 min(consumed_date) AS consumed_min, max(consumed_date) AS consumed_max,
 count(*) FILTER (WHERE redeemed15_mature)::DOUBLE / nullif(count(*) FILTER (WHERE mature15),0)
 AS redemption_rate15
FROM fact_offline_records;
