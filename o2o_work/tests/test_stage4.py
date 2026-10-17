"""Stage 4 semantic, aggregation, distinct-user, candidate-3381, and Tableau XSD validation unit tests."""
from pathlib import Path
import math
import tempfile
import unittest
import xml.etree.ElementTree as E
import duckdb

WORK = Path(__file__).resolve().parents[1]
import sys
if str(WORK / 'scripts') not in sys.path:
    sys.path.insert(0, str(WORK / 'scripts'))

from stage4 import BAND_LABELS
from stage4_workbook import validate_twb_against_tableau_xsd


def run_tests():
    checks = []
    with duckdb.connect(':memory:') as c:
        c.execute(
            """
            CREATE TABLE synthetic_receipts(
                record_id INT,
                user_id VARCHAR,
                merchant_id VARCHAR,
                coupon_id VARCHAR,
                received_date DATE,
                receipt_month VARCHAR,
                coupon_type VARCHAR,
                discount_rate VARCHAR,
                threshold DOUBLE,
                nominal_discount_fraction DOUBLE,
                discount_band VARCHAR,
                distance_group VARCHAR,
                lag_days INT,
                redeemed15_mature BOOLEAN,
                segment_id VARCHAR,
                segment_name VARCHAR
            )
            """
        )
        rows = [
            (1, 'u1', 'm1', 'c1', '2016-01-05', '2016-01', 'threshold_reduction', '100:10', 100.0, 0.10, '5_to_10pct', '0', None, False, 'recent_high_frequency', '近期高频消费'),
            (2, 'u1', 'm1', 'c2', '2016-05-02', '2016-05', 'direct_discount', '0.9', None, 0.10, '5_to_10pct', '1', 0, True, 'recent_high_frequency', '近期高频消费'),
            (3, 'u1', 'm1', 'c3', '2016-05-10', '2016-05', 'threshold_reduction', '200:50', 200.0, 0.25, '20_to_30pct', 'unknown', 3, True, 'recent_high_frequency', '近期高频消费'),
            (4, 'u2', 'm2', 'c4', '2016-05-12', '2016-05', 'threshold_reduction', '20:1', 20.0, 0.05, 'le_5pct', 'unknown', 15, True, 'unknown_history', '未知历史'),
            (5, 'u3', 'm2', 'c5', '2016-06-05', '2016-06', 'threshold_reduction', '50:20', 50.0, 0.40, 'gt_30pct', '2', None, False, 'unknown_history', '未知历史'),
        ]
        c.executemany('INSERT INTO synthetic_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)

        # 1. Non-additive distinct users across months and coupon types
        total_users = c.execute('SELECT count(DISTINCT user_id) FROM synthetic_receipts').fetchone()[0]
        sum_month_users = c.execute(
            'SELECT sum(u) FROM (SELECT receipt_month, count(DISTINCT user_id) u FROM synthetic_receipts GROUP BY receipt_month)'
        ).fetchone()[0]
        assert total_users == 3 and sum_month_users == 4
        checks.append({'name': 'distinct_users_non_additive_across_months', 'passed': True, 'total_users': total_users, 'sum_month_users': sum_month_users})

        # 2. Empty scenario zero-denominator safety
        empty_row = c.execute(
            """
            SELECT count(*) AS n,
                   coalesce(sum(redeemed15_mature::INT), 0) AS y,
                   CASE WHEN count(*) > 0 THEN sum(redeemed15_mature::INT)::DOUBLE / count(*) ELSE NULL END AS rate15
            FROM synthetic_receipts WHERE merchant_id = '__NO_MATCH__'
            """
        ).fetchone()
        assert empty_row == (0, 0, None)
        checks.append({'name': 'empty_filter_zero_denominator_returns_null', 'passed': True})

        # 3. Lag day 0..15 cumulative and share of successful redemptions
        lag_counts = dict(
            c.execute('SELECT lag_days, count(*) FROM synthetic_receipts WHERE redeemed15_mature GROUP BY lag_days').fetchall()
        )
        total_n, total_y = c.execute('SELECT count(*), sum(redeemed15_mature::INT) FROM synthetic_receipts').fetchone()
        cum = 0
        shares = []
        for d in range(16):
            dn = lag_counts.get(d, 0)
            cum += dn
            shares.append(dn / total_y)
        assert cum == total_y == 3
        assert math.isclose(sum(shares), 1.0, rel_tol=0, abs_tol=1e-12)
        checks.append({'name': 'lag_cumulative_day15_and_success_share_sum', 'passed': True, 'day15_cumulative': cum})

        # 4. Logical discount_band ordering & Chinese readable labels (≤5% < (5%, 10%] < (10%, 20%] < (20%, 30%] < >30%)
        band_orders = c.execute(
            """
            SELECT CASE discount_band
                       WHEN 'le_5pct' THEN '≤5%'
                       WHEN '5_to_10pct' THEN '(5%, 10%]'
                       WHEN '10_to_20pct' THEN '(10%, 20%]'
                       WHEN '20_to_30pct' THEN '(20%, 30%]'
                       WHEN 'gt_30pct' THEN '>30%'
                   END AS label,
                   CASE discount_band
                       WHEN 'le_5pct' THEN 0
                       WHEN '5_to_10pct' THEN 1
                       WHEN '10_to_20pct' THEN 2
                       WHEN '20_to_30pct' THEN 3
                       WHEN 'gt_30pct' THEN 4
                       ELSE 99
                   END AS ord
            FROM (VALUES ('gt_30pct'), ('le_5pct'), ('20_to_30pct'), ('5_to_10pct'), ('10_to_20pct')) t(discount_band)
            ORDER BY ord
            """
        ).fetchall()
        assert [b for b, _ in band_orders] == [BAND_LABELS[k] for k in ['le_5pct', '5_to_10pct', '10_to_20pct', '20_to_30pct', 'gt_30pct']]
        checks.append({'name': 'discount_band_logical_ordering_and_readable_labels', 'passed': True})

        # 5. Segment priority ordering matches Stage 3 PRIORITIES
        from stage3 import PRIORITIES
        case_sql = 'CASE segment_id ' + ' '.join(f"WHEN '{s}' THEN {i}" for i, s in enumerate(PRIORITIES)) + ' ELSE 99 END'
        seg_orders = c.execute(
            f"""
            SELECT segment_id, {case_sql} AS ord
            FROM (VALUES ('not_recent_consumption'), ('recent_high_frequency'), ('unknown_history'),
                         ('recent_low_frequency'), ('no_observed_consumption'), ('high_receipt_zero_redemption')) t(segment_id)
            ORDER BY ord
            """
        ).fetchall()
        assert [s for s, _ in seg_orders] == list(PRIORITIES)
        checks.append({'name': 'segment_priority_ordering', 'passed': True})

        # 6. Candidate merchant SQL includes Top 20 PLUS '3381' when ranked > 20
        c.execute('CREATE TABLE synth_merchants(merchant_id VARCHAR, n INT)')
        c.executemany(
            'INSERT INTO synth_merchants VALUES (?,?)',
            [(f'm{i:02d}', 1000 - i) for i in range(1, 25)] + [('3381', 53)],
        )
        cand_rows = c.execute(
            """
            WITH ranked AS (
                SELECT merchant_id AS category, sum(n) AS receipt_records,
                       row_number() OVER (ORDER BY sum(n) DESC, merchant_id ASC) AS rk
                FROM synth_merchants GROUP BY merchant_id
            )
            SELECT category, rk FROM ranked WHERE rk <= 20 OR category = '3381' ORDER BY rk
            """
        ).fetchall()
        assert len(cand_rows) == 21 and cand_rows[-1] == ('3381', 25)
        checks.append({'name': 'candidate_merchant_includes_top20_plus_3381', 'passed': True})

    # 7. Verify XSD Schema validator accepts valid workbook and rejects malformed .twb
    probe_twb = WORK / 'tableau/probe/connection_probe.twb'
    res_ok = validate_twb_against_tableau_xsd([probe_twb])
    assert res_ok['status'] == 'PASS'
    with tempfile.TemporaryDirectory() as tmp:
        bad_twb = Path(tmp) / 'bad.twb'
        doc = E.parse(probe_twb).getroot()
        pane = doc.find('.//pane')
        E.SubElement(pane, 'customized-label')
        E.SubElement(pane, 'customized-tooltip')
        E.ElementTree(doc).write(bad_twb, encoding='utf-8', xml_declaration=True)
        rejected = False
        try:
            validate_twb_against_tableau_xsd([bad_twb])
        except Exception:
            rejected = True
        assert rejected, 'XSD validator must reject customized-label before customized-tooltip'
    checks.append({'name': 'tableau_xsd_validator_accepts_valid_and_rejects_invalid', 'passed': True})

    return {'passed': all(c['passed'] for c in checks), 'checks_count': len(checks), 'checks': checks}


class TestStage4(unittest.TestCase):
    def test_stage4_suite(self):
        res = run_tests()
        self.assertTrue(res['passed'])
        self.assertEqual(res['checks_count'], 7)


if __name__ == '__main__':
    unittest.main()
