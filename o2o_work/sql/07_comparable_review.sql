-- Descriptive comparison on common-support strata; never a causal treatment effect.
-- comparison_specs is an explicit selection made after viewing the six primary tables.
CREATE TABLE comparison_cells AS
WITH expanded AS (
 SELECT s.comparison_id, s.dimension,
 CASE s.dimension WHEN 'coupon_type' THEN r.coupon_type
  WHEN 'discount_rate' THEN r.discount_rate WHEN 'distance' THEN r.distance_group
  WHEN 'receipt_month' THEN r.receipt_month
  WHEN 'merchant' THEN CASE WHEN r.merchant_id=s.group_a THEN s.group_a ELSE s.group_b END END AS group_value,
 s.group_a, s.group_b,
 CASE WHEN s.dimension='receipt_month' THEN 'all_months' ELSE r.receipt_month END AS stratum_month,
 CASE WHEN s.dimension='coupon_type' THEN r.nominal_discount_band
      WHEN s.dimension='discount_rate' THEN r.coupon_type || ':threshold=' || coalesce(r.threshold::VARCHAR,'none')
      ELSE r.discount_rate END AS stratum_scheme,
 CASE WHEN s.dimension='merchant' THEN 'all_merchants' ELSE r.merchant_id END AS stratum_merchant,
 CASE WHEN s.dimension='distance' THEN 'all_distances' ELSE r.distance_group END AS stratum_distance,
 r.redeemed15_mature
 FROM receipts r CROSS JOIN comparison_specs s
), scoped AS (SELECT * FROM expanded WHERE group_value IN (group_a, group_b))
SELECT comparison_id, CASE WHEN group_value=group_a THEN 'a' ELSE 'b' END AS group_side,
 stratum_month, stratum_scheme, stratum_merchant, stratum_distance,
 count(*) AS receipt_records, sum(redeemed15_mature::BIGINT) AS redeemed15_records,
 sum(redeemed15_mature::BIGINT)::DOUBLE / nullif(count(*),0) AS redemption_rate15
FROM scoped GROUP BY comparison_id,group_side,stratum_month,stratum_scheme,stratum_merchant,stratum_distance;

CREATE TABLE comparison_strata AS
SELECT comparison_id,stratum_month,stratum_scheme,stratum_merchant,stratum_distance,
 coalesce(a.receipt_records,0) AS a_receipts, coalesce(a.redeemed15_records,0) AS a_redeemed15,
 coalesce(b.receipt_records,0) AS b_receipts, coalesce(b.redeemed15_records,0) AS b_redeemed15,
 a.redemption_rate15 AS a_rate15, b.redemption_rate15 AS b_rate15,
 a.receipt_records IS NOT NULL AND b.receipt_records IS NOT NULL AS has_common_support,
 coalesce(a.receipt_records >= (SELECT comparison_min_receipts_each_stratum FROM stage2_config)
  AND b.receipt_records >= (SELECT comparison_min_receipts_each_stratum FROM stage2_config),FALSE) AS adequate_common_support
FROM (SELECT * FROM comparison_cells WHERE group_side='a') a
FULL JOIN (SELECT * FROM comparison_cells WHERE group_side='b') b
USING (comparison_id,stratum_month,stratum_scheme,stratum_merchant,stratum_distance);

CREATE TABLE comparison_summary AS
WITH full_groups AS (
 SELECT comparison_id,
 sum(receipt_records) FILTER (WHERE group_side='a') AS a_all_receipts,
 sum(redeemed15_records) FILTER (WHERE group_side='a') AS a_all_redeemed15,
 sum(receipt_records) FILTER (WHERE group_side='b') AS b_all_receipts,
 sum(redeemed15_records) FILTER (WHERE group_side='b') AS b_all_redeemed15
 FROM comparison_cells GROUP BY comparison_id
), support AS (
 SELECT *, 'common_min1' AS support_rule FROM comparison_strata WHERE has_common_support
 UNION ALL
 SELECT *, 'common_min30_each' AS support_rule FROM comparison_strata WHERE adequate_common_support
), standardized AS (
 SELECT comparison_id,support_rule,count(*) AS included_strata,
 sum(a_receipts) AS a_common_receipts,sum(a_redeemed15) AS a_common_redeemed15,
 sum(b_receipts) AS b_common_receipts,sum(b_redeemed15) AS b_common_redeemed15,
 sum(a_receipts+b_receipts) AS standard_weight_denominator,
 sum((a_receipts+b_receipts)*a_rate15) AS a_standardized_numerator_receipt_equivalents,
 sum((a_receipts+b_receipts)*b_rate15) AS b_standardized_numerator_receipt_equivalents,
 count(*) FILTER (WHERE a_rate15>b_rate15) AS strata_with_a_higher_rate
 FROM support GROUP BY comparison_id,support_rule
), rules AS (SELECT support_rule FROM (VALUES ('common_min1'),('common_min30_each')) AS v(support_rule))
SELECT s.*, r.support_rule,
 coalesce(f.a_all_receipts,0) AS a_all_receipts,
 coalesce(f.a_all_redeemed15,0) AS a_all_redeemed15,
 coalesce(f.b_all_receipts,0) AS b_all_receipts,
 coalesce(f.b_all_redeemed15,0) AS b_all_redeemed15,
 f.a_all_redeemed15::DOUBLE/nullif(f.a_all_receipts,0) AS a_all_rate15,
 f.b_all_redeemed15::DOUBLE/nullif(f.b_all_receipts,0) AS b_all_rate15,
 coalesce(z.included_strata,0) AS included_strata,
 coalesce(z.a_common_receipts,0) AS a_common_receipts,coalesce(z.a_common_redeemed15,0) AS a_common_redeemed15,
 coalesce(z.b_common_receipts,0) AS b_common_receipts,coalesce(z.b_common_redeemed15,0) AS b_common_redeemed15,
 z.a_common_redeemed15::DOUBLE/nullif(z.a_common_receipts,0) AS a_common_raw_rate15,
 z.b_common_redeemed15::DOUBLE/nullif(z.b_common_receipts,0) AS b_common_raw_rate15,
 coalesce(z.a_common_receipts,0)::DOUBLE/nullif(coalesce(f.a_all_receipts,0),0) AS a_coverage,
 coalesce(z.b_common_receipts,0)::DOUBLE/nullif(coalesce(f.b_all_receipts,0),0) AS b_coverage,
 z.standard_weight_denominator, z.a_standardized_numerator_receipt_equivalents,
 z.b_standardized_numerator_receipt_equivalents,
 z.a_standardized_numerator_receipt_equivalents/nullif(z.standard_weight_denominator,0) AS a_standardized_rate15,
 z.b_standardized_numerator_receipt_equivalents/nullif(z.standard_weight_denominator,0) AS b_standardized_rate15,
 coalesce(z.strata_with_a_higher_rate,0) AS strata_with_a_higher_rate
FROM comparison_specs s CROSS JOIN rules r LEFT JOIN full_groups f USING(comparison_id)
LEFT JOIN standardized z USING(comparison_id,support_rule)
ORDER BY comparison_id,support_rule;
