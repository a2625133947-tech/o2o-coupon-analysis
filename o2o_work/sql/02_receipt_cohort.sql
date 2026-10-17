-- Attribute all redemption results to receipt month, including cross-month consumption.
CREATE TABLE receipt_cohort AS
SELECT receipt_month, min(received_date) AS receipt_start, max(received_date) AS receipt_end,
 CASE WHEN receipt_month='2016-06' THEN 'partial_month_1_to_15' ELSE 'full_calendar_month' END AS period_scope,
 count(*) AS receipt_records,
 count(DISTINCT user_id) AS receipt_users,
 sum(redeemed15_mature::BIGINT) AS redeemed15_records,
 count(DISTINCT user_id) FILTER (WHERE redeemed15_mature) AS redeemed15_users,
 sum(redeemed15_mature::BIGINT)::DOUBLE / nullif(count(*),0) AS redemption_rate15,
 count(DISTINCT user_id) FILTER (WHERE redeemed15_mature)::DOUBLE /
 nullif(count(DISTINCT user_id),0) AS redemption_user_share15,
 count(*) < (SELECT small_sample_receipts FROM stage2_config) AS small_sample FROM receipts GROUP BY receipt_month ORDER BY receipt_month;
-- Equal calendar-day range for volume comparison; this isn't a seasonality estimate.
CREATE TABLE receipt_cohort_days1_15 AS
SELECT receipt_month, min(received_date) AS receipt_start, max(received_date) AS receipt_end,
 'calendar_days_1_to_15' AS period_scope,
 count(*) AS receipt_records,
 count(DISTINCT user_id) AS receipt_users,
 sum(redeemed15_mature::BIGINT) AS redeemed15_records,
 count(DISTINCT user_id) FILTER (WHERE redeemed15_mature) AS redeemed15_users,
 sum(redeemed15_mature::BIGINT)::DOUBLE / nullif(count(*),0) AS redemption_rate15,
 count(DISTINCT user_id) FILTER (WHERE redeemed15_mature)::DOUBLE /
 nullif(count(DISTINCT user_id),0) AS redemption_user_share15,
 count(*) < (SELECT small_sample_receipts FROM stage2_config) AS small_sample FROM receipts
WHERE day(received_date) <= (SELECT same_day_monthly_max_day FROM stage2_config)
GROUP BY receipt_month ORDER BY receipt_month;
