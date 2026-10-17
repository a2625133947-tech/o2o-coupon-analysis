"""Execute real Tableau Public 2026.2.3 runtime verification across all 3 dashboards and representative filters."""
from pathlib import Path
import base64
import copy
import json
import subprocess
import tempfile
import time
import xml.etree.ElementTree as E

WORK = Path(__file__).resolve().parents[1]
TABLEAU_BIN = Path('/Applications/Tableau Public (Apple silicon).app/Contents/MacOS/Tableau Public')
from stage4_dependencies import resolve_tool
LOG_DIR = Path.home() / 'Documents' / '我的 Tableau 存储库' / '日志'


def parse_recent_tableau_logs(since_ts):
    """Parse log.txt and log_bk.txt for entries with ts >= since_ts."""
    queries = []
    partitions = []
    rendered = []
    errors = []
    for fn in ['log_bk.txt', 'log.txt', 'log_1.txt']:
        p = LOG_DIR / fn
        if not p.exists():
            continue
        for line in p.read_text(encoding='utf-8', errors='replace').splitlines():
            if '"ts":"' not in line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            ts = obj.get('ts', '')
            if ts < since_ts:
                continue
            k = obj.get('k', '')
            v = obj.get('v', {})
            a = obj.get('a', {})
            if k == 'begin-protocol.query' and isinstance(v, dict):
                q = v.get('query', '')
                if '"Extract"."Extract"' in q:
                    queries.append({'ts': ts, 'query': q})
            elif k == 'begin-compute-model-task.execute' and isinstance(v, dict):
                partitions.append({'ts': ts, 'event': 'execute', 'ws': v.get('ws')})
            elif k == 'end-partition-interpreter.instance' and isinstance(v, dict):
                partitions.append(
                    {
                        'ts': ts,
                        'event': 'partition',
                        'rk': a.get('rk'),
                        'tuples': v.get('tuples'),
                        'x_cells': v.get('x-cells'),
                        'y_cells': v.get('y-cells'),
                    }
                )
            elif k == 'msg' and isinstance(v, str) and 'Finished rendering sheet:' in v:
                rendered.append({'ts': ts, 'sheet': v.split('Finished rendering sheet:', 1)[1].strip()})
            elif k in ('detailed-error-msg', 'show-detailed-error-dialog'):
                errors.append({'ts': ts, 'k': k, 'v': v})
    return {'queries': queries, 'partitions': partitions, 'rendered': rendered, 'errors': errors}


def query_hyper_kpi(hyper_path, scenario):
    """Query the .hyper extract directly via hyperd using Tableau's exact WHERE clause."""
    with tempfile.TemporaryDirectory() as td:
        sql = (
            'SELECT SUM("Extract"."receipt_records"), '
            'SUM("Extract"."redeemed15_records"), '
            'SUM("Extract"."receipt_users"), '
            'SUM("Extract"."redeemed15_users") '
            'FROM "Extract"."Extract" "Extract" '
            f'WHERE (("Extract"."scenario" = \'{scenario}\' COLLATE "zh_Hans_CN") AND ("Extract"."view_id" = \'kpi\'));\n'
        )
        res = subprocess.run(
            [
                str(resolve_tool('hyperd')),
                f'--log-dir={td}',
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
        lines = [
            ln.strip()
            for ln in combined.splitlines()
            if ln.strip() and ln.strip()[0].isdigit() and 'rows' not in ln and 'row' not in ln
        ]
        vals = [int(x) for x in lines[-1].split()]
        n, y, un, uy = vals
        return {
            'receipt_records': n,
            'redeemed15_records': y,
            'receipt_users': un,
            'redeemed15_users': uy,
            'rate15': (y / n) if n else None,
            'user_share15': (uy / un) if un else None,
        }


def run_tableau_session(*args, **kwargs):
    raise RuntimeError('旧桌面自动化已停用；请通过原生UI独立验证并使用 stage4.py --accept-native-evidence 登记证据。')


def set_workbook_parameters(src_twb, dst_twb, overview_val, may_val):
    """Resolve displayed aliases through member values and preserve Tableau datatype."""
    tree = E.parse(src_twb)
    params = tree.getroot().find("./datasources/datasource[@name='Parameters']")
    if params is None:
        raise ValueError('工作簿缺少 Parameters 数据源')
    for name, requested in (('[OverviewScope]', overview_val), ('[MayScope]', may_val)):
        col = params.find(f"column[@name='{name}']")
        if col is None:
            raise ValueError(f'缺少参数 {name}')
        members = col.findall('members/member')
        requested = str(requested)
        matches = {m.get('value') for m in members
                   if requested in (m.get('value'), m.get('alias'))}
        if len(matches) != 1:
            raise ValueError(f'{name} 不支持参数值 {requested!r}')
        value = matches.pop()
        datatype = col.get('datatype')
        if datatype == 'integer':
            try:
                value = str(int(value))
            except (TypeError, ValueError):
                raise ValueError(f'{name} integer 成员非法: {value!r}')
        elif datatype != 'string':
            raise ValueError(f'{name} 暂不支持 datatype={datatype!r}')
        calc = col.find('calculation')
        if calc is None:
            raise ValueError(f'{name} 缺少 calculation')
        col.set('value', value)
        calc.set('formula', value)
    tree.write(dst_twb, encoding='utf-8', xml_declaration=True)


def verify_and_embed_tableau_runtime(*args, **kwargs):
    raise RuntimeError('旧桌面自动化已停用；默认prepare-only，原生验收使用独立证据登记。')
