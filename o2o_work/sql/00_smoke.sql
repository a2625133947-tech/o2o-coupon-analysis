-- 仅源文件前10000行，证明导入、类型转换与真实SQL执行；不是全量分析结果。
CREATE OR REPLACE TABLE smoke_typed AS
SELECT *,
  try_strptime(Date_received, '%Y%m%d')::DATE AS received_date,
  try_strptime(Date, '%Y%m%d')::DATE AS consumed_date
FROM smoke_raw;
CREATE OR REPLACE VIEW smoke_receipts AS
SELECT *, date_diff('day', received_date, consumed_date) AS lag_days,
       received_date + INTERVAL 15 DAY <= (SELECT observation_end FROM smoke_config) AS mature15
FROM smoke_typed
WHERE Coupon_id IS NOT NULL AND received_date IS NOT NULL;
SELECT
 (SELECT count(*) FROM smoke_raw) AS sample_records,
 count(*) AS receipt_records,
 count(DISTINCT User_id) AS receipt_users,
 count(*) FILTER (WHERE mature15) AS mature_receipts,
 count(*) FILTER (WHERE mature15 AND lag_days BETWEEN 0 AND 15) AS redeemed15,
 count(*) FILTER (WHERE mature15 AND lag_days BETWEEN 0 AND 15)::DOUBLE
   / nullif(count(*) FILTER (WHERE mature15),0) AS redemption_rate15
FROM smoke_receipts;
