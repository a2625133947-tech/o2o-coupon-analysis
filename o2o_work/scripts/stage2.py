"""Stage2 real SQL execution, source-backed aggregation checks and descriptive reviews."""
from pathlib import Path
from datetime import datetime,date
from zoneinfo import ZoneInfo
from collections import Counter,defaultdict
from functools import lru_cache
import csv,json,hashlib,os,sys,time,traceback,math
from verification import replay, require_stage
import duckdb

WORK=Path(__file__).resolve().parents[1]
ROOT=WORK.parent
SQL_FILES=['01_overview.sql','02_receipt_cohort.sql','03_coupon.sql','04_distance.sql','05_merchant.sql','06_redemption_lag.sql','07_comparable_review.sql']
TABLES=['overview','receipt_cohort','receipt_cohort_days1_15','coupon_type_metrics','coupon_threshold_metrics','coupon_discount_band_metrics','coupon_scheme_metrics','coupon_type_month_metrics','distance_metrics','distance_month_metrics','merchant_metrics','merchant_structure','merchant_distribution','redemption_lag','redemption_lag_summary','redemption_lag_by_coupon_type','comparison_cells','comparison_strata','comparison_summary']

def table_contracts():
 groups={
 '01_overview.sql':{'overview':'entire mature receipt population plus whole offline source consumption counts'},
 '02_receipt_cohort.sql':{'receipt_cohort':'receipt month','receipt_cohort_days1_15':'receipt month, receipt calendar day1 through15 only'},
 '03_coupon.sql':{'coupon_type_metrics':'coupon type','coupon_threshold_metrics':'coupon type + threshold; direct discount threshold null','coupon_discount_band_metrics':'coupon type + nominal discount band','coupon_scheme_metrics':'coupon type + raw discount format + threshold + reduction + nominal fraction','coupon_type_month_metrics':'receipt month + coupon type'},
 '04_distance.sql':{'distance_metrics':'raw distance code or unknown','distance_month_metrics':'receipt month + raw distance code or unknown'},
 '05_merchant.sql':{'merchant_metrics':'merchant','merchant_structure':'merchant + receipt month + coupon type + threshold + distance code','merchant_distribution':'distribution across receipt-issuing merchants; not rates'},
 '06_redemption_lag.sql':{'redemption_lag':'day0 through15; receipt denominator repeated, never sum denominator across days','redemption_lag_summary':'successful mature receipts only; wait-day statistics, not rates','redemption_lag_by_coupon_type':'coupon type + day0 through15; per-type denominator repeated'},
 '07_comparable_review.sql':{'comparison_cells':'comparison + side + exact control strata','comparison_strata':'comparison + exact strata; both sides retained including unmatched','comparison_summary':'comparison + common-support rule; standardized numerator is a weighted equivalent quantity'}
 }
 return {table:{'sql':file,'grain':grain,'rate_contract':'standard record rate=redeemed15_records/receipt_records; user share=redeemed15_users/receipt_users when present; lag rates use explicit mature_receipt_records or total_redeemed15_records; review rate uses its named numerator/denominator','window':'receipt2016-01-01 through2016-06-15; consume observed through2016-06-30; days0-15 inclusive','duplicates':'retained','zero_denominator':'NULL/not computable','user_counts':'distinct within row only; nonadditive'} for file,tables in groups.items() for table,grain in tables.items()}

def now():return datetime.now(ZoneInfo('Asia/Singapore')).isoformat()
def digest(path):
 with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def dump(path,obj):Path(path).write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
def quote(s):return "'"+str(s).replace("'","''")+"'"
def read_rows(c,sql):
 cur=c.execute(sql);cols=[d[0] for d in cur.description];return [dict(zip(cols,r)) for r in cur.fetchall()]
def export(c,table,path):
 cur=c.execute('SELECT * FROM '+table)
 with path.open('w',encoding='utf-8',newline='') as f:
  wr=csv.writer(f);wr.writerow([d[0] for d in cur.description])
  while rows:=cur.fetchmany(25000):wr.writerows(rows)
DEFAULT_BANDS={'le_5pct':[0,0.05],'5_to_10pct':[0.05,0.1],'10_to_20pct':[0.1,0.2],'20_to_30pct':[0.2,0.3],'gt_30pct':[0.3,1]}
def validated_bands(bands=None):
 b=DEFAULT_BANDS if bands is None else bands
 if not isinstance(b,dict) or not b:raise ValueError('折扣分档不能为空')
 result=[];previous=0.0
 for name,bounds in b.items():
  if not isinstance(bounds,(list,tuple)) or len(bounds)!=2:raise ValueError('每档必须有下界和上界')
  lo,hi=map(float,bounds)
  if not math.isfinite(lo) or not math.isfinite(hi) or round(lo,10)!=previous or not 0<=lo<hi<=1:raise ValueError('折扣分档须按顺序连续覆盖[0,1]，不能重叠、留空或越界')
  result.append((name,(round(lo,10),round(hi,10))));previous=round(hi,10)
 if previous!=1:raise ValueError('折扣分档须覆盖到1')
 return result

def band(frac,bands=None):
 f=round(frac,10)
 if not math.isfinite(f) or not 0<=f<=1:raise ValueError('名义折扣必须为[0,1]有限值')
 for name,(_,hi) in validated_bands(bands):
  if f<=hi:return name
 raise ValueError('折扣没有对应分档')

def setup(c,cfg,source):
 items=validated_bands(cfg.get('nominal_discount_bands'))
 c.execute('ATTACH '+quote(source)+' AS source (READ_ONLY)')
 c.execute('CREATE TABLE stage2_config(small_sample_receipts INT,same_day_monthly_max_day INT,comparison_min_receipts_each_stratum INT)')
 c.execute('INSERT INTO stage2_config VALUES (?,?,?)',[cfg['small_sample_receipts'],cfg['same_day_monthly_max_day'],cfg['comparison_min_receipts_each_stratum']])
 whens='\n '.join(f"WHEN round(nominal_discount_fraction,10)<={hi!r} THEN {quote(name)}" for name,(_,hi) in items)
 c.execute(f"""CREATE VIEW receipts AS SELECT *,CASE
 {whens}
 ELSE NULL END AS nominal_discount_band
 FROM source.fact_coupon_receipts WHERE mature15""")
def specs(largest):
 return [
 ('direct_vs_threshold','coupon_type','direct_discount','threshold_reduction','month + merchant + distance + nominal_discount_band','总体差异大，3月方向相反；力度按名义比例分档，满减门槛仍不可完全控制'),
 ('20_5_vs_20_1','discount_rate','20:5','20:1','month + merchant + distance + same threshold/type','同20门槛的高领券量券方案；未按效果最大挑选'),
 ('100_30_vs_100_10','discount_rate','100:30','100:10','month + merchant + distance + same threshold/type','同100门槛的高领券量券方案；未按效果最大挑选'),
 ('distance0_vs10','distance','0','10','month + merchant + exact discount scheme','编码0与10总体差异大；只比较编码，不推测公里'),
 ('march_vs_january','receipt_month','2016-03','2016-01','merchant + distance + exact discount scheme','最高与最低总体月份率，探索性选择；同时另查各月1—15日'),
 ('largest_merchant_vs_peers','merchant',largest,'other_merchants','month + distance + exact discount scheme','按领券量选择最大商家，参照其余商家同月份/方案/距离')]

def add(counter,key,y):
 if key not in counter:counter[key]=[0,0]
 counter[key][0]+=1;counter[key][1]+=int(y)
@lru_cache(maxsize=1000)
def parse(value):return datetime.strptime(value,'%Y%m%d').date() if value not in ('null','') else None
@lru_cache(maxsize=100)
def coupon(discount):
 if ':' in discount:
  a,b=map(float,discount.split(':'));return 'threshold_reduction',a,b/a
 return 'direct_discount',None,1-float(discount)

def independent_csv(path,comparison_specs,cutoff,bands=None):
 groups={t:{} for t in ['receipt_cohort','receipt_cohort_days1_15','coupon_type_metrics','coupon_threshold_metrics','coupon_discount_band_metrics','coupon_scheme_metrics','coupon_type_month_metrics','distance_metrics','distance_month_metrics','merchant_metrics']}
 cells={};lag=Counter();users=set();redusers=set();month_users=defaultdict(set);month_redusers=defaultdict(set);source_n=ordinary=consumption=coupon_consumption=0
 with path.open(encoding='utf-8',newline='') as f:
  for row in csv.DictReader(f):
   source_n+=1;r=parse(row['Date_received']);d=parse(row['Date']);has_coupon=row['Coupon_id'] not in ('null','')
   consumption+=int(d is not None);ordinary+=int(not has_coupon and r is None and d is not None)
   coupon_consumption+=int(has_coupon and r is not None and d is not None)
   if not(has_coupon and r and (cutoff-r).days>=15):continue
   delay=(d-r).days if d else None;y=delay is not None and 0<=delay<=15
   u=row['User_id'];m=row['Merchant_id'];discount=row['Discount_rate'];dist=row['Distance'] if row['Distance'] not in ('null','') else 'unknown';month=r.strftime('%Y-%m')
   typ,threshold,frac=coupon(discount);b=band(frac,bands)
   users.add(u);month_users[month].add(u)
   if y:lag[delay]+=1;redusers.add(u);month_redusers[month].add(u)
   for table,key in [('receipt_cohort',(month,)),('coupon_type_metrics',(typ,)),('coupon_threshold_metrics',(typ,threshold)),('coupon_discount_band_metrics',(typ,b)),('coupon_scheme_metrics',(discount,)),('coupon_type_month_metrics',(month,typ)),('distance_metrics',(dist,)),('distance_month_metrics',(month,dist)),('merchant_metrics',(m,))]:add(groups[table],key,y)
   if r.day<=15:add(groups['receipt_cohort_days1_15'],(month,),y)
   for cid,dimension,a,other,*_ in comparison_specs:
    value={'coupon_type':typ,'discount_rate':discount,'distance':dist,'receipt_month':month,'merchant':m}[dimension]
    if dimension=='merchant':value=a if m==a else other
    if value not in (a,other):continue
    side='a' if value==a else 'b'
    strmonth='all_months' if dimension=='receipt_month' else month
    scheme=b if dimension=='coupon_type' else f'{typ}:threshold={threshold if threshold is not None else "none"}' if dimension=='discount_rate' else discount
    merchant='all_merchants' if dimension=='merchant' else m
    distance='all_distances' if dimension=='distance' else dist
    add(cells,(cid,side,strmonth,scheme,merchant,distance),y)
 return {'groups':groups,'cells':cells,'lag':dict(lag),'receipt_users':len(users),'redeemed15_users':len(redusers),'month_users':{k:len(v) for k,v in month_users.items()},'month_redeemed_users':{k:len(v) for k,v in month_redusers.items()},'source_records':source_n,'ordinary_consumption_records':ordinary,'consumption_records':consumption,'coupon_consumption_records':coupon_consumption}

class Audit:
 def __init__(self,path):self.path=path;self.checks=[];self.events=[]
 def check(self,name,actual,expected):
  ok=math.isclose(actual,expected,rel_tol=1e-10,abs_tol=1e-10) if isinstance(expected,float) and actual is not None else actual==expected
  def retained(value):
   if isinstance(value,dict):
    content=repr(sorted(value.items(),key=lambda item:repr(item[0]))).encode()
    return {'mapping_entries':len(value),'deterministic_digest':hashlib.sha256(content).hexdigest()}
   return value
  self.checks.append({'check':name,'actual':retained(actual),'expected':retained(expected),'status':'PASS' if ok else 'FAIL'})
  if not ok:
   dump(self.path/'checks.json',self.checks);raise RuntimeError(f'{name}: 实际{actual!r} != 期望{expected!r}')
 def log(self,msg):self.events.append({'time':now(),'message':msg});dump(self.path/'execution_log.json',self.events);print(msg,flush=True)

def validate(c,a,py,baseline,profile,cfg):
 overview=read_rows(c,'SELECT * FROM overview')[0]
 for k in ['source_records','ordinary_consumption_records','consumption_records','coupon_consumption_records','receipt_users','redeemed15_users']:a.check('python.overview.'+k,overview[k],py[k])
 a.check('baseline.source_records',overview['source_records'],baseline['rows']);a.check('baseline.receipt_records',overview['receipt_records'],baseline['states']['receipt_records']);a.check('baseline.redeemed15',overview['redeemed15_records'],baseline['states']['redeemed_within15'])
 a.check('baseline.rate15',overview['redemption_rate15'],baseline['all_receipt_redeem15_rate'])
 keycols={'receipt_cohort':['receipt_month'],'receipt_cohort_days1_15':['receipt_month'],'coupon_type_metrics':['coupon_type'],'coupon_threshold_metrics':['coupon_type','threshold'],'coupon_discount_band_metrics':['coupon_type','nominal_discount_band'],'coupon_scheme_metrics':['discount_rate'],'coupon_type_month_metrics':['receipt_month','coupon_type'],'distance_metrics':['distance_group'],'distance_month_metrics':['receipt_month','distance_group'],'merchant_metrics':['merchant_id']}
 for table,cols in keycols.items():
  rows=read_rows(c,'SELECT * FROM '+table);actual={tuple(r[k] for k in cols):[r['receipt_records'],r['redeemed15_records']] for r in rows}
  a.check('python.group_counts.'+table,actual,py['groups'][table])
  if table!='receipt_cohort_days1_15':
   a.check('sum.receipts.'+table,sum(r['receipt_records'] for r in rows),overview['receipt_records']);a.check('sum.redeemed.'+table,sum(r['redeemed15_records'] for r in rows),overview['redeemed15_records'])
  for r in rows:
   identity=table+':'+str(tuple(r[k] for k in cols))
   a.check('rate.'+identity,r['redemption_rate15'],r['redeemed15_records']/r['receipt_records'])
   a.check('user_rate.'+identity,r['redemption_user_share15'],r['redeemed15_users']/r['receipt_users'])
   a.check('small_sample.'+identity,r['small_sample'],r['receipt_records']<cfg['small_sample_receipts'])
   if table=='receipt_cohort':
    a.check('monthly_users.'+r['receipt_month'],r['receipt_users'],py['month_users'][r['receipt_month']]);a.check('monthly_redeemed_users.'+r['receipt_month'],r['redeemed15_users'],py['month_redeemed_users'][r['receipt_month']])
 for r in read_rows(c,'SELECT * FROM receipt_cohort'):
  old=next(x for x in profile['receipt_cohorts'] if x['month']==r['receipt_month'])
  a.check('profile.month.receipts.'+r['receipt_month'],r['receipt_records'],old['receipt']);a.check('profile.month.redeemed.'+r['receipt_month'],r['redeemed15_records'],old['redeemed15'])
 a.check('june_partial_flag',c.execute("SELECT period_scope FROM receipt_cohort WHERE receipt_month='2016-06'").fetchone()[0],'partial_month_1_to_15')
 a.check('same_day_scope',c.execute("SELECT count(*) FROM receipt_cohort_days1_15 WHERE day(receipt_end)>15").fetchone()[0],0)
 a.check('merchant_structure_sums',c.execute('SELECT sum(receipt_records),sum(redeemed15_records) FROM merchant_structure').fetchone(),(overview['receipt_records'],overview['redeemed15_records']))
 a.check('merchant_structure_grain_unique',c.execute('SELECT count(*) FROM merchant_structure').fetchone()[0],c.execute('SELECT count(*) FROM (SELECT DISTINCT merchant_id, receipt_month, coupon_type, threshold, distance_group FROM merchant_structure)').fetchone()[0])
 lagrows=read_rows(c,'SELECT * FROM redemption_lag');a.check('lag_days',len(lagrows),16)
 cumulative=0
 for r in lagrows:
  day=r['lag_day'];n=py['lag'].get(day,0);cumulative+=n
  for k,val in [('daily_redeemed15_records',n),('cumulative_redeemed15_records',cumulative),('mature_receipt_records',overview['receipt_records']),('total_redeemed15_records',overview['redeemed15_records']),('daily_redemption_rate15',n/overview['receipt_records']),('cumulative_redemption_rate15',cumulative/overview['receipt_records']),('share_of_successful_redemptions',n/overview['redeemed15_records']),('cumulative_share_of_successes',cumulative/overview['redeemed15_records'])]:a.check('lag.'+str(day)+'.'+k,r[k],val)
 a.check('day15_matches_overview',lagrows[-1]['cumulative_redeemed15_records'],overview['redeemed15_records'])
 a.check('lag_type_sum',c.execute('SELECT sum(daily_redeemed15_records) FROM redemption_lag_by_coupon_type').fetchone()[0],overview['redeemed15_records'])
 # Independently reproduce all comparison cell membership from original CSV.
 cell_rows=read_rows(c,'SELECT * FROM comparison_cells')
 sqlcells={(r['comparison_id'],r['group_side'],r['stratum_month'],r['stratum_scheme'],r['stratum_merchant'],r['stratum_distance']):[r['receipt_records'],r['redeemed15_records']] for r in cell_rows}
 a.check('comparison.cell_counts_from_original_csv',sqlcells,py['cells'])
 for r in read_rows(c,'SELECT * FROM comparison_summary'):
  cid=r['comparison_id'];sides={side:{key[2:]:v for key,v in py['cells'].items() if key[0]==cid and key[1]==side} for side in ['a','b']}
  minimum=1 if r['support_rule']=='common_min1' else cfg['comparison_min_receipts_each_stratum']
  keys=[key for key in sides['a'].keys()&sides['b'].keys() if min(sides['a'][key][0],sides['b'][key][0])>=minimum]
  a.check(cid+'.'+r['support_rule']+'.included_strata',r['included_strata'],len(keys))
  weight=sum(sides['a'][k][0]+sides['b'][k][0] for k in keys)
  a.check(cid+'.'+r['support_rule']+'.weight_denominator',r['standard_weight_denominator'],weight if keys else None)
  for side in ['a','b']:
   prefix=cid+'.'+r['support_rule']+'.'+side
   all_n=sum(v[0] for v in sides[side].values());all_y=sum(v[1] for v in sides[side].values());cn=sum(sides[side][k][0] for k in keys);cy=sum(sides[side][k][1] for k in keys)
   a.check(prefix+'.all_receipts',r[side+'_all_receipts'],all_n);a.check(prefix+'.all_redeemed',r[side+'_all_redeemed15'],all_y);a.check(prefix+'.common_receipts',r[side+'_common_receipts'],cn);a.check(prefix+'.common_redeemed',r[side+'_common_redeemed15'],cy);a.check(prefix+'.coverage',r[side+'_coverage'],cn/all_n if all_n else None)
   numerator=math.fsum((sides['a'][k][0]+sides['b'][k][0])*sides[side][k][1]/sides[side][k][0] for k in keys)
   a.check(prefix+'.standardized_numerator',r[side+'_standardized_numerator_receipt_equivalents'],numerator if keys else None)
   a.check(prefix+'.standardized_rate',r[side+'_standardized_rate15'],numerator/weight if weight else None)
 return overview

def update(status,details):
 p=WORK/'qa/stage_status.json';state=json.loads(p.read_text());state['updated_at']=now();state['stage2']=status;state['stages']['2']=status;state['stage2_details']=details
 if status in ('RUNNING','FAIL'):
  if state.get('stages',{}).get('3')=='PASS':state['stages']['3']='STALE'
  if state.get('stage3')=='PASS':state['stage3']='STALE'
 dump(p,state)

def main():
 runid=datetime.now(ZoneInfo('Asia/Singapore')).strftime('%Y%m%dT%H%M%S%f');out=WORK/'results/stage2/runs'/runid;qa=WORK/'qa/stage2/runs'/runid;out.mkdir(parents=True);qa.mkdir(parents=True)
 details={'run_id':runid,'results_dir':str(out),'qa_dir':str(qa),'started_at':now()};a=Audit(qa);started=time.monotonic()
 try:
  state=json.loads((WORK/'qa/stage_status.json').read_text())
  require_stage(state,0);require_stage(state,1);update('RUNNING',details)
  cfg=json.loads((WORK/'config/stage2.json').read_text());config=json.loads((WORK/'config/analysis.json').read_text());a.check('review_min30_contract',cfg['comparison_min_receipts_each_stratum'],30);a.check('same_days_contract',cfg['same_day_monthly_max_day'],15)
  lock=json.loads((WORK/'config/input_lock.json').read_text());spec=next(x for x in lock['files'] if x['profile_key']=='offline');source_csv=ROOT/spec['path'];a.check('input_sha256_before',digest(source_csv),spec['sha256']);database=WORK/'data/o2o.duckdb';a.check('stage1_db_exists',database.exists(),True);dbhash=digest(database)
  stage1_latest=json.loads((WORK/'qa/stage1/latest.json').read_text());profile=json.loads((ROOT/lock['profile']).read_text())['offline']
  with duckdb.connect(str(out/'stage2_metrics.duckdb')) as c:
   setup(c,cfg,database);a.check('stage1_loaded_run_id',c.execute('SELECT run_id FROM source.pipeline_metadata').fetchone()[0],stage1_latest['run_id']);a.check('source_not_filtered_silently',c.execute('SELECT count(*) FROM source.fact_coupon_receipts WHERE NOT mature15').fetchone()[0],0)
   for name in SQL_FILES[:6]:a.log('执行 '+name);c.execute((WORK/'sql'/name).read_text())
   largest=c.execute('SELECT merchant_id FROM merchant_metrics ORDER BY receipt_records DESC,merchant_id LIMIT 1').fetchone()[0]
   review_specs=specs(largest);c.execute('CREATE TABLE comparison_specs(comparison_id VARCHAR,dimension VARCHAR,group_a VARCHAR,group_b VARCHAR,controls VARCHAR,selection_reason VARCHAR)');c.executemany('INSERT INTO comparison_specs VALUES (?,?,?,?,?,?)',review_specs);dump(qa/'review_selection.json',review_specs)
   a.log('执行可比范围SQL，保留共同覆盖和每组至少30条的两种结果。');c.execute((WORK/'sql'/SQL_FILES[-1]).read_text())
   a.log('从原CSV独立计算专题汇总、独立用户及所有比较分层的归属。');py=independent_csv(source_csv,review_specs,date.fromisoformat(config['training_observation_end']),bands=cfg.get('nominal_discount_bands'));overview=validate(c,a,py,config['offline_baseline'],profile,cfg)
   from test_stage2 import run_tests
   testresult=run_tests();dump(qa/'semantic_tests.json',testresult);a.check('semantic_edge_cases',testresult['passed'],True)
   files={}
   for table in TABLES:
    export(c,table,out/(table+'.csv'));files[table]={'csv':str(out/(table+'.csv')),'rows':c.execute('SELECT count(*) FROM '+table).fetchone()[0],'sha256':digest(out/(table+'.csv'))}
   # Recompute source rates on the dedup sensitivity view without changing main results.
   dedup=read_rows(c,'SELECT count(*) AS receipt_records, sum(redeemed15_mature::BIGINT) AS redeemed15_records, sum(redeemed15_mature::BIGINT)::DOUBLE/count(*) AS redemption_rate15 FROM source.fact_coupon_receipts_dedup WHERE mature15')[0];dump(qa/'dedup_overview.json',dedup)
   a.check('dedup_baseline_receipts',dedup['receipt_records'],profile['dedup_sensitivity']['receipts']);a.check('dedup_baseline_redeemed',dedup['redeemed15_records'],profile['dedup_sensitivity']['redeemed15'])
   a.check('original_csv_unchanged',digest(source_csv),spec['sha256']);a.check('stage1_database_unchanged',digest(database),dbhash)
   provenance={'input':spec,'stage1_run_id':stage1_latest['run_id'],'stage1_database_sha256':dbhash,'config':config,'review_config':cfg,'sql_hashes':{name:digest(WORK/'sql'/name) for name in SQL_FILES},'stage2_script_sha256':digest(Path(__file__)),'companion_script_hashes':{name:digest(WORK/'scripts'/name) for name in ['stage2_findings.py','stage2_notebook.py','verification.py']},'semantic_test_sha256':digest(WORK/'tests/test_stage2.py'),'stage2_config_sha256':digest(WORK/'config/stage2.json'),'source':str(database),'receipt_window':['2016-01-01','2016-06-15'],'consumption_observation_end':'2016-06-30','grain':'source records; duplicates retained','table_contracts':table_contracts(),'common_support':'both observed; standardized with pooled receipt weights, descriptive only','rates':'all ratios retain numerators/denominators; grouped distinct users never additive','small_sample':'<100 receipts is an operational caution, not statistical assurance at >=100','sql_execution':'real DuckDB 1.4.3','files':files}
   dump(out/'manifest.json',provenance);dump(qa/'source_validation.json',{'database_unchanged':True,'csv_unchanged':True,'stage1_run_id':stage1_latest['run_id']});dump(qa/'independent_summary.json',{k:v for k,v in py.items() if k not in ['groups','cells']})
   write_findings(c,out,qa,details,overview,cfg)
   c.execute('DROP VIEW receipts')
   c.execute('CREATE TABLE analysis_provenance(run_id VARCHAR, provenance_json VARCHAR)');c.execute('INSERT INTO analysis_provenance VALUES (?,?)',[runid,json.dumps(provenance,ensure_ascii=False)]);c.execute('CHECKPOINT')
  a.log('SQL与独立复算已通过，生成并执行Notebook伴随材料。')
  from stage2_notebook import build_execute
  presentation=build_execute(out,qa)
  a.check('notebook_executed',presentation['executed'],True)
  details.update(completed_at=now(),elapsed_seconds=time.monotonic()-started,checks_passed=len(a.checks),overview=overview,presentation=presentation)
  previous_pointer=WORK/'results/stage2/latest.json'
  previous=json.loads(previous_pointer.read_text()).get('results_dir') if previous_pointer.exists() else None
  replay_info=replay(out,previous,TABLES,'stage2_metrics.duckdb',WORK)
  dump(qa/'replay_verification.json',replay_info);dump(WORK/'qa/stage2/复现检查.json',replay_info)
  dump(qa/'checks.json',a.checks);dump(qa/'status.json',{'stage2':'PASS',**details});dump(WORK/'qa/stage2/latest.json',{'stage2':'PASS',**details});dump(WORK/'results/stage2/latest.json',{'stage2':'PASS',**details});update('PASS',details)
  (WORK/'qa/stage2/验收报告.md').write_text(f'# 阶段2验收\n\nPASS。完成时间：{details["completed_at"]}。{len(a.checks)}项检查通过；六专题及可比复核SQL实际执行。\n\n源CSV与阶段1库哈希前后相同，所有分组记录分子分母对齐总体，第15日累计核销64,395。专题汇总及比较分层归属从原CSV独立计算。每种可比范围均报告覆盖，权重分子分母可回查；没有因果或显著性主张。Notebook已从头执行。图像结构校验通过；视觉检查另见本次qa目录visual_check.json，脚本不会代填人工检查结论。\n\n结果目录：`{out}`\n\n证据目录：`{qa}`\n',encoding='utf-8')
  a.log(f'阶段2 PASS：{len(a.checks)}项检查；结果目录 {out}');print(json.dumps(details,ensure_ascii=False,default=str,indent=2))
 except Exception as e:
  details.update(error=str(e),completed_at=now());dump(qa/'checks.json',a.checks);dump(qa/'status.json',{'stage2':'FAIL',**details});update('FAIL',details);a.log('阶段2 FAIL：'+str(e));traceback.print_exc();return 1
 return 0

# A narrative builder is kept separate from calculation SQL.
from stage2_findings import write_findings
if __name__=='__main__':
 sys.path.insert(0,str(WORK/'tests'))
 sys.exit(main())
