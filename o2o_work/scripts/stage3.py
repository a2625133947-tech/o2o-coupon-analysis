"""Historical RF/coupon features, frozen segmentation and subsequent May evaluation."""
from pathlib import Path
from datetime import datetime,date
from zoneinfo import ZoneInfo
from functools import lru_cache
from collections import defaultdict,Counter
import csv,json,hashlib,math,sys,time,traceback
from verification import replay, require_stage
import duckdb
import numpy as np

WORK=Path(__file__).resolve().parents[1];ROOT=WORK.parent
FEATURE_SQL='08_user_features_asof.sql';SEGMENT_SQL='09_user_segments.sql';EVAL_SQL='10_user_segment_evaluation.sql'
FEATURE_COLUMNS=['user_id','asof_date','has_observed_history','history_active_days','history_receipt_active_days','history_consumption_active_days','latest_history_event_date','history_consumption_records','last_consumption_date','recency_days','history_receipt_records','history_mature_receipt_records','history_immature_receipt_records','history_redeemed15_records','history_redemption_rate15']
NAMES={'unknown_history':'未知历史','high_receipt_zero_redemption':'历史多领券零核销','no_observed_consumption':'有历史但无已观察消费','recent_high_frequency':'近期高频消费','recent_low_frequency':'近期低频消费','not_recent_consumption':'非近期消费'}
PRIORITIES=list(NAMES)
TABLES=['user_features_asof','user_segments','segment_coverage','evaluation_receipts','evaluation_user_outcomes','segment_evaluation','segment_user_evaluation','segment_bootstrap_intervals','feature_distribution','feature_bins','segmentation_rules','rule_catalog','user_case_features','user_case_history_events']

def table_contracts():
 grains={'user_features_asof':'user_id + asof_date; historical features plus unknown-history flag','user_segments':'user_id + asof_date; exactly one priority-rule segment','segment_coverage':'segment; entire retrospective universe including users with no May receipts','evaluation_receipts':'one original May receipt source record; record_id retained, duplicates not removed','evaluation_user_outcomes':'user_id + asof_date; May receipt count, redeemed count and at-least-one indicator','segment_evaluation':'segment; May record and distinct-user numerators/denominators','segment_user_evaluation':'segment; record weights, user redemption share and equal-user mean','segment_bootstrap_intervals':'segment + metric; user resampling percentile interval and observed numerator/denominator','feature_distribution':'historical feature; specified eligible population, quartiles and quantiles','feature_bins':'historical feature + ordered bin; observed-history consumers only','segmentation_rules':'one frozen threshold set','rule_catalog':'ordered priority per mutually exclusive group','user_case_features':'one anonymous example per segment','user_case_history_events':'example user + historical event type + record_id; never May outcomes'}
 return {table:{'grain':grain,'time_contract':'history[2016-01-01,2016-05-01); historical receipts mature15 before asof; evaluation May with consumption observed through June15','duplicate_policy':'retain source records; separate fixed-rule exact-dedup sensitivity','rate_contract':'historical redeemed/mature receipts; subsequent record redeemed/mature receipts; user share redeeming users/receipt users; user-mean=sum(personal y/n)/evaluation_users; zero denominator NULL'} for table,grain in grains.items()}

def now():return datetime.now(ZoneInfo('Asia/Singapore')).isoformat()
def dump(p,obj):Path(p).write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
def digest(p):
 with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def quote(s):return "'"+str(s).replace("'","''")+"'"
def rows(c,sql):
 q=c.execute(sql);return [dict(zip([d[0] for d in q.description],r)) for r in q.fetchall()]
def export(c,table,p):
 q=c.execute('SELECT * FROM '+table+' ORDER BY ALL')
 with p.open('w',encoding='utf-8',newline='') as f:
  w=csv.writer(f);w.writerow([d[0] for d in q.description])
  while batch:=q.fetchmany(25000):w.writerows(batch)
def config_table(c,config):
 c.execute('CREATE TABLE stage3_config(history_start DATE,asof_date DATE,evaluation_start DATE,evaluation_end DATE,evaluation_observation_end DATE)')
 c.execute('INSERT INTO stage3_config VALUES(?,?,?,?,?)',[config['history_start'],config['segmentation_asof'],config['evaluation_receipt_start'],config['evaluation_receipt_end_inclusive'],config['evaluation_observation_end']])
def rule_table(c,rules):
 c.execute('CREATE TABLE segmentation_rules(recent_days_max INT,high_consumption_min INT,high_receipt_min INT)');c.execute('INSERT INTO segmentation_rules VALUES(?,?,?)',[rules['recent_days_max'],rules['high_consumption_min'],rules['high_receipt_min']])
def segment(has_history,frequency,recency,mature,redeemed,rules):
 if not has_history:return 'unknown_history'
 if mature>=rules['high_receipt_min'] and redeemed==0:return 'high_receipt_zero_redemption'
 if frequency==0:return 'no_observed_consumption'
 if recency<=rules['recent_days_max'] and frequency>=rules['high_consumption_min']:return 'recent_high_frequency'
 if recency<=rules['recent_days_max']:return 'recent_low_frequency'
 return 'not_recent_consumption'

class Audit:
 def __init__(self,p):self.p=p;self.checks=[];self.events=[]
 def check(self,name,actual,expected):
  ok=math.isclose(actual,expected,rel_tol=1e-10,abs_tol=1e-10) if isinstance(expected,float) and actual is not None else actual==expected
  self.checks.append({'check':name,'actual':actual,'expected':expected,'status':'PASS' if ok else 'FAIL'});dump(self.p/'checks.json',self.checks)
  if not ok:raise RuntimeError(f'{name}: 实际{actual!r}，期望{expected!r}')
 def log(self,msg):self.events.append({'time':now(),'message':msg});dump(self.p/'execution_log.json',self.events);print(msg,flush=True)

def distributions(c):
 c.execute('CREATE TABLE feature_distribution(feature VARCHAR,population VARCHAR,users BIGINT,minimum DOUBLE,q25 DOUBLE,q50 DOUBLE,q75 DOUBLE,q90 DOUBLE,maximum DOUBLE)')
 for name,condition in [('recency_days','history_consumption_records>0'),('history_consumption_records','history_consumption_records>0'),('history_mature_receipt_records','history_mature_receipt_records>0'),('history_redemption_rate15','history_mature_receipt_records>0'),('history_active_days','TRUE')]:
  c.execute(f"INSERT INTO feature_distribution SELECT ?,?,count(*),min({name}),quantile_cont({name},.25),quantile_cont({name},.5),quantile_cont({name},.75),quantile_cont({name},.9),max({name}) FROM historical_user_features WHERE {condition}",[name,condition])
 c.execute("""CREATE TABLE feature_bins AS
 SELECT 'recency_days' AS feature,
 CASE WHEN recency_days<=7 THEN '1-7' WHEN recency_days<=14 THEN '8-14' WHEN recency_days<=35 THEN '15-35' WHEN recency_days<=60 THEN '36-60' WHEN recency_days<=90 THEN '61-90' ELSE '91-121' END AS bin_label,
 CASE WHEN recency_days<=7 THEN 1 WHEN recency_days<=14 THEN 2 WHEN recency_days<=35 THEN 3 WHEN recency_days<=60 THEN 4 WHEN recency_days<=90 THEN 5 ELSE 6 END AS bin_order,count(*) AS users
 FROM historical_user_features WHERE history_consumption_records>0 GROUP BY ALL
 UNION ALL
 SELECT 'history_consumption_records',
 CASE WHEN history_consumption_records=1 THEN '1' WHEN history_consumption_records=2 THEN '2' WHEN history_consumption_records<=5 THEN '3-5' WHEN history_consumption_records<=10 THEN '6-10' ELSE '>10' END,
 CASE WHEN history_consumption_records=1 THEN 1 WHEN history_consumption_records=2 THEN 2 WHEN history_consumption_records<=5 THEN 3 WHEN history_consumption_records<=10 THEN 4 ELSE 5 END,count(*)
 FROM historical_user_features WHERE history_consumption_records>0 GROUP BY ALL""")
 d={r['feature']:r for r in rows(c,'SELECT * FROM feature_distribution')}
 rule={'asof_date':'2016-05-01','recent_days_max':max(1,math.ceil(d['recency_days']['q50'])),'high_consumption_min':max(2,math.ceil(d['history_consumption_records']['q75'])),'high_receipt_min':max(2,math.ceil(d['history_mature_receipt_records']['q90'])),'historical_redemption_condition':'history_redeemed15_records=0','priority':PRIORITIES,'names':NAMES,'derivation':{'recent_days_max':'ceil(有历史消费用户recency_days的中位数)','high_consumption_min':'max(2, ceil(有历史消费用户消费记录频次Q75))','high_receipt_min':'max(2, ceil(有成熟领券用户成熟历史领券频次Q90))','zero_redemption':'历史成熟领券群体核销率Q90为0，因此采用零核销的可解释条件，不任意设置低率分数'},'distribution':d,'rule_selection_uses_may_outcomes':False,'unknown_universe':'历史用户与5月评价用户的并集仅用于回溯评价；未知用户并非已知的5月1日平台名单'}
 rule_table(c,rule)
 c.execute('CREATE TABLE rule_catalog(priority INT,segment_id VARCHAR,segment_name VARCHAR,condition VARCHAR)')
 cond=['NOT has_observed_history',f'history_mature_receipt_records>={rule["high_receipt_min"]} AND history_redeemed15_records=0','history_consumption_records=0',f'recency_days<={rule["recent_days_max"]} AND history_consumption_records>={rule["high_consumption_min"]}',f'recency_days<={rule["recent_days_max"]}','remaining history-consumption users']
 c.executemany('INSERT INTO rule_catalog VALUES(?,?,?,?)',[(i+1,k,NAMES[k],cond[i]) for i,k in enumerate(PRIORITIES)])
 return rule

@lru_cache(maxsize=1000)
def parsed(s):return datetime.strptime(s,'%Y%m%d').date() if s not in ('null','') else None

def independent_csv(path,config):
 start=date.fromisoformat(config['history_start']);asof=date.fromisoformat(config['segmentation_asof']);evstart=date.fromisoformat(config['evaluation_receipt_start']);evend=date.fromisoformat(config['evaluation_receipt_end_inclusive']);cutoff=date.fromisoformat(config['evaluation_observation_end'])
 history={};evaluation={};total=Counter()
 # [consumption_n,last_consumption,receipts,mature_receipts,red15,all_day_bits,cons_day_bits,receipt_day_bits,last_event]
 with path.open(encoding='utf-8',newline='') as f:
  for r in csv.DictReader(f):
   u=r['User_id'];received=parsed(r['Date_received']);consumed=parsed(r['Date']);receipt=r['Coupon_id'] not in ('null','') and received is not None
   isr=receipt and start<=received<asof;isc=consumed is not None and start<=consumed<asof
   if isr or isc:
    h=history.setdefault(u,[0,None,0,0,0,0,0,0,None])
    if isr:
     bit=1<<((received-start).days);h[2]+=1;h[5]|=bit;h[7]|=bit;h[8]=max(h[8],received) if h[8] else received
     mature=(asof-received).days>15
     h[3]+=int(mature);h[4]+=int(mature and consumed is not None and consumed<asof and 0<=(consumed-received).days<=15)
    if isc:
     bit=1<<((consumed-start).days);h[0]+=1;h[1]=max(h[1],consumed) if h[1] else consumed;h[5]|=bit;h[6]|=bit;h[8]=max(h[8],consumed) if h[8] else consumed
   if receipt and evstart<=received<=evend:
    v=evaluation.setdefault(u,[0,0]);v[0]+=1;v[1]+=int(consumed is not None and consumed<=cutoff and 0<=(consumed-received).days<=15)
 for h in history.values():total.update({'history_consumption_records':h[0],'history_receipt_records':h[2],'history_mature_receipt_records':h[3],'history_redeemed15_records':h[4]})
 return history,evaluation,dict(total)

def validate_independent(c,a,history,evaluation,totals,rules,config):
 asof=date.fromisoformat(config['segmentation_asof']);universe=set(history)|set(evaluation);seen=set();coverage=Counter();group_eval=defaultdict(lambda:[0,0,0,0]);segments={};sample_errors=[]
 cursor=c.execute('SELECT '+','.join(FEATURE_COLUMNS)+',segment_id FROM user_segments ORDER BY user_id')
 compared=0
 while batch:=cursor.fetchmany(25000):
  for row in batch:
   u=row[0];h=history.get(u);frequency=h[0] if h else 0;recency=(asof-h[1]).days if h and h[1] else None;mature=h[3] if h else 0;red=h[4] if h else 0;label=segment(h is not None,frequency,recency,mature,red,rules);segments[u]=label
   expected=(u,asof,h is not None,h[5].bit_count() if h else 0,h[7].bit_count() if h else 0,h[6].bit_count() if h else 0,h[8] if h else None,frequency,h[1] if h else None,recency,h[2] if h else 0,mature,(h[2]-h[3]) if h else 0,red,red/mature if mature else None,label)
   for k,(actual,wanted) in enumerate(zip(row,expected)):
    same=math.isclose(actual,wanted,rel_tol=1e-12,abs_tol=1e-12) if isinstance(wanted,float) and actual is not None else actual==wanted
    if not same:raise RuntimeError(f'独立特征/标签不一致：user={u}, column={FEATURE_COLUMNS[k] if k<len(FEATURE_COLUMNS) else "segment_id"}, actual={actual}, expected={wanted}')
   seen.add(u);coverage[label]+=1;compared+=1
 a.check('full_user_feature_and_label_comparison',compared,len(universe));a.check('feature_universe_membership',seen==universe,True)
 for u,v in evaluation.items():
  label=segments[u];stats=group_eval[label];stats[0]+=v[0];stats[1]+=v[1];stats[2]+=1;stats[3]+=int(v[1]>0)
 for r in rows(c,'SELECT * FROM segment_evaluation'):
  v=group_eval[r['segment_id']]
  for key,val in zip(['mature_receipt_records','redeemed15_records','mature_receipt_users','redeemed15_users'],v):a.check('independent_eval.'+r['segment_id']+'.'+key,r[key],val)
  a.check('coverage.'+r['segment_id'],r['universe_users'],coverage[r['segment_id']])
 for key,val in totals.items():a.check('historical_total.'+key,c.execute('SELECT sum('+key+') FROM historical_user_features').fetchone()[0],val)
 # Every evaluation user is checked individually, retaining source-record multiplicity.
 n=0
 for r in rows(c,'SELECT user_id,mature_receipt_records,redeemed15_records FROM evaluation_user_outcomes'):
  if [r['mature_receipt_records'],r['redeemed15_records']]!=evaluation[r['user_id']]:raise RuntimeError('评价用户明细不一致:'+r['user_id'])
  n+=1
 a.check('evaluation_user_comparison',n,len(evaluation))
 return {'historical_users':len(history),'universe_users':len(universe),'may_receipt_users':len(evaluation),'may_receipt_records':sum(v[0] for v in evaluation.values()),'may_redeemed15_records':sum(v[1] for v in evaluation.values()),'may_redeemed15_users':sum(v[1]>0 for v in evaluation.values()),'full_user_features_compared':compared,'historical_totals':totals}

def feature_fingerprint(c):
 h=hashlib.sha256();n=0;q=c.execute('SELECT * FROM historical_user_features ORDER BY user_id')
 while batch:=q.fetchmany(25000):
  for row in batch:h.update((repr(row)+'\n').encode());n+=1
 return n,h.hexdigest()
def alternative_source(database,config,rules,mode):
 c=duckdb.connect(':memory:');c.execute('ATTACH '+quote(database)+' AS baseline (READ_ONLY)');c.execute("ATTACH ':memory:' AS source")
 if mode=='future_mutation':
  c.execute("""CREATE VIEW source.fact_offline_records AS SELECT * EXCLUDE(received_date,consumed_date,redeemed_within15,redeemed15_mature,record_state),
   CASE WHEN received_date>=DATE '2016-05-01' THEN DATE '2030-01-01' ELSE received_date END AS received_date,
   CASE WHEN consumed_date>=DATE '2016-05-01' THEN DATE '2030-01-01' ELSE consumed_date END AS consumed_date,
   NOT redeemed_within15 AS redeemed_within15,NOT redeemed15_mature AS redeemed15_mature,'perturbed_final_state' AS record_state
   FROM baseline.fact_offline_records""")
  c.execute('CREATE VIEW source.fact_coupon_receipts AS SELECT * FROM source.fact_offline_records WHERE is_coupon_receipt')
 else:
  c.execute('CREATE VIEW source.fact_offline_records AS SELECT * FROM baseline.fact_offline_records_dedup');c.execute('CREATE VIEW source.fact_coupon_receipts AS SELECT * FROM baseline.fact_coupon_receipts_dedup')
 config_table(c,config);c.execute((WORK/'sql'/FEATURE_SQL).read_text());rule_table(c,rules);c.execute((WORK/'sql'/SEGMENT_SQL).read_text())
 if mode!='future_mutation':c.execute((WORK/'sql'/EVAL_SQL).read_text())
 return c

def bootstrap(c,replicates=2000,seed=20261014):
 rng=np.random.default_rng(seed);output=[]
 grouped=rows(c,'SELECT segment_id,mature_receipt_records,redeemed15_records,count(*) AS users FROM evaluation_user_outcomes GROUP BY ALL ORDER BY ALL')
 for label in PRIORITIES:
  sample=[r for r in grouped if r['segment_id']==label]
  if not sample:
   for metric in ['record_redemption_rate15','redemption_user_share15','mean_within_user_redemption_rate15']:
    output.append((label,metric,0,replicates,seed,None,None,None,0.0,0.0))
   continue
  n=np.array([r['mature_receipt_records'] for r in sample],dtype=float);y=np.array([r['redeemed15_records'] for r in sample],dtype=float);counts=np.array([r['users'] for r in sample],dtype=np.int64);users=int(counts.sum());draws=rng.multinomial(users,counts/users,size=replicates)
  num=draws@y;den=draws@n;usernum=draws@(y>0).astype(float);mean=(draws@(y/n))/users
  for metric,values,point,numerator,denominator in [('record_redemption_rate15',num/den,float(counts@y/(counts@n)),float(counts@y),float(counts@n)),('redemption_user_share15',usernum/users,float(counts@(y>0)/users),float(counts@(y>0)),float(users)),('mean_within_user_redemption_rate15',mean,float(counts@(y/n)/users),float(counts@(y/n)),float(users))]:
   lo,hi=np.quantile(values,[.025,.975]);output.append((label,metric,users,replicates,seed,point,float(lo),float(hi),numerator,denominator))
 c.execute('CREATE TABLE segment_bootstrap_intervals(segment_id VARCHAR,metric VARCHAR,resampled_users BIGINT,replicates INT,seed INT,point_estimate DOUBLE,ci95_low DOUBLE,ci95_high DOUBLE,observed_numerator DOUBLE,observed_denominator DOUBLE)');c.executemany('INSERT INTO segment_bootstrap_intervals VALUES(?,?,?,?,?,?,?,?,?,?)',output)
 return {'replicates':replicates,'seed':seed,'numpy_version':np.__version__,'rng_algorithm':rng.bit_generator.__class__.__name__,'unit':'user; multinomial over exact (receipt count, redemption count) classes is equivalent to resampling individual users','interval':'95% percentile bootstrap, conditional on observed May receipt users; users assumed independent; not causal or multiplicity adjusted'}

def update(status,details):
 p=WORK/'qa/stage_status.json';s=json.loads(p.read_text());s['updated_at']=now();s['stage3']=status;s['stages']['3']=status;s['stage3_details']=details;dump(p,s)

def main():
 runid=datetime.now(ZoneInfo('Asia/Singapore')).strftime('%Y%m%dT%H%M%S%f');out=WORK/'results/stage3/runs'/runid;qa=WORK/'qa/stage3/runs'/runid;out.mkdir(parents=True);qa.mkdir(parents=True);started=time.monotonic();a=Audit(qa);details={'run_id':runid,'started_at':now(),'results_dir':str(out),'qa_dir':str(qa)}
 try:
  state=json.loads((WORK/'qa/stage_status.json').read_text());require_stage(state,0);require_stage(state,1);require_stage(state,2);a.check('stage1_db_exists',(WORK/'data/o2o.duckdb').exists(),True);update('RUNNING',details)
  config=json.loads((WORK/'config/analysis.json').read_text());a.check('fixed_asof',config['segmentation_asof'],'2016-05-01');a.check('fixed_evaluation_start',config['evaluation_receipt_start'],'2016-05-01');a.check('fixed_evaluation_end',config['evaluation_receipt_end_inclusive'],'2016-05-31');a.check('fixed_observation_end',config['evaluation_observation_end'],'2016-06-15')
  lock=json.loads((WORK/'config/input_lock.json').read_text());spec=next(x for x in lock['files'] if x['profile_key']=='offline');path=ROOT/spec['path'];db=WORK/'data/o2o.duckdb';sourcehash=digest(db);a.check('input_sha256_before',digest(path),spec['sha256'])
  stage1=json.loads((WORK/'qa/stage1/latest.json').read_text());stage2=json.loads((WORK/'results/stage2/latest.json').read_text());stage2manifest=json.loads((Path(stage2['results_dir'])/'manifest.json').read_text());a.check('source_matches_stage2',sourcehash,stage2manifest['stage1_database_sha256'])
  with duckdb.connect(str(out/'stage3_users.duckdb')) as c:
   c.execute('ATTACH '+quote(db)+' AS source (READ_ONLY)');a.check('source_run_id',c.execute('SELECT run_id FROM source.pipeline_metadata').fetchone()[0],stage1['run_id']);config_table(c,config)
   a.log('构造仅时点前可见的RF、活跃天数及成熟领券核销特征。');c.execute((WORK/'sql'/FEATURE_SQL).read_text());rules=distributions(c)
   cfg_rules_path = WORK/'config/segmentation_rules.json'
   if cfg_rules_path.exists():
    locked_rules = json.loads(cfg_rules_path.read_text())
    for k in ['asof_date','recent_days_max','high_consumption_min','high_receipt_min','priority']:
     a.check('locked_rule.'+k, rules[k], locked_rules[k])
   else:
    dump(cfg_rules_path, rules)
   dump(out/'segmentation_rules.json', rules)
   a.log('依据历史分布锁定互斥规则，再计算5月后续评价。');c.execute((WORK/'sql'/SEGMENT_SQL).read_text());c.execute((WORK/'sql'/EVAL_SQL).read_text())
   a.check('feature_unique_grain',c.execute('SELECT count(*) FROM user_features_asof').fetchone()[0],c.execute('SELECT count(*) FROM (SELECT DISTINCT user_id,asof_date FROM user_features_asof)').fetchone()[0]);a.check('segment_unique_grain',c.execute('SELECT count(*) FROM user_segments').fetchone()[0],c.execute('SELECT count(DISTINCT user_id) FROM user_segments').fetchone()[0]);a.check('no_history_event_on_or_after_asof',c.execute("SELECT count(*) FROM historical_events WHERE event_date>=DATE '2016-05-01'").fetchone()[0],0);a.check('latest_date_before_asof',c.execute("SELECT count(*) FROM user_features_asof WHERE latest_history_event_date>=asof_date OR last_consumption_date>=asof_date").fetchone()[0],0)
   a.check('historical_mature_cutoff',str(c.execute("SELECT max(received_date) FROM source.fact_coupon_receipts WHERE received_date+INTERVAL 15 DAY<DATE '2016-05-01'").fetchone()[0]),'2016-04-15')
   a.check('evaluation_join_no_expansion',c.execute('SELECT count(*) FROM evaluation_receipts').fetchone()[0],c.execute("SELECT count(*) FROM source.fact_coupon_receipts WHERE receipt_month='2016-05'").fetchone()[0]);a.check('evaluation_record_id_unique',c.execute('SELECT count(*) FROM evaluation_receipts').fetchone()[0],c.execute('SELECT count(DISTINCT record_id) FROM evaluation_receipts').fetchone()[0]);a.check('all_evaluation_has_segment',c.execute('SELECT count(*) FROM evaluation_receipts WHERE segment_id IS NULL').fetchone()[0],0);a.check('all_may_receipts_mature',c.execute('SELECT count(*) FROM evaluation_receipts WHERE NOT evaluation_mature15').fetchone()[0],0);a.check('no_future_success_beyond_observation',c.execute("SELECT count(*) FROM evaluation_receipts WHERE evaluation_redeemed15 AND consumed_date>DATE '2016-06-15'").fetchone()[0],0)
   a.log('从原CSV独立复算全部用户历史特征、标签及5月用户结局。');history,ev,totals=independent_csv(path,config);independent=validate_independent(c,a,history,ev,totals,rules,config);dump(qa/'independent_summary.json',independent)
   for key,value in [('may_receipt_records',215348),('may_redeemed15_records',21580),('may_receipt_users',148439),('may_redeemed15_users',14860)]:a.check('may_baseline.'+key,independent[key],value)
   a.check('coverage_users_sum',c.execute('SELECT sum(universe_users) FROM segment_coverage').fetchone()[0],independent['universe_users'])
   for col,target in [('mature_receipt_records','may_receipt_records'),('redeemed15_records','may_redeemed15_records'),('mature_receipt_users','may_receipt_users'),('redeemed15_users','may_redeemed15_users')]:a.check('evaluation_partition.'+col,c.execute('SELECT sum('+col+') FROM segment_evaluation').fetchone()[0],independent[target])
   for r in rows(c,'SELECT * FROM segment_evaluation'):
    a.check('record_rate.'+r['segment_id'],r['record_redemption_rate15'],r['redeemed15_records']/r['mature_receipt_records'] if r['mature_receipt_records'] else None);a.check('user_rate.'+r['segment_id'],r['redemption_user_share15'],r['redeemed15_users']/r['mature_receipt_users'] if r['mature_receipt_users'] else None)
   a.log('改变全部未来事件日期和最终核销标签，检验历史特征是否完全不变。');original_fingerprint=feature_fingerprint(c)
   with alternative_source(db,config,rules,'future_mutation') as altered:
    future_fingerprint=feature_fingerprint(altered)
   a.check('future_mutation_historical_user_count',future_fingerprint[0],original_fingerprint[0]);a.check('future_mutation_historical_feature_digest',future_fingerprint[1],original_fingerprint[1]);dump(qa/'future_mutation_test.json',{'historical_feature_fingerprint':original_fingerprint,'mutated_fingerprint':future_fingerprint,'changed':'all receipt/consumption dates>=asof shifted to2030; final outcome flags inverted for all rows; record_state overwritten','comparison':'historical_user_features only; cohort membership is deliberately separate','pass':True})
   a.log('用固定规则做完全去重敏感性，并进行用户级重抽样。')
   with alternative_source(db,config,rules,'exact_dedup') as dedup:
    labels={r['user_id']:r['segment_id'] for r in rows(dedup,'SELECT user_id,segment_id FROM user_segments')};transitions=Counter()
    for r in rows(c,'SELECT user_id,segment_id FROM user_segments'):transitions[(r['segment_id'],labels[r['user_id']])]+=1
    sensitivity={'rules_frozen':True,'label_users_compared':len(labels),'segment_changes':sum(v for (old,new),v in transitions.items() if old!=new),'dedup_evaluation':rows(dedup,'SELECT * FROM segment_evaluation'),'main_duplicates_retained':True};dump(out/'dedup_sensitivity.json',sensitivity)
    with (out/'dedup_segment_transitions.csv').open('w',encoding='utf-8',newline='') as f:
     writer=csv.writer(f);writer.writerow(['main_segment','dedup_segment','users']);writer.writerows((old,new,n) for (old,new),n in sorted(transitions.items()))
   boot=bootstrap(c);dump(out/'bootstrap_method.json',boot)
   user_measures={r['segment_id']:r for r in rows(c,'SELECT * FROM segment_user_evaluation')}
   for interval in rows(c,'SELECT * FROM segment_bootstrap_intervals'):
    a.check('bootstrap_point.'+interval['segment_id']+'.'+interval['metric'],interval['point_estimate'],user_measures[interval['segment_id']][interval['metric']])
    ok_bounds = (interval['ci95_low'] is None and interval['ci95_high'] is None) if interval['point_estimate'] is None else (0<=interval['ci95_low']<=interval['ci95_high']<=1)
    a.check('bootstrap_bounds.'+interval['segment_id']+'.'+interval['metric'],ok_bounds,True)
   for feature in ['recency_days','history_consumption_records']:
    a.check('histogram_users.'+feature,c.execute('SELECT sum(users) FROM feature_bins WHERE feature=?',[feature]).fetchone()[0],c.execute('SELECT count(*) FROM historical_user_features WHERE history_consumption_records>0').fetchone()[0])
   from test_stage3 import run_tests
   tests=run_tests();dump(qa/'semantic_tests.json',tests);a.check('semantic_edge_tests',tests['passed'],True)
   c.execute("CREATE TABLE user_case_features AS SELECT * FROM (SELECT *,row_number() OVER(PARTITION BY segment_id ORDER BY user_id) AS sample_rank FROM user_segments) WHERE sample_rank=1")
   c.execute('CREATE TABLE user_case_history_events AS SELECT e.* FROM historical_events e JOIN user_case_features u USING(user_id) ORDER BY user_id,event_date,event_type,record_id')
   cases=rows(c,'SELECT user_id,segment_id FROM user_case_features');dump(out/'user_cases.json',cases)
   files={}
   for table in TABLES:export(c,table,out/(table+'.csv'));files[table]={'rows':c.execute('SELECT count(*) FROM '+table).fetchone()[0],'sha256':digest(out/(table+'.csv'))}
   a.check('input_sha256_after',digest(path),spec['sha256']);a.check('stage1_db_unchanged',digest(db),sourcehash)
   manifest={'run_id':runid,'source_stage1_run':stage1['run_id'],'input':spec,'source_database_sha256':sourcehash,'config':config,'rules':rules,'bootstrap_method':boot,'script_hashes':{name:digest(WORK/'scripts'/name) for name in ['stage3.py','stage3_report.py','stage3_notebook.py','verification.py']},'sql_hashes':{name:digest(WORK/'sql'/name) for name in [FEATURE_SQL,SEGMENT_SQL,EVAL_SQL]},'files':files,'table_contracts':table_contracts(),'scope':'历史事件[2016-01-01,2016-05-01)，历史15日成熟领券最晚4月15日；5月领券评价，观察至6月15日；重复保留','universe':'历史用户与5月领券用户并集用于回溯评价；未知历史用户不是5月1日已知平台名单','rates':'每种记录率/用户占比保留分子分母；每用户平均率另列sum_within_user_redemption_rate15和evaluation_users','not_rfm':'无金额，仅RF及领券核销行为；未知历史不等于新用户'};dump(out/'manifest.json',manifest)
   from stage3_report import write_report
   write_report(c,out,qa,details,rules,independent,sensitivity,boot)
   c.execute('CREATE TABLE provenance(run_id VARCHAR,provenance_json VARCHAR)');c.execute('INSERT INTO provenance VALUES(?,?)',[runid,json.dumps(manifest,ensure_ascii=False)]);c.execute('CHECKPOINT')
  a.log('生成并执行用户分层Notebook。')
  from stage3_notebook import build_execute
  presentation=build_execute(out,qa)
  a.check('notebook_executed',presentation['executed'],True);details.update(completed_at=now(),elapsed_seconds=time.monotonic()-started,checks_passed=len(a.checks),rules={'recent_days_max':rules['recent_days_max'],'high_consumption_min':rules['high_consumption_min'],'high_receipt_min':rules['high_receipt_min']},independent_summary=independent,presentation=presentation)
  previous_pointer=WORK/'results/stage3/latest.json'
  previous=json.loads(previous_pointer.read_text()).get('results_dir') if previous_pointer.exists() else None
  replay_info=replay(out,previous,TABLES,'stage3_users.duckdb',WORK)
  dump(qa/'replay_verification.json',replay_info);dump(WORK/'qa/stage3/复现检查.json',replay_info)
  dump(qa/'status.json',{'stage3':'PASS',**details});dump(WORK/'qa/stage3/latest.json',{'stage3':'PASS',**details});dump(WORK/'results/stage3/latest.json',{'stage3':'PASS',**details});update('PASS',details)
  (WORK/'qa/stage3/验收报告.md').write_text(f'# 阶段3验收\n\nPASS。完成于{details["completed_at"]}，{len(a.checks)}项检查通过。\n\n独立复算{independent["universe_users"]:,}用户完整特征和分层；全部May记录及用户结局对齐基准。未来扰动测试不改变任何历史特征；唯一粒度、观察期和关联不扩行通过。规则仅由历史分布选择。Notebook已执行，图像结构校验通过；视觉检查另见本次qa目录visual_check.json，脚本不会代填人工检查结论。\n\n结果目录：`{out}`\n\n证据目录：`{qa}`\n',encoding='utf-8');a.log('阶段3 PASS：'+str(out));print(json.dumps(details,ensure_ascii=False,indent=2));return 0
 except Exception as e:
  details.update(error=str(e),completed_at=now());dump(qa/'status.json',{'stage3':'FAIL',**details});update('FAIL',details);a.log('阶段3 FAIL：'+str(e));traceback.print_exc();return 1
if __name__=='__main__':
 sys.path.insert(0,str(WORK/'tests'));sys.exit(main())
