"""Stage 0 only: lock verification, bounded real import, independent SQL reconciliation."""
from pathlib import Path
from datetime import date, datetime
from zoneinfo import ZoneInfo
import csv
import hashlib
import itertools
import json
import platform
import sys
import traceback
from verification import invalidate_downstream
import duckdb

WORK = Path(__file__).resolve().parents[1]
ROOT = WORK.parent
QA = WORK / 'qa'
CHECK_KEYS = (
    'four_input_hashes_before_after',
    'existing_profile_and_notebook_reuse',
    'isolated_environment',
    'real_bounded_duckdb_import',
    'date_and_id_types',
    'sql_python_reconciliation',
    'dictionaries_and_time_config',
)

def digest(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

def write_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

def require(condition, message):
    if not condition:
        raise RuntimeError(message)

def main(checks=None):
    if checks is None:
        checks = {k: 'FAIL' for k in CHECK_KEYS}
    require(sys.prefix != sys.base_prefix, '必须在项目隔离环境运行')
    require(duckdb.__version__ == '1.4.3', 'DuckDB版本与锁定依赖不一致')
    checks['isolated_environment'] = 'PASS'

    for doc_name in ('数据字典.md', '指标字典.md', '环境说明.md', '输入清单.md'):
        doc_path = WORK / 'docs' / doc_name
        require(doc_path.is_file() and doc_path.stat().st_size > 100, f'文档缺失或内容过短: docs/{doc_name}')

    config = json.loads((WORK/'config/analysis.json').read_text())
    history_start = date.fromisoformat(config['history_start'])
    segmentation_asof = date.fromisoformat(config['segmentation_asof'])
    history_end_exclusive = date.fromisoformat(config['history_end_exclusive'])
    evaluation_receipt_start = date.fromisoformat(config['evaluation_receipt_start'])
    evaluation_receipt_end_inclusive = date.fromisoformat(config['evaluation_receipt_end_inclusive'])
    evaluation_observation_end = date.fromisoformat(config['evaluation_observation_end'])
    training_observation_end = date.fromisoformat(config['training_observation_end'])
    require(history_start < segmentation_asof, '时间窗口合同非法: history_start >= segmentation_asof')
    require(segmentation_asof == history_end_exclusive, '时间窗口合同非法: segmentation_asof != history_end_exclusive')
    require(
        segmentation_asof <= evaluation_receipt_start <= evaluation_receipt_end_inclusive < evaluation_observation_end <= training_observation_end,
        '时间窗口合同非法: 评价与训练观察窗口顺序不一致',
    )
    require((evaluation_observation_end - evaluation_receipt_end_inclusive).days >= 15, '时间窗口合同非法: 评价领券观察期不足15天')
    require(config.get('redemption_days_inclusive') == [0, 15], '时间窗口合同非法: redemption_days_inclusive != [0, 15]')
    checks['dictionaries_and_time_config'] = 'PASS'

    lock = json.loads((WORK/'config/input_lock.json').read_text())
    profile_path = ROOT / lock['profile']
    profile = json.loads(profile_path.read_text())
    inputs = []
    rows = []
    notebook = ROOT/'outputs/dataset_review_20261014/数据核验.ipynb'
    nb = json.loads(notebook.read_text())
    require(any(c.get('outputs') for c in nb['cells'] if c['cell_type']=='code'), '已有Notebook无执行输出')
    for spec in lock['files']:
        path = ROOT/spec['path']
        sha = digest(path)
        with path.open(encoding='utf-8', newline='') as f:
            reader = csv.DictReader(f) if spec['has_header'] else csv.DictReader(f, fieldnames=spec['columns'])
            columns = reader.fieldnames
            if not spec['has_header']:
                first = next(reader)
                require(None not in first and all(v is not None for v in first.values()), '无表头模板必须为四列')
            # Only headers for non-core inputs; sample_submission is never an analytical input.
            if spec['profile_key'] == 'offline':
                rows = list(itertools.islice(reader, config['sample_rows']))
        require(path.stat().st_size == spec['bytes'], f"输入大小不一致: {spec['path']}")
        require(sha == spec['sha256'], f"输入SHA256不一致: {spec['path']}")
        require(columns == spec['columns'], f"输入字段不一致: {spec['path']}: {columns}")
        old = profile['files'][spec['path']]
        require(old['sha256'] == sha and old['bytes']==spec['bytes'], '已有profile输入版本不一致')
        if spec['profile_key']:
            require(profile[spec['profile_key']]['rows']==spec['rows'], '锁定行数与既有profile不一致')
        inputs.append({**spec, 'absolute_path':str(path), 'actual_sha256':sha, 'actual_bytes':path.stat().st_size, 'columns':columns, 'has_header':spec['has_header'], 'verified':True, 'row_count_method':'复用已执行profile；SHA256及大小匹配，不重复全量计数'})
    require(len(inputs) == 4, '锁定输入文件数量必须为4')
    checks['existing_profile_and_notebook_reuse'] = 'PASS'
    require(len(rows)==config['sample_rows'], '小样本长度不足')
    fields = profile['offline']['columns']
    normalized = [tuple(None if r[k] in ('null','') else r[k] for k in fields) for r in rows]
    expected = {'sample_records':len(rows), 'receipt_records':0,'receipt_users':0,'mature_receipts':0,'redeemed15':0}
    users = set()
    cutoff = training_observation_end
    for r in rows:
        rd = datetime.strptime(r['Date_received'], '%Y%m%d').date() if r['Date_received'] not in ('null','') else None
        cd = datetime.strptime(r['Date'], '%Y%m%d').date() if r['Date'] not in ('null','') else None
        if rd and r['Coupon_id'] not in ('null',''):
            expected['receipt_records']+=1
            if r['User_id'] not in ('null',''): users.add(r['User_id'])
            mature=(cutoff-rd).days>=15
            expected['mature_receipts']+=int(mature)
            expected['redeemed15']+=int(mature and cd is not None and 0 <= (cd-rd).days <= 15)
    expected['receipt_users']=len(users)
    expected['redemption_rate15']=expected['redeemed15']/expected['mature_receipts'] if expected['mature_receipts'] else None
    db=WORK/'data/stage0_smoke.duckdb'
    with duckdb.connect(str(db)) as con:
        con.execute('CREATE OR REPLACE TABLE smoke_raw (source_file VARCHAR, source_row_number BIGINT, '+', '.join(f'"{k}" VARCHAR' for k in fields)+')')
        source='o2o/ccf_offline_stage1_train.csv'
        con.executemany('INSERT INTO smoke_raw VALUES ('+','.join('?' for _ in range(len(fields)+2))+')',[(source,i,*values) for i,values in enumerate(normalized,1)])
        sql=(WORK/'sql/00_smoke.sql').read_text()
        con.execute('CREATE OR REPLACE TABLE smoke_config (observation_end DATE)')
        con.execute('INSERT INTO smoke_config VALUES (?)', [config['training_observation_end']])
        for statement in con.extract_statements(sql):
            con.execute(statement)
        actual=dict(zip([d[0] for d in con.description],con.fetchone()))
        require(con.execute('SELECT count(DISTINCT source_row_number) FROM smoke_raw').fetchone()[0]==len(rows), '源行号不唯一')
        checks['real_bounded_duckdb_import'] = 'PASS'
        require(con.execute('SELECT count(*) FROM smoke_typed WHERE (Date_received IS NOT NULL AND received_date IS NULL) OR (Date IS NOT NULL AND consumed_date IS NULL)').fetchone()[0]==0,'样本日期解析失败')
        schema=con.execute('DESCRIBE smoke_typed').fetchall()
        require(any(r[0]=='received_date' and r[1]=='DATE' for r in schema),'日期未转换为DATE')
        require(all(next(r[1] for r in schema if r[0]==key)=='VARCHAR' for key in ['User_id','Merchant_id','Coupon_id']),'ID未保留为字符串')
        checks['date_and_id_types'] = 'PASS'
        for key, value in expected.items():
            require(actual[key]==value if not isinstance(value,float) else abs(actual[key]-value)<1e-12, f'SQL/Python不一致: {key}')
        checks['sql_python_reconciliation'] = 'PASS'
    # Confirm source versions also after import; no writes to originals.
    for item in inputs:
        require(digest(ROOT/item['path'])==item['actual_sha256'],'运行后原始文件改变')
        item['post_run_sha256_matches']=True
    checks['four_input_hashes_before_after'] = 'PASS'
    evidence={'scope':'线下训练CSV前10000条，不是随机样本，不代表全量核销率','sql_result':actual,'independent_python_result':expected,'sql_python_match':True,'database':str(db),'tables':['smoke_raw','smoke_typed','smoke_receipts','smoke_config'],'schema':schema,'full_baseline_recomputed':False}
    write_json(QA/'input_manifest.json',{'inputs':inputs,'profile':str(profile_path),'profile_sha256':digest(profile_path),'notebook':str(notebook),'notebook_sha256':digest(notebook),'reuse_reason':'已执行全量profile与关键Notebook；四个O2O输入指纹一致'})
    write_json(QA/'smoke_result.json',evidence)
    write_json(QA/'environment.json',{'python':sys.version,'executable':sys.executable,'platform':platform.platform(),'duckdb':duckdb.__version__,'isolation':sys.prefix!=sys.base_prefix,'dependency_lock':str(WORK/'requirements.txt'),'external_database_service':False})
    with (QA/'smoke_result.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=actual.keys());writer.writeheader();writer.writerow(actual)
    return actual

if __name__=='__main__':
    QA.mkdir(exist_ok=True)
    state_path=QA/'stage_status.json'
    state=json.loads(state_path.read_text()) if state_path.exists() else {}
    state.setdefault('stages',{str(i):'NOT IMPLEMENTED' for i in range(1,7)})
    state.pop('error', None)
    checks={k:'FAIL' for k in CHECK_KEYS}
    state.update(updated_at=datetime.now(ZoneInfo('Asia/Singapore')).isoformat(),stage0='RUNNING',manual_learning='未验证；不阻止技术验收')
    invalidate_downstream(state,0)
    state['stages']['0']='RUNNING'
    state['checks']=checks
    write_json(state_path,state)
    try:
        result=main(checks)
        require(all(v=='PASS' for v in checks.values()), '阶段0存在未通过检查项')
        state.update(stage0='PASS',checks=checks,sample_result=result,limits=['未重新扫描全量业务指标；按匹配指纹复用既有核验','Distance距离映射、Action原始来源说明与再分发许可未独立确认','阶段0仅验证小样本；正式数据层与后续阶段状态见stages'])
        print(json.dumps({'stage0':'PASS','sample_result':result,'qa':str(QA)},ensure_ascii=False,indent=2))
    except Exception as exc:
        for k in CHECK_KEYS:
            if checks.get(k)!='PASS':
                checks[k]='FAIL'
        stages=state.get('stages',{})
        for s in ('1','2','3'):
            if stages.get(s)=='PASS':
                stages[s]='STALE'
            if state.get(f'stage{s}')=='PASS':
                state[f'stage{s}']='STALE'
        state.update(stage0='FAIL',checks=checks,error=str(exc))
        traceback.print_exc()
    finally:
        state['stages']['0']=state['stage0']
        write_json(QA/'stage_status.json',state)
    if state['stage0']!='PASS':sys.exit(1)
