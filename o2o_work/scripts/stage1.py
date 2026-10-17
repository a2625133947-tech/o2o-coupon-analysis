"""Full offline data layer with exact source lineage and fail-closed reconciliation."""
from __future__ import annotations
import csv
import hashlib
import itertools
import json
import os
import platform
import sys
import time
import traceback
from collections import Counter
from datetime import datetime, date
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo
from verification import publish_database, require_stage
import duckdb

WORK = Path(__file__).resolve().parents[1]
ROOT = WORK.parent
FIELDS = ['User_id', 'Merchant_id', 'Coupon_id', 'Discount_rate', 'Distance', 'Date_received', 'Date']

def now():
    return datetime.now(ZoneInfo('Asia/Singapore')).isoformat()

def digest(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

def dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str)+'\n', encoding='utf-8')

def query_one(con, sql):
    cursor = con.execute(sql)
    return dict(zip([d[0] for d in cursor.description], cursor.fetchone()))

def export_csv(con, sql, path):
    cursor = con.execute(sql)
    with path.open('w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([d[0] for d in cursor.description])
        while batch := cursor.fetchmany(25000):
            writer.writerows(batch)

def load_config(con, config):
    con.execute('CREATE TABLE analysis_config (history_start DATE, training_observation_end DATE, redemption_min INTEGER, redemption_max INTEGER)')
    con.execute('INSERT INTO analysis_config VALUES (?, ?, ?, ?)', [config['history_start'], config['training_observation_end'], *config['redemption_days_inclusive']])

def import_raw(con, path, source):
    # Preserve original null/empty tokens. Source order is checked on every real row.
    con.execute('CREATE TABLE raw_offline AS SELECT ?::VARCHAR AS source_file, row_number() OVER ()::BIGINT AS source_row_number, ? || \':\' || (row_number() OVER ())::VARCHAR AS record_id, * FROM read_csv(?, header=true, all_varchar=true, force_not_null=[\'User_id\',\'Merchant_id\',\'Coupon_id\',\'Discount_rate\',\'Distance\',\'Date_received\',\'Date\'], parallel=false, strict_mode=true)', [source,source,str(path)])

def build_layers(con, config):
    load_config(con, config)
    # The locked stage0 contract currently defines inclusive day 0 through 15.
    if config['redemption_days_inclusive'] != [0,15]:
        raise ValueError('SQL实现固定为0—15日；变更口径需同步修订代码与验收')
    con.execute((WORK/'sql/10_stage1_layers.sql').read_text())

@lru_cache(maxsize=1000)
def parse_date(value):
    if value in ('null',''):
        return None
    if len(value)!=8 or not value.isdigit():
        raise ValueError(f'非YYYYMMDD日期: {value!r}')
    return datetime.strptime(value, '%Y%m%d').date()

def compare_csv_and_python(con, path, source, cutoff):
    """All rows compared in source order; Python computes labels from original CSV."""
    cursor=con.cursor()
    cursor.execute('SELECT source_file, source_row_number, record_id, '+', '.join('"'+k+'"' for k in FIELDS)+' FROM raw_offline ORDER BY source_row_number')
    counts=Counter()
    missing=Counter({k:0 for k in FIELDS})
    with path.open(encoding='utf-8', newline='') as f:
        reader=csv.reader(f)
        if next(reader)!=FIELDS: raise RuntimeError('CSV列结构不匹配')
        offset=0
        while batch:=list(itertools.islice(reader,25000)):
            stored=cursor.fetchmany(len(batch))
            if len(stored)!=len(batch):raise RuntimeError('原始表行数短于源CSV')
            for i,(raw, db) in enumerate(zip(batch,stored),offset+1):
                if len(raw)!=7 or tuple(db)!=(source,i,f'{source}:{i}',*raw):
                    raise RuntimeError(f'源行追溯/原值不一致，数据行{i}')
                u,m,c,discount,distance,received,consumed=raw
                for field,value in zip(FIELDS,raw):missing[field]+=int(value in ('null',''))
                r=parse_date(received); d=parse_date(consumed)
                receipt=c not in ('null','') and r is not None
                lag=(d-r).days if r and d else None
                mature=receipt and (cutoff-r).days>=15
                redeemed=receipt and lag is not None and 0<=lag<=15
                counts.update({'rows':1,'receipt_records':int(receipt),
                 'receipt_with_consumption':int(receipt and d is not None),
                 'receipt_without_consumption':int(receipt and d is None),
                 'consumption_without_receipt':int(c in ('null','') and r is None and d is not None),
                 'redeemed_within15':int(redeemed),'lag_over15':int(receipt and lag is not None and lag>15),
                 'negative_lag':int(lag is not None and lag<0),'neither_date':int(r is None and d is None),
                 'coupon_receipt_mismatch':int((c not in ('null','')) != (r is not None)),
                 'mature_receipts':int(mature),'mature_redeemed15':int(mature and redeemed),
                 'immature_receipts':int(receipt and not mature)})
            offset+=len(batch)
        if cursor.fetchone() is not None:raise RuntimeError('原始表行数长于源CSV')
    cursor.close()
    return dict(counts),dict(missing)

def update_stage(status, details):
    path=WORK/'qa/stage_status.json'
    state=json.loads(path.read_text()) if path.exists() else {'stages':{str(i):'NOT IMPLEMENTED' for i in range(1,7)}}
    state.pop('error',None)
    state['updated_at']=now()
    state['stage1']=status
    state.setdefault('stages',{})['1']=status
    if status in ('RUNNING','FAIL'):
        for s in ('2','3'):
            if state['stages'].get(s)=='PASS':
                state['stages'][s]='STALE'
            if state.get('stage'+s)=='PASS':
                state['stage'+s]='STALE'
    state['stage1_details']=details
    dump(path,state)

class Audit:
    def __init__(self,run_dir):
        self.run_dir=run_dir
        self.checks=[]
        self.events=[]
    def log(self,message):
        self.events.append({'time':now(),'message':message})
        dump(self.run_dir/'execution_log.json',self.events)
        print(message,flush=True)
    def check(self,name,actual,expected):
        ok=abs(actual-expected)<1e-12 if isinstance(expected,float) else actual==expected
        self.checks.append({'check':name,'actual':actual,'expected':expected,'status':'PASS' if ok else 'FAIL'})
        dump(self.run_dir/'checks.json',self.checks)
        if not ok:raise RuntimeError(f'对账失败 {name}: 实际{actual!r}，基准{expected!r}')

def main():
    run_id=datetime.now(ZoneInfo('Asia/Singapore')).strftime('%Y%m%dT%H%M%S%f')
    run_dir=WORK/'qa/stage1/runs'/run_id
    run_dir.mkdir(parents=True)
    audit=Audit(run_dir)
    started=time.monotonic()
    published=False
    details={'run_id':run_id,'run_dir':str(run_dir),'started_at':now()}
    update_stage('RUNNING',details)
    build_db=WORK/'data'/f'.stage1_build_{run_id}.duckdb'
    final_db=WORK/'data/o2o.duckdb'
    try:
        if (WORK/'qa/stage_status.json').exists():
            require_stage(json.loads((WORK/'qa/stage_status.json').read_text()),0)
        config=json.loads((WORK/'config/analysis.json').read_text())
        lock=json.loads((WORK/'config/input_lock.json').read_text())
        spec=next(x for x in lock['files'] if x['profile_key']=='offline')
        path=ROOT/spec['path']
        profile_path=ROOT/lock['profile']
        profile=json.loads(profile_path.read_text())['offline']
        audit.check('isolated_environment',sys.prefix!=sys.base_prefix,True)
        audit.check('duckdb_version',duckdb.__version__,'1.4.3')
        audit.check('input_bytes',path.stat().st_size,spec['bytes'])
        audit.check('input_sha256_before',digest(path),spec['sha256'])
        audit.check('profile_baseline_consistency',config['offline_baseline']['rows'],profile['rows'])
        for key,val in config['offline_baseline']['states'].items():
            audit.check('profile_state_lock.'+key,profile['states'][key],val)
        # Targeted semantic tests run the same transformation SQL on edge cases.
        from test_stage1 import run_tests
        synthetic=run_tests()
        dump(run_dir/'semantic_tests.json',synthetic)
        audit.check('semantic_edge_cases',synthetic['passed'],True)
        audit.log('输入指纹与边界语义检查通过，开始全量导入线下训练CSV。')
        with duckdb.connect(str(build_db)) as con:
            con.execute("SET memory_limit='2GB'")
            con.execute('SET threads=4')
            con.execute('SET preserve_insertion_order=true')
            # parallel=false + insertion order + full-row CSV comparison establish row lineage.
            import_raw(con,path,spec['path'])
            audit.check('raw_count',con.execute('SELECT count(*) FROM raw_offline').fetchone()[0],profile['rows'])
            audit.log('原始层导入完成，开始类型转换、状态标签、优惠拆分与完全重复定位。')
            build_layers(con,config)
            actual=query_one(con,(WORK/'sql/11_stage1_quality.sql').read_text())
            dump(run_dir/'full_metrics.json',actual)
            export_csv(con,'SELECT record_state, count(*) AS records FROM fact_offline_records GROUP BY record_state ORDER BY record_state',run_dir/'state_counts.csv')
            export_csv(con,'SELECT reason, count(*) AS records FROM offline_anomalies GROUP BY reason ORDER BY reason',run_dir/'anomaly_counts.csv')
            export_csv(con,'SELECT * FROM offline_anomalies ORDER BY source_row_number, reason',run_dir/'anomalies.csv')
            export_csv(con,'SELECT record_id, source_file, source_row_number, first_source_row_number, exact_duplicate_rank, exact_duplicate_group_size FROM fact_offline_records WHERE is_exact_duplicate_excess ORDER BY source_row_number',run_dir/'duplicate_excess.csv')
            audit.log('分析层已生成，开始逐行比较源CSV原值/源行号，并用Python独立重算全部状态。')
            py,py_missing=compare_csv_and_python(con,path,spec['path'],date.fromisoformat(config['training_observation_end']))
            dump(run_dir/'independent_python.json',{'metrics':py,'missing':py_missing,'raw_lineage_rows_compared':py['rows']})
            for key,val in py.items():audit.check('python_vs_sql.'+key,actual[key],val)
            audit.check('source_row_lineage',py['rows'],profile['rows'])
            audit.check('fact_count',actual['rows'],profile['rows'])
            audit.check('record_id_unique',actual['distinct_record_ids'],actual['rows'])
            audit.check('user_count',actual['users'],profile['distinct']['User_id'])
            for key,val in profile['states'].items():audit.check('baseline.'+key,actual[key],val)
            for key,target in [('mature_receipts','receipts'),('mature_redeemed15','redeemed15'),('immature_receipts','immature_receipts')]:
                audit.check('baseline.'+key,actual[key],profile['mature15'][target])
            audit.check('baseline.redemption_rate15',actual['redemption_rate15'],profile['all_receipt_redeem15_rate'])
            audit.check('baseline.duplicate_excess',actual['exact_duplicate_excess'],profile['exact_duplicate_excess'])
            audit.check('invalid_received_date',actual['invalid_received_date'],profile['invalid_date_strings']['Date_received'])
            audit.check('invalid_consumed_date',actual['invalid_consumed_date'],profile['invalid_date_strings']['Date'])
            audit.check('anomaly_rows',con.execute('SELECT count(*) FROM offline_anomalies').fetchone()[0],0)
            state_counts=dict(con.execute('SELECT record_state,count(*) FROM fact_offline_records GROUP BY record_state').fetchall())
            audit.check('exclusive_state_total',sum(state_counts.values()),actual['rows'])
            for state,key in [('ordinary_consumption','consumption_without_receipt'),('receipt_without_consumption','receipt_without_consumption'),('redeemed_within15','redeemed_within15'),('consumed_after15','lag_over15')]:
                audit.check('state.'+state,state_counts.get(state,0),profile['states'][key])
            for col in FIELDS:
                norm={'Date_received':'received_text','Date':'consumed_text','Distance':'distance_code'}.get(col,col.lower())
                missing=con.execute(f'SELECT count(*) FROM fact_offline_records WHERE "{norm}" IS NULL').fetchone()[0]
                audit.check('missing.'+col,missing,profile['missing'][col])
                audit.check('python_missing.'+col,missing,py_missing[col])
            distance=dict(con.execute("SELECT coalesce(distance_code, '<NA>'),count(*) FROM fact_offline_records GROUP BY distance_code").fetchall())
            discounts=dict(con.execute("SELECT coalesce(discount_rate, '<NA>'),count(*) FROM fact_offline_records GROUP BY discount_rate").fetchall())
            audit.check('distance_raw_codes',distance,profile['distance_counts'])
            audit.check('discount_raw_formats',discounts,profile['discount_counts'])
            for prefix,col in [('received','received_date'),('consumed','consumed_date')]:
                expected=profile['periods']['received' if prefix=='received' else 'consumption']
                audit.check(prefix+'_date_min',str(actual[prefix+'_min']),expected[0])
                audit.check(prefix+'_date_max',str(actual[prefix+'_max']),expected[1])
            dedup=query_one(con,'SELECT count(*) AS rows, count(*) FILTER (WHERE is_coupon_receipt) AS receipts, count(*) FILTER (WHERE redeemed_within15) AS redeemed15, count(*) FILTER (WHERE mature15) AS mature_receipts, count(*) FILTER (WHERE redeemed15_mature) AS mature_redeemed15 FROM fact_offline_records_dedup')
            for key,val in profile['dedup_sensitivity'].items():audit.check('dedup.'+key,dedup[key],val)
            # GROUP BY all original values is an independent SQL check of duplicate-window selection.
            unique_raw=con.execute('SELECT count(*) FROM (SELECT '+', '.join('"'+k+'"' for k in FIELDS)+' FROM raw_offline GROUP BY ALL)').fetchone()[0]
            audit.check('dedup_raw_groupby',dedup['rows'],unique_raw)
            audit.check('duplicate_row_accounting',dedup['rows']+actual['exact_duplicate_excess'],actual['rows'])
            audit.check('receipt_fact_count',con.execute('SELECT count(*) FROM fact_coupon_receipts').fetchone()[0],actual['receipt_records'])
            audit.check('no_ordinary_in_receipts',con.execute("SELECT count(*) FROM fact_coupon_receipts WHERE record_state='ordinary_consumption'").fetchone()[0],0)
            schemas={table:con.execute(f'DESCRIBE {table}').fetchall() for table in ['raw_offline','fact_offline_records','fact_coupon_receipts','fact_offline_records_dedup','fact_coupon_receipts_dedup','offline_anomalies','offline_duplicate_map']}
            for field in FIELDS:audit.check('raw_type.'+field,dict((r[0],r[1]) for r in schemas['raw_offline'])[field],'VARCHAR')
            fact_schema=dict((r[0],r[1]) for r in schemas['fact_offline_records'])
            for field in ['user_id','merchant_id','coupon_id']:audit.check('id_type.'+field,fact_schema[field],'VARCHAR')
            for field in ['received_date','consumed_date']:audit.check('date_type.'+field,fact_schema[field],'DATE')
            dump(run_dir/'table_schemas.json',schemas)
            export_csv(con,"SELECT record_id, source_file, source_row_number, record_state, user_id, merchant_id, coupon_id, discount_rate, distance_group, received_date, consumed_date, lag_days, mature15, redeemed_within15, coupon_type, threshold, reduction, nominal_discount_fraction FROM (SELECT *, row_number() OVER (PARTITION BY record_state ORDER BY source_row_number) AS sample_rank FROM fact_offline_records) WHERE sample_rank=1 ORDER BY record_state",run_dir/'state_examples.csv')
            sensitivity={'original':{'rows':actual['rows'],'receipts':actual['receipt_records'],'redeemed15':actual['redeemed_within15'],'mature_receipts':actual['mature_receipts'],'mature_redeemed15':actual['mature_redeemed15'],'redemption_rate15':actual['redemption_rate15']},'exact_dedup':{**dedup,'redemption_rate15':dedup['mature_redeemed15']/dedup['mature_receipts']},'duplicates_retained_in_main':True,'duplicate_excess':actual['exact_duplicate_excess']}
            sensitivity['rate_difference_percentage_points']=(sensitivity['exact_dedup']['redemption_rate15']-actual['redemption_rate15'])*100
            dump(run_dir/'dedup_sensitivity.json',sensitivity)
            with (run_dir/'dedup_sensitivity.csv').open('w',encoding='utf-8',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=['version','rows','receipts','redeemed15','mature_receipts','mature_redeemed15','redemption_rate15']);writer.writeheader()
                for version in ['original','exact_dedup']:writer.writerow({'version':version,**sensitivity[version]})
            audit.check('input_sha256_after',digest(path),spec['sha256'])
            provenance={**details,'source':spec,'source_absolute_path':str(path),'config':config,'config_sha256':digest(WORK/'config/analysis.json'),'input_lock_sha256':digest(WORK/'config/input_lock.json'),'script_sha256':digest(Path(__file__)), 'verification_script_sha256':digest(WORK/'scripts/verification.py'),'sql_hashes':{n:digest(WORK/'sql'/n) for n in ['10_stage1_layers.sql','11_stage1_quality.sql']},'profile_sha256':digest(profile_path),'duckdb':duckdb.__version__,'python':sys.version,'platform':platform.platform(),'source_hash_unchanged':True,'database':str(final_db),'scope':'offline_train_only'}
            con.execute('CREATE TABLE pipeline_metadata (run_id VARCHAR, completed_at VARCHAR, provenance_json VARCHAR)')
            con.execute('INSERT INTO pipeline_metadata VALUES (?, ?, ?)',[run_id,now(),json.dumps(provenance,ensure_ascii=False)])
            dump(run_dir/'provenance.json',provenance)
            con.execute('CHECKPOINT')
        elapsed=time.monotonic()-started
        details.update(completed_at=now(),elapsed_seconds=elapsed,database=str(final_db),checks_passed=len(audit.checks),full_metrics=actual,dedup_sensitivity=sensitivity)
        dump(run_dir/'status.json',{'stage1':'PASS',**details})
        write_report(run_dir,details,state_counts)
        # Keep any previous accepted DB until all new checks pass, reports are written, and file is closed.
        def publish():
            dump(WORK/'qa/stage1/latest.json',{'stage1':'PASS',**details})
            with duckdb.connect(str(final_db),read_only=True) as reopened:
                measured={t:reopened.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ['raw_offline','fact_offline_records','fact_coupon_receipts','fact_offline_records_dedup','fact_coupon_receipts_dedup','offline_anomalies']}
                if reopened.execute('SELECT run_id FROM pipeline_metadata').fetchone()[0]!=run_id:raise RuntimeError('Published provenance mismatch')
            dump(WORK/'qa/stage1/复现检查.json',{'accepted_run_id':run_id,'read_only_reopen_counts':measured,'database_bytes':final_db.stat().st_size,'invocation_cwd':str(Path.cwd()),'cross_run_comparison':'NOT_PERFORMED; source-row independent reconciliation is in this run QA'})
            (WORK/'qa/stage1/质量与验收报告.md').write_text((run_dir/'质量与验收报告.md').read_text()+'\n本次详细证据目录：`'+str(run_dir)+'`。\n',encoding='utf-8')
            update_stage('PASS',details)
        publish_database(build_db,final_db,publish,[WORK/'qa/stage1/latest.json',WORK/'qa/stage1/复现检查.json',WORK/'qa/stage_status.json',WORK/'qa/stage1/质量与验收报告.md'])
        published=True
        audit.log(f'阶段1 PASS：{actual["rows"]:,}源记录；{actual["receipt_records"]:,}领券；{actual["redeemed_within15"]:,}条15日核销；{len(audit.checks)}项检查通过。')
        print(json.dumps({'stage1':'PASS','database':str(final_db),'report':str(run_dir/'质量与验收报告.md'),'redemption_rate15':actual['redemption_rate15'],'dedup':sensitivity['exact_dedup'],'elapsed_seconds':elapsed},ensure_ascii=False,indent=2))
    except Exception as exc:
        if not published and build_db.exists():
            try:
                if build_db.stat().st_size<=16384:
                    build_db.unlink(missing_ok=True)
            except OSError:
                pass
        details.update(completed_at=now(),error=str(exc),published_new_database=published,diagnostic_build_database=str(build_db) if build_db.exists() else None,elapsed_seconds=time.monotonic()-started)
        dump(run_dir/'status.json',{'stage1':'FAIL',**details})
        update_stage('FAIL',details)
        audit.log(('阶段1失败，未发布新数据库；诊断记录保留。原因：' if not published else '阶段1在数据库替换后发生后续写入异常：')+str(exc))
        traceback.print_exc()
        return 1
    return 0

def write_report(run_dir,details,state_counts):
    actual=details['full_metrics'];s=details['dedup_sensitivity'];d=s['exact_dedup']
    text=f'''# 阶段1：线下数据层质量与验收报告

状态：PASS。完成时间：{details['completed_at']}（Asia/Singapore）。全量实际执行，耗时{details['elapsed_seconds']:.2f}秒；{details['checks_passed']}项检查通过。输入仅线下训练表；线上、7月测试和提交模板未混入。

## 输入、追溯与数据层

正式库：`{details['database']}`。输入指纹和代码/配置版本见provenance.json及库内pipeline_metadata。原CSV运行前后SHA256一致。

raw_offline保留7字段原始字符串，包括字面量null；source_row_number从1开始不含表头，record_id为文件路径:源数据行号，仅技术定位。所有1,754,884行均由Python CSV读取与DuckDB原始表逐行比较，原值、顺序、源文件、行号、ID一致。不是只检查首末几行。

fact_offline_records与原表一对一；ID为VARCHAR，缺失转NULL，日期为DATE。fact_coupon_receipts只包含有效领券。优惠拆分、名义比例、lag、领券月份、ISO星期、完整观察期和核销标签已建立。未知距离保留unknown，不补0。

## 全量状态对账

|状态|记录数|进入15日分母|进入15日分子|
|---|---:|---|---|
|普通消费|{state_counts.get('ordinary_consumption',0):,}|否|否|
|领券无消费日期|{state_counts.get('receipt_without_consumption',0):,}|是（完整观察期）|否|
|15日内核销|{state_counts.get('redeemed_within15',0):,}|是|是|
|超过15日消费|{state_counts.get('consumed_after15',0):,}|是|否|
|合计|{actual['rows']:,}|有效领券共{actual['receipt_records']:,}|{actual['redeemed_within15']:,}|

领券后有消费日期共{actual['receipt_with_consumption']:,}；普通消费+领券记录=总行数。有效领券均完整观察15日，未成熟领券为0。主核销率={actual['redeemed_within15']:,}/{actual['mature_receipts']:,}={actual['redemption_rate15']:.10%}。所有关键基准及独立Python全量计算均一致。

## 质量检查与异常

日期解析失败、负lag、券与领券日不一致、主ID缺失/格式异常、券优惠缺失/格式异常、距离越界、日期超出训练窗口、未分类状态均为0；offline_anomalies已建立，anomalies.csv保留表头且无数据行。正常未核销Date缺失977,900条不算坏数据；距离缺失106,003条保留unknown。全部7字段缺失数量、优惠原格式分布、距离编码分布与既有profile一致。

领券日期2016-01-01至06-15；消费日期2016-01-01至06-30。6月仍标为部分月份。若未来发现异常，不静默删除：一条异常原因对应一行，并保留record_id可回查raw_offline；异常行数和异常记录数应分别计算。

## 完全去重敏感性

|版本|源记录|有效领券|15日核销|核销率|
|---|---:|---:|---:|---:|
|保留源记录（主口径）|{actual['rows']:,}|{actual['receipt_records']:,}|{actual['redeemed_within15']:,}|{actual['redemption_rate15']:.7%}|
|7字段完全去重（敏感性）|{d['rows']:,}|{d['receipts']:,}|{d['redeemed15']:,}|{d['redemption_rate15']:.7%}|

完全重复额外行{s['duplicate_excess']:,}；去重后率差为{s['rate_difference_percentage_points']:.7f}个百分点。主表未删除任何重复行。去重视图仅保留各组最早源行；offline_duplicate_map与duplicate_excess.csv提供首行、组规模及排名。另用原始7字段GROUP BY复核去重行数。相同字段不证明同一个业务事件，敏感性版本不是建议删除重复的清洗结果。

## 验证与可复现

`./o2o_work/run.sh stage1`从原CSV重新建立全量层；只有全部检查通过后替换正式数据库，失败标记FAIL并保留运行诊断与此前正式库。每次日志独立保存至qa/stage1/runs目录。运行前用合成边界样本验证第0/15/16日、跨月、无券消费、坏日期、负lag、缺失ID、优惠格式、未知距离及重复规则。合成样本仅用于测试，不混入真实结果；semantic_tests.json记录覆盖。

证据：checks.json、full_metrics.json、independent_python.json、state_counts.csv、anomaly_counts.csv、anomalies.csv、duplicate_excess.csv、dedup_sensitivity.json/csv、state_examples.csv、table_schemas.json、provenance.json、execution_log.json。四种状态真实样例附源行定位，可供人工学习；不标记用户已独立复现。

Distance精确距离映射、来源真实性/代表性/再分发许可仍未独立确认。本阶段不计算不存在的金额指标，也不形成营销因果结论。本阶段仅建立线下基础数据层，阶段2专题SQL与阶段3用户分层由各自阶段入口执行，阶段4及以后未实施。
'''
    (run_dir/'质量与验收报告.md').write_text(text,encoding='utf-8')


if __name__=='__main__':
    sys.path.insert(0,str(WORK/'tests'))
    sys.exit(main())
