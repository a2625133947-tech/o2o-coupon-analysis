"""Stage 4: Prepare audited single-table Tableau sources, Hyper extracts, native workbooks, and SQL reconciliation."""
from pathlib import Path
from datetime import date, datetime
from zoneinfo import ZoneInfo
import csv
import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import duckdb
from verification import compare_database_tables, digest, require_stage

WORK = Path(__file__).resolve().parents[1]
ROOT = WORK.parent
from stage4_dependencies import resolve_tool
STANDALONE_TABLES = [
    'chart_data',
    'filter_expected',
    'candidate_merchant_structure',
    'fixed_receipt_cohort_days1_15',
    'fixed_coupon_scheme_full',
    'fixed_segment_evaluation',
    'fixed_may_bootstrap',
]
BAND_LABELS = {
    'le_5pct': '≤5%',
    '5_to_10pct': '(5%, 10%]',
    '10_to_20pct': '(10%, 20%]',
    '20_to_30pct': '(20%, 30%]',
    'gt_30pct': '>30%',
}


def now():
    return datetime.now(ZoneInfo('Asia/Singapore')).isoformat()


def dump(p, o):
    Path(p).write_text(json.dumps(o, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')


class Audit:
    def __init__(self, qa):
        self.qa = Path(qa)
        self.checks = []
        self.logs = []

    def log(self, msg):
        entry = {'ts': now(), 'message': msg}
        self.logs.append(entry)
        dump(self.qa / 'execution_log.json', self.logs)

    def check(self, name, actual, expected, tolerance=1e-12):
        if isinstance(actual, float) and isinstance(expected, float):
            passed = math.isfinite(actual) and math.isfinite(expected) and math.isclose(actual, expected, rel_tol=0, abs_tol=tolerance)
        else:
            passed = actual == expected
        self.checks.append({'name': name, 'actual': actual, 'expected': expected, 'status': 'PASS' if passed else 'FAIL'})
        if not passed:
            dump(self.qa / 'checks.json', self.checks)
            raise RuntimeError(f'Check failed: {name}: actual={actual!r} != expected={expected!r}')


def rows(c, sql):
    q = c.execute(sql)
    return [dict(zip([x[0] for x in q.description], r)) for r in q.fetchall()]


def export(c, sql, p):
    q = c.execute(sql)
    with Path(p).open('w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow([d[0] for d in q.description])
        while b := q.fetchmany(25000):
            w.writerows(b)


def build_hyper_extract(csv_path, hyper_path):
    """Compile a CSV into a Tableau .hyper extract using Tableau Public's built-in hyperd engine and reconcile."""
    csv_path = Path(csv_path)
    hyper_path = Path(hyper_path)
    hyper_path.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as log_dir:
        subprocess.run(
            [
                str(resolve_tool('hyperd')),
                f'--log-dir={log_dir}',
                '--init-user=tableau_internal_user',
                '--no-password=true',
                '--skip-license=true',
                'shell',
            ],
            input=f'CREATE DATABASE "{hyper_path}" WITH VERSION 2;\n',
            text=True,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        sql = f"""
CREATE SCHEMA "Extract";
CREATE TABLE "Extract"."Extract" (
    "scenario" TEXT COLLATE "zh_Hans_CN",
    "view_id" TEXT COLLATE "zh_Hans_CN",
    "category" TEXT COLLATE "zh_Hans_CN",
    "category_order" BIGINT,
    "receipt_records" BIGINT,
    "redeemed15_records" BIGINT,
    "receipt_users" BIGINT,
    "redeemed15_users" BIGINT,
    "cumulative_redeemed" BIGINT,
    "successful_receipts" BIGINT,
    "sample_note" TEXT COLLATE "zh_Hans_CN"
);
COPY "Extract"."Extract" FROM '{csv_path}' WITH (FORMAT csv, HEADER true, DELIMITER ',');
SELECT count(*), coalesce(sum("receipt_records") FILTER (WHERE "view_id"='kpi'), 0), coalesce(sum("redeemed15_records") FILTER (WHERE "view_id"='kpi'), 0) FROM "Extract"."Extract";
"""
        res = subprocess.run(
            [
                str(resolve_tool('hyperd')),
                f'--log-dir={log_dir}',
                '--init-user=tableau_internal_user',
                '--no-password=true',
                '--skip-license=true',
                '-d',
                str(hyper_path),
                'shell',
            ],
            input=sql,
            text=True,
            check=True,
            capture_output=True,
        )
        combined = (res.stdout or '') + '\n' + (res.stderr or '')
        lines = [ln.strip() for ln in combined.splitlines() if ln.strip() and ln.strip()[0].isdigit() and 'rows' not in ln and 'row' not in ln]
        parts = [int(x) for x in lines[-1].split()]
        return {'rows': parts[0], 'kpi_receipt_sum': parts[1], 'kpi_redeemed_sum': parts[2]}


def independent_verify_scenarios(source_csv, user_segments_csv, scenarios, may_scenarios, seg_names):
    """Independently compute all 23 filter scenarios and lag days 0..15 from raw offline CSV and Stage 3 user_segments.csv."""
    seg_map = {}
    with Path(user_segments_csv).open(encoding='utf-8', newline='') as f:
        for r in csv.DictReader(f):
            seg_map[r['user_id']] = r['segment_id']

    ov_stats = {label: [0, 0, set(), set()] for label, _ in scenarios}
    may_stats = {label: [0, 0, set(), set()] for label, _ in may_scenarios}
    lag_daily = {d: 0 for d in range(16)}

    with Path(source_csv).open(encoding='utf-8', newline='') as f:
        for r in csv.DictReader(f):
            cid = r['Coupon_id']
            drec = r['Date_received']
            if cid in ('null', '') or drec in ('null', ''):
                continue
            uid = r['User_id']
            mid = r['Merchant_id']
            drate = r['Discount_rate']
            dist = 'unknown' if r['Distance'] in ('null', '') else r['Distance']
            ctype = 'threshold_reduction' if ':' in drate else 'direct_discount'
            rec_date = date(int(drec[:4]), int(drec[4:6]), int(drec[6:8]))
            rmonth = f'{rec_date.year:04d}-{rec_date.month:02d}'
            dcon = r['Date']
            redeemed15 = False
            if dcon not in ('null', ''):
                con_date = date(int(dcon[:4]), int(dcon[4:6]), int(dcon[6:8]))
                lag = (con_date - rec_date).days
                if 0 <= lag <= 15:
                    redeemed15 = True
                    lag_daily[lag] += 1

            ov_matches = ['全部']
            m_idx = int(drec[4:6])
            if 1 <= m_idx <= 6:
                ov_matches.append(f'{m_idx}月')
            if ctype == 'direct_discount':
                ov_matches.append('直接折扣')
                if rmonth == '2016-05':
                    ov_matches.append('5月·直接折扣')
            else:
                ov_matches.append('满减')
            if dist == 'unknown':
                ov_matches.append('未知距离')
            if mid == '3381':
                ov_matches.append('商家3381')

            for sc in ov_matches:
                st = ov_stats[sc]
                st[0] += 1
                st[2].add(uid)
                if redeemed15:
                    st[1] += 1
                    st[3].add(uid)

            if rmonth == '2016-05':
                seg_id = seg_map[uid]
                seg_label = seg_names[seg_id]
                may_matches = ['5月全部', seg_label]
                if mid == '3381':
                    may_matches.append('商家3381')
                if dist == 'unknown':
                    may_matches.append('未知距离')
                for sc in may_matches:
                    st = may_stats[sc]
                    st[0] += 1
                    st[2].add(uid)
                    if redeemed15:
                        st[1] += 1
                        st[3].add(uid)

    out = {}
    for scope, d in [('overview', ov_stats), ('may', may_stats)]:
        for sc, st in d.items():
            n, y, u_n, u_y = st[0], st[1], len(st[2]), len(st[3])
            out[(scope, sc)] = {
                'receipt_records': n,
                'redeemed15_records': y,
                'receipt_users': u_n,
                'redeemed15_users': u_y,
                'rate15': (y / n) if n else None,
                'user_share15': (u_y / u_n) if u_n else None,
            }
    return out, lag_daily


def update_state(status, details):
    p = WORK / 'qa/stage_status.json'
    state = json.loads(p.read_text(encoding='utf-8'))
    state['updated_at'] = now()
    state['stage4'] = status
    state['stages']['4'] = status
    state['stage4_details'] = details
    dump(p, state)


NATIVE_CHECKS = ('native_open', 'three_dashboards', 'representative_filters',
                 'clear_reset', 'save_close_reopen', 'visual_inspection')


def validate_native_evidence(evidence_path, workbook):
    """Reject stale, incomplete or unsupported acceptance; no GUI is invoked."""
    evidence_path = Path(evidence_path).resolve()
    evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
    if evidence.get('workbook_sha256') != digest(workbook):
        raise ValueError('原生证据与当前工作簿 SHA256 不一致')
    checks = evidence.get('checks', {})
    if any(checks.get(name) != 'PASS' for name in NATIVE_CHECKS):
        raise ValueError('原生证据缺少加载、三页、筛选、清除复位、保存重开或视觉检查 PASS')
    if not evidence.get('evidence_files'):
        raise ValueError('原生证据必须绑定实际证据文件')
    screenshot_count = 0
    for item in evidence['evidence_files']:
        path = Path(item['path'])
        if not path.is_absolute():
            path = evidence_path.parent / path
        if not path.is_file() or digest(path) != item.get('sha256'):
            raise ValueError(f'原生证据文件缺失或哈希不符: {path}')
        item['path'] = str(path.resolve())
        if path.suffix.lower() in ('.png', '.jpg', '.jpeg'):
            with path.open('rb') as handle:
                header = handle.read(8)
            if not (header == b'\x89PNG\r\n\x1a\n' or header[:3] == b'\xff\xd8\xff'):
                raise ValueError(f'截图文件格式无效: {path}')
            screenshot_count += 1
    if not screenshot_count:
        raise ValueError('原生证据至少需要一个真实截图文件')
    return evidence


def accept_native_evidence(evidence_path):
    latest_path = WORK / 'qa/stage4/latest.json'
    details = json.loads(latest_path.read_text(encoding='utf-8'))
    if any(details.get(k) != 'PASS' for k in
           ('data_preparation', 'hyper_extract_generation', 'workbook_xsd_validation')):
        raise ValueError('数据、Hyper与XSD未通过，不能登记原生PASS')
    # The top package is the declared current delivery; run packages are immutable
    # build provenance and may differ bytewise because generation uses UUIDs.
    workbook = Path(details['workbooks']['top_twbx']).resolve()
    if not workbook.is_file():
        raise ValueError(f'当前交付包不存在: {workbook}')
    evidence = validate_native_evidence(evidence_path, workbook)
    qa = Path(details['qa_dir'])
    dump(qa / 'native_acceptance.json', evidence)
    details.update(stage4='PASS', final_acceptance='PASS',
                   accepted_workbook_path=str(workbook), tableau_native_open='PASS',
                   tableau_filter_reconciliation='PASS', visual_inspection='PASS',
                   native_evidence_sha256=digest(qa / 'native_acceptance.json'),
                   native_workbook_sha256=digest(workbook),
                   tableau_runtime={'status': 'PASS', 'checks': evidence['checks'],
                                    'evidence_path': str(qa / 'native_acceptance.json')})
    for path in (qa / 'status.json', latest_path, WORK / 'tableau/latest.json'):
        dump(path, details)
    update_state('PASS', details)
    # Preserve the prepare report as an audit trail; explicitly record this later acceptance.
    acceptance_note = ('\n\n## 后续原生验收登记\n\n'
                       f'本次工作簿 SHA256 `{digest(workbook)}` 的六项原生验收通过。'
                       f'证据：`{qa / "native_acceptance.json"}`。此登记不表示人工独立复现。\n')
    for path in (qa / '验收报告.md', WORK / 'qa/stage4/验收报告.md'):
        if path.is_file():
            with path.open('a', encoding='utf-8') as handle:
                handle.write(acceptance_note)
    print(json.dumps(details, ensure_ascii=False, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(description='阶段4默认只重建数据与工作簿，不执行桌面UI。')
    parser.add_argument('--accept-native-evidence', type=Path,
                        help='只登记 latest top_twbx 当前交付包的独立原生验收证据，不重建、不运行UI')
    args = parser.parse_args()
    if args.accept_native_evidence:
        return accept_native_evidence(args.accept_native_evidence)
    if sys.prefix == sys.base_prefix:
        raise RuntimeError('必须在项目隔离环境运行 (.venv)')
    if duckdb.__version__ != '1.4.3':
        raise RuntimeError(f'DuckDB版本不匹配: {duckdb.__version__}')

    runid = datetime.now(ZoneInfo('Asia/Singapore')).strftime('%Y%m%dT%H%M%S%f')
    out = WORK / 'tableau/runs' / runid
    qa = WORK / 'qa/stage4/runs' / runid
    out.mkdir(parents=True)
    qa.mkdir(parents=True)
    (out / 'data').mkdir()
    (out / 'workbook').mkdir()
    started = time.monotonic()
    a = Audit(qa)
    details = {'stage4': 'RUNNING', 'run_id': runid, 'started_at': now(), 'results_dir': str(out), 'qa_dir': str(qa)}

    try:
        state = json.loads((WORK / 'qa/stage_status.json').read_text(encoding='utf-8'))
        for st in range(4):
            require_stage(state, st)
        update_state('RUNNING', details)

        latest = {i: json.loads((WORK / f'qa/stage{i}/latest.json').read_text(encoding='utf-8')) for i in [1, 2, 3]}
        source = WORK / 'data/o2o.duckdb'
        a.check('stage1_db_exists', source.exists(), True)
        sourcehash = digest(source)
        s2 = Path(latest[2]['results_dir'])
        s3 = Path(latest[3]['results_dir'])
        m2 = json.loads((s2 / 'manifest.json').read_text(encoding='utf-8'))
        m3 = json.loads((s3 / 'manifest.json').read_text(encoding='utf-8'))
        a.check('source_hash_matches_stage2', m2['stage1_database_sha256'], sourcehash)
        a.check('source_hash_matches_stage3', m3['source_database_sha256'], sourcehash)
        a.check('source_run_matches_stage2', m2['stage1_run_id'], latest[1]['run_id'])
        a.check('source_run_matches_stage3', m3['source_stage1_run'], latest[1]['run_id'])

        lock = json.loads((WORK / 'config/input_lock.json').read_text(encoding='utf-8'))
        offline_spec = next(x for x in lock['files'] if x['profile_key'] == 'offline')
        source_csv = ROOT / offline_spec['path']
        a.check('input_sha256_before', digest(source_csv), offline_spec['sha256'])

        for path, m in [(s2, m2), (s3, m3)]:
            for n, sp in m['files'].items():
                a.check(f'upstream_csv_sha256.{path.parent.parent.name}.{n}', digest(path / (n + '.csv')), sp['sha256'])

        from stage3 import NAMES, PRIORITIES
        scenarios = (
            [('全部', 'TRUE')]
            + [(f'{m}月', f"receipt_month='2016-{m:02}'") for m in range(1, 7)]
            + [
                ('直接折扣', "coupon_type='direct_discount'"),
                ('满减', "coupon_type='threshold_reduction'"),
                ('未知距离', "distance_group='unknown'"),
                ('商家3381', "merchant_id='3381'"),
                ('5月·直接折扣', "receipt_month='2016-05' AND coupon_type='direct_discount'"),
                ('空结果', "merchant_id='__NO_MATCH__'"),
            ]
        )
        may_scenarios = (
            [('5月全部', 'TRUE')]
            + [(NAMES[seg_id], f"segment_id='{seg_id}'") for seg_id in PRIORITIES]
            + [
                ('商家3381', "merchant_id='3381'"),
                ('未知距离', "distance_group='unknown'"),
                ('空结果', "merchant_id='__NO_MATCH__'"),
            ]
        )

        a.log('构建 Stage 4 DuckDB 审计库与单表受控看板数据。')
        db = out / 'tableau_audit.duckdb'
        with duckdb.connect(str(db)) as c:
            c.execute("ATTACH '" + str(source).replace("'", "''") + "' AS src (READ_ONLY)")
            c.execute("ATTACH '" + str(s2 / 'stage2_metrics.duckdb').replace("'", "''") + "' AS s2 (READ_ONLY)")
            c.execute("ATTACH '" + str(s3 / 'stage3_users.duckdb').replace("'", "''") + "' AS users (READ_ONLY)")

            from stage2 import validated_bands
            bands = validated_bands(json.loads((WORK / 'config/stage2.json').read_text(encoding='utf-8'))['nominal_discount_bands'])
            case_band = 'CASE ' + ' '.join(f"WHEN round(nominal_discount_fraction,10)<={hi!r} THEN '{name}'" for name, (_, hi) in bands) + ' END'
            c.execute(f'CREATE VIEW receipts AS SELECT *, {case_band} AS discount_band FROM src.fact_coupon_receipts WHERE mature15')

            names_case = 'CASE u.segment_id ' + ' '.join(f"WHEN '{k}' THEN '{v}'" for k, v in NAMES.items()) + ' END'
            c.execute(
                f"CREATE VIEW may AS SELECT r.*, u.segment_id, {names_case} AS segment_name "
                f"FROM receipts r JOIN users.user_segments u USING(user_id) WHERE r.receipt_month='2016-05'"
            )
            a.check('may_receipt_rows', c.execute('SELECT count(*) FROM may').fetchone()[0], 215348)
            a.check('user_segments_rows', c.execute('SELECT count(*) FROM users.user_segments').fetchone()[0], 501041)
            a.check('user_segments_unique', c.execute('SELECT count(*)=count(DISTINCT user_id) FROM users.user_segments').fetchone()[0], True)

            c.execute(
                """
                CREATE TABLE chart_data(
                    page_scope VARCHAR,
                    scenario VARCHAR,
                    view_id VARCHAR,
                    category VARCHAR,
                    category_order INT,
                    receipt_records BIGINT,
                    redeemed15_records BIGINT,
                    receipt_users BIGINT,
                    redeemed15_users BIGINT,
                    cumulative_redeemed BIGINT,
                    successful_receipts BIGINT,
                    sample_note VARCHAR
                )
                """
            )
            expected = []
            sql_log = []
            agg = (
                'count(*)::BIGINT receipt_records, '
                'coalesce(sum(redeemed15_mature::BIGINT),0)::BIGINT redeemed15_records, '
                'count(DISTINCT user_id)::BIGINT receipt_users, '
                'count(DISTINCT user_id) FILTER(WHERE redeemed15_mature)::BIGINT redeemed15_users'
            )
            band_label_case = (
                'CASE discount_band '
                + ' '.join(f"WHEN '{code}' THEN '{BAND_LABELS.get(code, code)}'" for code, _ in bands)
                + ' ELSE discount_band END'
            )
            band_order_case = (
                'CASE discount_band '
                + ' '.join(f"WHEN '{name}' THEN {idx}" for idx, (name, _) in enumerate(bands))
                + ' ELSE 99 END'
            )
            seg_order_case = (
                'CASE segment_id '
                + ' '.join(f"WHEN '{seg_id}' THEN {idx}" for idx, seg_id in enumerate(PRIORITIES))
                + ' ELSE 99 END'
            )
            overview_dims = [
                (
                    'month',
                    "receipt_month || CASE WHEN receipt_month='2016-06' THEN '（1—15日）' ELSE '' END",
                    'receipt_month',
                ),
                (
                    'coupon_type',
                    "CASE WHEN coupon_type='direct_discount' THEN '直接折扣' ELSE '满减' END",
                    "CASE WHEN coupon_type='direct_discount' THEN 0 ELSE 1 END",
                ),
                (
                    'threshold',
                    "CASE WHEN threshold IS NULL THEN '直接折扣（无门槛字段）' ELSE '满' || cast(threshold::INT AS VARCHAR) END",
                    'coalesce(threshold,0)',
                ),
                ('discount_band', band_label_case, band_order_case),
                (
                    'distance',
                    "CASE WHEN distance_group='unknown' THEN '未知距离' ELSE distance_group END",
                    "CASE WHEN distance_group='unknown' THEN 11 ELSE try_cast(distance_group AS INT) END",
                ),
                ('merchant', 'merchant_id', 'count(*) DESC, merchant_id ASC'),
            ]
            may_dims = [
                ('segment', 'segment_name', seg_order_case),
                ('merchant', 'merchant_id', 'count(*) DESC, merchant_id ASC'),
            ]
            label_to_band_code = {v: k for k, v in BAND_LABELS.items()}

            c.execute('BEGIN TRANSACTION')
            for scope, table, choices in [('overview', 'receipts', scenarios), ('may', 'may', may_scenarios)]:
                for scenario, predicate in choices:
                    filtered = f'(SELECT * FROM {table} WHERE {predicate})'
                    sql = f'SELECT {agg} FROM {filtered}'
                    kpi = rows(c, sql)[0]
                    rate15 = kpi['redeemed15_records'] / kpi['receipt_records'] if kpi['receipt_records'] else None
                    user_share15 = kpi['redeemed15_users'] / kpi['receipt_users'] if kpi['receipt_users'] else None
                    expected.append(
                        {
                            'page_scope': scope,
                            'scenario': scenario,
                            'predicate': predicate,
                            **kpi,
                            'rate15': rate15,
                            'user_share15': user_share15,
                        }
                    )
                    kpi_note = '空结果：分母为0，率不可计算' if not kpi['receipt_records'] else '15日观察期完整；重复保留；独立用户按当前范围精确去重'
                    c.execute(
                        'INSERT INTO chart_data VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                        [
                            scope,
                            scenario,
                            'kpi',
                            '总体',
                            0,
                            kpi['receipt_records'],
                            kpi['redeemed15_records'],
                            kpi['receipt_users'],
                            kpi['redeemed15_users'],
                            0,
                            kpi['redeemed15_records'],
                            kpi_note,
                        ],
                    )
                    for view, expr, order in (overview_dims if scope == 'overview' else may_dims):
                        if view == 'merchant':
                            sql = f'SELECT {expr} category, {agg} FROM {filtered} GROUP BY {expr} ORDER BY {order}'
                        else:
                            sql = f'SELECT {expr} category, {agg} FROM {filtered} GROUP BY {expr}, {order} ORDER BY {order}'
                        values = rows(c, sql)
                        sql_log.append({'scope': scope, 'scenario': scenario, 'view': view, 'sql': sql})
                        a.check(
                            f'partition_sum_receipts.{scope}.{scenario}.{view}',
                            sum(v['receipt_records'] for v in values),
                            kpi['receipt_records'],
                        )
                        a.check(
                            f'partition_sum_redeemed.{scope}.{scenario}.{view}',
                            sum(v['redeemed15_records'] for v in values),
                            kpi['redeemed15_records'],
                        )
                        for i, v in enumerate(values):
                            notes = []
                            if view == 'month' and '2016-06' in v['category']:
                                notes.append('部分月份（仅1—15日领券，勿直接与完整月比总量）')
                            if view == 'discount_band':
                                bcode = label_to_band_code.get(v['category'], v['category'])
                                notes.append(f'名义优惠比例分档（配置编码: {bcode}）')
                            if view == 'distance' and v['category'] == '未知距离':
                                notes.append('未知距离（原编码unknown保留，不补0；距离编码非精确公里数）')
                            if view == 'segment' and v['category'] == '未知历史':
                                notes.append('未知历史≠平台新用户（回溯评价补入）')
                            if v['receipt_records'] < 100:
                                notes.append('领券<100；小样本（不作稳定最优）')
                            c.execute(
                                'INSERT INTO chart_data VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                                [
                                    scope,
                                    scenario,
                                    view,
                                    v['category'],
                                    i,
                                    v['receipt_records'],
                                    v['redeemed15_records'],
                                    v['receipt_users'],
                                    v['redeemed15_users'],
                                    0,
                                    kpi['redeemed15_records'],
                                    '；'.join(notes),
                                ],
                            )

                    if scope == 'overview':
                        # Scheme view: Top 12 mainstream schemes + merged remaining >=100 + merged low-sample <100
                        scheme_sql = f"""
                        WITH sc_counts AS (
                            SELECT discount_rate, count(*) AS cnt,
                                   row_number() OVER (ORDER BY count(*) DESC, discount_rate ASC) AS rn
                            FROM {filtered}
                            GROUP BY discount_rate
                        ),
                        tagged AS (
                            SELECT f.*,
                                   CASE
                                       WHEN s.rn <= 12 THEN f.discount_rate
                                       WHEN s.cnt >= 100 THEN '其余达标方案（≥100条合并）'
                                       ELSE '低样本方案汇总（<100条合并）'
                                   END AS category,
                                   CASE
                                       WHEN s.rn <= 12 THEN s.rn - 1
                                       WHEN s.cnt >= 100 THEN 12
                                       ELSE 13
                                   END AS ord,
                                   s.cnt AS scheme_cnt,
                                   s.rn AS scheme_rn
                            FROM {filtered} f
                            JOIN sc_counts s USING (discount_rate)
                        )
                        SELECT category,
                               ord::INT AS category_order,
                               {agg},
                               count(DISTINCT discount_rate)::INT AS merged_schemes,
                               min(scheme_cnt)::BIGINT AS min_scheme_receipts
                        FROM tagged
                        GROUP BY category, ord
                        ORDER BY ord
                        """
                        s_vals = rows(c, scheme_sql)
                        sql_log.append({'scope': scope, 'scenario': scenario, 'view': 'scheme', 'sql': scheme_sql})
                        a.check(
                            f'partition_sum_receipts.{scope}.{scenario}.scheme',
                            sum(v['receipt_records'] for v in s_vals),
                            kpi['receipt_records'],
                        )
                        a.check(
                            f'partition_sum_redeemed.{scope}.{scenario}.scheme',
                            sum(v['redeemed15_records'] for v in s_vals),
                            kpi['redeemed15_records'],
                        )
                        for idx_s, sv in enumerate(s_vals):
                            if sv['category'] == '其余达标方案（≥100条合并）':
                                snote = f"合并其余 {sv['merged_schemes']} 种单方案≥100条的达标券方案；完整45种明细见 fixed_coupon_scheme_full.csv"
                            elif sv['category'] == '低样本方案汇总（<100条合并）':
                                snote = f"合并 {sv['merged_schemes']} 种单方案<100条的低样本方案（如150:5仅7条/28.57%）；领券<100；小样本（不作稳定最优）"
                            else:
                                snote = '领券<100；小样本（不作稳定最优）' if sv['receipt_records'] < 100 else '主流高覆盖券方案（按领券量降序）'
                            c.execute(
                                'INSERT INTO chart_data VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                                [
                                    scope,
                                    scenario,
                                    'scheme',
                                    sv['category'],
                                    idx_s,
                                    sv['receipt_records'],
                                    sv['redeemed15_records'],
                                    sv['receipt_users'],
                                    sv['redeemed15_users'],
                                    0,
                                    kpi['redeemed15_records'],
                                    snote,
                                ],
                            )

                        lag = rows(c, f'SELECT lag_days, count(*)::BIGINT n FROM {filtered} WHERE redeemed15_mature GROUP BY lag_days')
                        byday = {v['lag_days']: v['n'] for v in lag}
                        cum = 0
                        for d in range(16):
                            n = byday.get(d, 0)
                            cum += n
                            c.execute(
                                'INSERT INTO chart_data VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                                [
                                    scope,
                                    scenario,
                                    'lag',
                                    str(d),
                                    d,
                                    kpi['receipt_records'],
                                    n,
                                    kpi['receipt_users'],
                                    kpi['redeemed15_users'],
                                    cum,
                                    kpi['redeemed15_records'],
                                    '等待分布分母=15日成功核销总数；累计核销率分母=成熟领券总数；每日分母重复勿跨日求和',
                                ],
                            )
                        a.check(f'lag_day15_matches_kpi.{scenario}', cum, kpi['redeemed15_records'])

                    # Candidate merchants: Top 20 by receipt_records DESC PLUS benchmark merchant '3381' if present in filtered set
                    cand_sql = f"""
                    WITH ranked AS (
                        SELECT merchant_id AS category,
                               {agg},
                               mode(CASE WHEN coupon_type='direct_discount' THEN '直接折扣' ELSE '满减' END) AS top_type,
                               avg((coupon_type='threshold_reduction')::INT) AS threshold_share,
                               mode(CASE WHEN threshold IS NULL THEN '直接折扣' ELSE '满' || cast(threshold::INT AS VARCHAR) END) AS top_thresh,
                               avg((distance_group='unknown')::INT) AS unk_dist_share
                               {", avg((segment_id='unknown_history')::INT) AS unk_seg_share" if scope == 'may' else ''},
                               row_number() OVER (ORDER BY count(*) DESC, merchant_id ASC) AS rk
                        FROM {filtered}
                        GROUP BY merchant_id
                    )
                    SELECT * FROM ranked WHERE rk <= 20 OR category = '3381' ORDER BY rk
                    """
                    top_m = rows(c, cand_sql)
                    for i, mv in enumerate(top_m):
                        t_share = mv['threshold_share'] if mv['top_type'] == '满减' else (1.0 - mv['threshold_share'])
                        prefix = f"全期最大基准商家(当前排第{mv['rk']}名)｜" if mv['rk'] > 20 and mv['category'] == '3381' else ''
                        diag = (
                            f"{prefix}主券型:{mv['top_type']}({t_share * 100:.0f}%)"
                            f"｜主门槛:{mv['top_thresh']}｜unknown距离:{mv['unk_dist_share'] * 100:.1f}%"
                        )
                        if scope == 'may':
                            diag += f"｜未知历史记录:{mv['unk_seg_share'] * 100:.1f}%"
                        if mv['receipt_records'] < 100:
                            diag += '｜领券<100；小样本'
                        c.execute(
                            'INSERT INTO chart_data VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                            [
                                scope,
                                scenario,
                                'candidate',
                                mv['category'],
                                i,
                                mv['receipt_records'],
                                mv['redeemed15_records'],
                                mv['receipt_users'],
                                mv['redeemed15_users'],
                                0,
                                kpi['redeemed15_records'],
                                diag,
                            ],
                        )
            c.execute('COMMIT')

            # Verify candidate view in '5月全部' has 21 rows (Top 20 + '3381')
            cand_may_all = rows(
                c,
                "SELECT * FROM chart_data WHERE page_scope='may' AND scenario='5月全部' AND view_id='candidate' ORDER BY category_order",
            )
            a.check('may_candidate_count_includes_3381', len(cand_may_all), 21)
            a.check('may_candidate_has_3381', any(r['category'] == '3381' for r in cand_may_all), True)

            # Create standalone tables in tableau_audit.duckdb
            c.execute(
                """
                CREATE TABLE filter_expected(
                    page_scope VARCHAR,
                    scenario VARCHAR,
                    predicate VARCHAR,
                    receipt_records BIGINT,
                    redeemed15_records BIGINT,
                    receipt_users BIGINT,
                    redeemed15_users BIGINT,
                    rate15 DOUBLE,
                    user_share15 DOUBLE
                )
                """
            )
            c.executemany(
                'INSERT INTO filter_expected VALUES (?,?,?,?,?,?,?,?,?)',
                [
                    (
                        r['page_scope'],
                        r['scenario'],
                        r['predicate'],
                        r['receipt_records'],
                        r['redeemed15_records'],
                        r['receipt_users'],
                        r['redeemed15_users'],
                        r['rate15'],
                        r['user_share15'],
                    )
                    for r in expected
                ],
            )

            c.execute(
                """
                CREATE TABLE candidate_merchant_structure AS
                WITH top_may_merchants AS (
                    SELECT category AS merchant_id, category_order AS may_rank
                    FROM chart_data
                    WHERE page_scope = 'may' AND scenario = '5月全部' AND view_id = 'candidate'
                )
                SELECT m.may_rank,
                       r.merchant_id,
                       CASE WHEN r.coupon_type = 'direct_discount' THEN '直接折扣' ELSE '满减' END AS coupon_type_label,
                       CASE WHEN r.threshold IS NULL THEN '直接折扣（无门槛字段）' ELSE '满' || cast(r.threshold::INT AS VARCHAR) END AS threshold_label,
                       CASE WHEN r.distance_group = 'unknown' THEN '未知距离' ELSE r.distance_group END AS distance_group,
                       r.segment_id,
                       r.segment_name,
                       count(*)::BIGINT AS receipt_records,
                       coalesce(sum(r.redeemed15_mature::BIGINT), 0)::BIGINT AS redeemed15_records,
                       count(DISTINCT r.user_id)::BIGINT AS receipt_users,
                       count(DISTINCT r.user_id) FILTER (WHERE r.redeemed15_mature)::BIGINT AS redeemed15_users,
                       sum(r.redeemed15_mature::BIGINT)::DOUBLE / nullif(count(*), 0) AS redemption_rate15,
                       count(*) < 100 AS small_sample
                FROM may r
                JOIN top_may_merchants m USING (merchant_id)
                GROUP BY ALL
                ORDER BY m.may_rank, r.merchant_id, receipt_records DESC, coupon_type_label, threshold_label, distance_group, r.segment_id
                """
            )
            c.execute('CREATE TABLE fixed_receipt_cohort_days1_15 AS SELECT * FROM s2.receipt_cohort_days1_15 ORDER BY receipt_month')
            c.execute('CREATE TABLE fixed_coupon_scheme_full AS SELECT * FROM s2.coupon_scheme_metrics ORDER BY receipt_records DESC, discount_rate ASC')
            c.execute('CREATE TABLE fixed_segment_evaluation AS SELECT * FROM users.segment_evaluation ORDER BY segment_id')
            c.execute('CREATE TABLE fixed_may_bootstrap AS SELECT * FROM users.segment_bootstrap_intervals ORDER BY segment_id, metric')

            doc_views = {
                'month': rows(c, "SELECT * FROM chart_data WHERE page_scope='overview' AND scenario='全部' AND view_id='month' ORDER BY category_order"),
                'lag': rows(c, "SELECT * FROM chart_data WHERE page_scope='overview' AND scenario='全部' AND view_id='lag' ORDER BY category_order"),
                'discount_band': rows(c, "SELECT * FROM chart_data WHERE page_scope='overview' AND scenario='全部' AND view_id='discount_band' ORDER BY category_order"),
                'scheme': rows(c, "SELECT * FROM chart_data WHERE page_scope='overview' AND scenario='全部' AND view_id='scheme' ORDER BY category_order"),
                'segment': rows(c, "SELECT * FROM chart_data WHERE page_scope='may' AND scenario='5月全部' AND view_id='segment' ORDER BY category_order"),
                'candidate': cand_may_all,
            }

            # Export controlled CSVs to out/data
            for scope in ['overview', 'may']:
                export(
                    c,
                    f"SELECT * EXCLUDE(page_scope) FROM chart_data WHERE page_scope='{scope}' ORDER BY scenario, view_id, category_order",
                    out / f'data/{scope}_charts.csv',
                )
            export(c, 'SELECT * FROM fixed_receipt_cohort_days1_15 ORDER BY receipt_month', out / 'data/fixed_receipt_cohort_days1_15.csv')
            export(c, 'SELECT * FROM fixed_coupon_scheme_full ORDER BY receipt_records DESC, discount_rate ASC', out / 'data/fixed_coupon_scheme_full.csv')
            export(c, 'SELECT * FROM fixed_segment_evaluation ORDER BY segment_id', out / 'data/fixed_segment_evaluation.csv')
            export(c, 'SELECT * FROM fixed_may_bootstrap ORDER BY segment_id, metric', out / 'data/fixed_may_bootstrap.csv')
            export(
                c,
                'SELECT * FROM candidate_merchant_structure ORDER BY may_rank, merchant_id, receipt_records DESC, coupon_type_label, threshold_label, distance_group, segment_id',
                out / 'data/fixed_candidate_merchant_structure.csv',
            )
            export(c, 'SELECT * FROM chart_data ORDER BY page_scope, scenario, view_id, category_order', qa / 'chart_expected.csv')

            # Drop external views so tableau_audit.duckdb is 100% standalone
            c.execute('DROP VIEW may')
            c.execute('DROP VIEW receipts')
            c.execute('CHECKPOINT')

        a.log('从原 CSV 与阶段3用户分层表独立复算全部 23 个代表性筛选场景与第0—15日逐日核销指标。')
        py_expected, py_lag_daily = independent_verify_scenarios(source_csv, s3 / 'user_segments.csv', scenarios, may_scenarios, NAMES)
        for r in expected:
            key = (r['page_scope'], r['scenario'])
            pe = py_expected[key]
            for metric in ['receipt_records', 'redeemed15_records', 'receipt_users', 'redeemed15_users', 'rate15', 'user_share15']:
                a.check(f'independent_csv.{r["page_scope"]}.{r["scenario"]}.{metric}', r[metric], pe[metric])
        for lr in doc_views['lag']:
            d_idx = int(lr['category'])
            a.check(f'independent_csv.lag_day_{d_idx}.redeemed15', lr['redeemed15_records'], py_lag_daily[d_idx])
        a.check('lag_day1_exact_8355', doc_views['lag'][1]['redeemed15_records'], 8355)
        a.check('lag_day1_cum_exact_14202', doc_views['lag'][1]['cumulative_redeemed'], 14202)

        a.log('运行 Stage 4 合成边界与 XSD Schema 校验单元测试。')
        from test_stage4 import run_tests
        test_res = run_tests()
        dump(qa / 'semantic_tests.json', test_res)
        a.check('semantic_edge_tests', test_res['passed'], True)

        a.log('调用 Tableau Public 内置 hyperd 引擎编译 .hyper 数据提取并回读对账。')
        for scope in ['overview', 'may']:
            csv_p = out / f'data/{scope}_charts.csv'
            hyp_p = out / f'data/{scope}_charts.hyper'
            h_stats = build_hyper_extract(csv_p, hyp_p)
            with duckdb.connect(':memory:') as mc:
                c_rows, c_rec, c_red = mc.execute(
                    "SELECT count(*), coalesce(sum(receipt_records) FILTER (WHERE view_id='kpi'), 0), coalesce(sum(redeemed15_records) FILTER (WHERE view_id='kpi'), 0) FROM read_csv(?, header=true)",
                    [str(csv_p)],
                ).fetchone()
            a.check(f'hyper_rows.{scope}', h_stats['rows'], c_rows)
            a.check(f'hyper_kpi_receipt_sum.{scope}', h_stats['kpi_receipt_sum'], c_rec)
            a.check(f'hyper_kpi_redeemed_sum.{scope}', h_stats['kpi_redeemed_sum'], c_red)

        # Sync controlled data + .hyper extracts to fixed top-level tableau/data/
        top_data = WORK / 'tableau/data'
        top_data.mkdir(parents=True, exist_ok=True)
        for p in sorted((out / 'data').iterdir()):
            shutil.copy2(p, top_data / p.name)

        a.check('input_sha256_after', digest(source_csv), offline_spec['sha256'])
        a.check('stage1_db_unchanged', digest(source), sourcehash)

        dump(qa / 'filter_expected.json', expected)
        dump(qa / 'executed_sql.json', sql_log)
        with (qa / 'filter_expected.csv').open('w', newline='', encoding='utf-8') as f:
            wr = csv.DictWriter(f, fieldnames=list(expected[0]))
            wr.writeheader()
            wr.writerows(expected)
        shutil.copy2(qa / 'filter_expected.csv', WORK / 'tableau/filter_expected.csv')

        files = {p.name: {'bytes': p.stat().st_size, 'sha256': digest(p)} for p in sorted((out / 'data').iterdir())}
        manifest = {
            'run_id': runid,
            'source_stage1_run': latest[1]['run_id'],
            'source_database_sha256': sourcehash,
            'stage2_run': latest[2]['run_id'],
            'stage3_run': latest[3]['run_id'],
            'files': files,
            'scenario_contract': {'overview': scenarios, 'may': may_scenarios},
            'grain': 'scenario + view_id + category; each worksheet MUST fix view_id and exactly one scenario; no joins',
            'distinct_users': 'precomputed from source for exact legal scenario; never sum across categories/months',
            'rates': 'SUM(redeemed15_records)/SUM(receipt_records); zero denominator NULL; formatted as p0.00%',
            'duplicate_policy': 'retained',
            'may_window': 'asof May1; May receipts; outcomes observed through June15; no month control on page3',
            'publication': 'local only; no publication authorized',
            'script_hashes': {
                name: digest(WORK / 'scripts' / name)
                for name in ['stage4_dependencies.py', 'stage4.py', 'stage4_workbook.py', 'stage4_tableau_runtime.py', 'stage4_docs.py', 'verification.py']
            },
            'semantic_test_sha256': digest(WORK / 'tests/test_stage4.py'),
        }
        dump(out / 'manifest.json', manifest)

        with duckdb.connect(str(db)) as c:
            c.execute('CREATE TABLE tableau_provenance(run_id VARCHAR, provenance_json VARCHAR)')
            c.execute('INSERT INTO tableau_provenance VALUES (?,?)', [runid, json.dumps(manifest, ensure_ascii=False)])
            c.execute('CHECKPOINT')

        a.log('生成含参考线、对数轴与 Dashboard Actions 的 .twb 与 .twbx 原生工作簿，并执行 Tableau 2026.2 官方 XSD Schema 校验。')
        from stage4_workbook import build_workbooks
        wb_info = build_workbooks(out, qa)
        a.check('workbook_xsd_validation', wb_info['xsd_validation'], 'PASS')
        a.check('worksheets_count', wb_info['worksheets_count'], 24)
        a.check('dashboards_count', wb_info['dashboards_count'], 3)
        a.check('actions_count', wb_info['actions_count'], 6)

        a.log('prepare-only：不启动或关闭 Tableau；原生加载、交互与保存重开均需独立证据。')
        rt_report = {'status': 'NOT_RUN', 'native_open': 'NOT_RUN',
                     'three_dashboards': 'NOT_RUN', 'representative_filters': 'NOT_RUN',
                     'clear_reset': 'NOT_RUN', 'save_close_reopen': 'NOT_RUN',
                     'visual_inspection': 'NOT_RUN'}
        dump(qa / 'tableau_runtime_verification.json', rt_report)

        # Standalone read-only check & multiset replay verification against previous run
        with duckdb.connect(str(db), read_only=True) as c:
            standalone_counts = {t: c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in STANDALONE_TABLES}
        previous_pointer = WORK / 'tableau/latest.json'
        prev_dir = None
        if previous_pointer.exists():
            prev_cand = Path(json.loads(previous_pointer.read_text(encoding='utf-8')).get('results_dir', ''))
            if prev_cand.is_dir() and (prev_cand / 'tableau_audit.duckdb').is_file() and prev_cand != out:
                try:
                    with duckdb.connect(str(prev_cand / 'tableau_audit.duckdb'), read_only=True) as pc:
                        existing_tables = {r[0] for r in pc.execute('SHOW TABLES').fetchall()}
                    if set(STANDALONE_TABLES).issubset(existing_tables):
                        prev_dir = prev_cand
                except Exception:
                    prev_dir = None

        replay_info = {
            'status': 'NOT_COMPARED',
            'current_run': out.name,
            'previous_run': None,
            'files_verified': len(files),
            'standalone_read_only_counts': standalone_counts,
            'workbook_xsd_validation': wb_info['xsd_validation'],
            'tableau_runtime_verification': rt_report['status'],
            'comparison_method': 'multiset including duplicates; bounded float tolerance; no row-order assumption',
        }
        if prev_dir is not None:
            try:
                replay_info['tables'] = compare_database_tables(db, prev_dir / 'tableau_audit.duckdb', STANDALONE_TABLES)
                replay_info['previous_run'] = prev_dir.name
                replay_info['status'] = 'PASS'
            except Exception:
                replay_info['status'] = 'SCHEMA_OR_CONTENT_UPDATED'
        dump(qa / 'replay_verification.json', replay_info)
        dump(WORK / 'qa/stage4/复现检查.json', replay_info)

        specs_dict = json.loads((qa / 'worksheet_specs.json').read_text(encoding='utf-8'))
        details.update(
            stage4='PARTIAL',
            completed_at=now(),
            elapsed_seconds=time.monotonic() - started,
            checks_passed=len(a.checks),
            source_stage1_run=latest[1]['run_id'],
            source_database_sha256=sourcehash,
            stage2_run=latest[2]['run_id'],
            stage3_run=latest[3]['run_id'],
            data_preparation='PASS',
            hyper_extract_generation='PASS',
            workbook_xsd_validation='PASS',
            tableau_native_open='NOT_RUN',
            tableau_filter_reconciliation='NOT_RUN',
            visual_inspection='NOT_RUN',
            publicly_published=False,
            workbooks=wb_info,
            tableau_runtime=rt_report,
        )
        from stage4_docs import write_stage4_docs
        write_stage4_docs(out, qa, details, expected, specs_dict, doc_views)
        doc_text = (WORK / 'tableau/逐图配置单与对账说明.md').read_text(encoding='utf-8')
        a.check('doc_lag_day1_is_8355', '8,355' in doc_text and '14,202' in doc_text and '8,389' not in doc_text, True)
        a.check('doc_includes_3381_candidate', '| 21 | `3381` | 53 | 5 |' in doc_text, True)
        details['checks_passed'] = len(a.checks)

        dump(qa / 'checks.json', a.checks)
        dump(qa / 'status.json', details)
        dump(WORK / 'qa/stage4/latest.json', details)
        dump(WORK / 'tableau/latest.json', details)
        update_state('PARTIAL', details)
        a.log(f'阶段4 数据、Hyper 提取与原生工作簿准备完成：{len(a.checks)} 项检查通过。')
        print(json.dumps(details, ensure_ascii=False, indent=2))
        return 0
    except Exception as e:
        details.update(stage4='FAIL', error=str(e), completed_at=now())
        dump(qa / 'checks.json', a.checks)
        dump(qa / 'status.json', details)
        update_state('FAIL', details)
        a.log('阶段4 FAIL：' + str(e))
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.path.insert(0, str(WORK / 'tests'))
    sys.exit(main())
