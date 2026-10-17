"""Meaningful date, maturity, temporal-visibility and partition boundary tests."""
from pathlib import Path
import sys,duckdb
WORK=Path(__file__).resolve().parents[1];sys.path.insert(0,str(WORK/'scripts'))

def run_tests():
 from stage1 import build_layers
 from stage3 import config_table,rule_table,bootstrap
 checks=[]
 def check(name,ok):
  if not ok:raise AssertionError(name)
  checks.append(name)
 records=[('101','1','1','0.9','0','20160415','20160430'),('102','1','2','0.9','0','20160416','20160417'),('103','1','3','0.9','0','20160415','20160501'),('104','1','null','null','0','null','20160430'),('104','1','null','null','0','null','20160430'),('104','1','null','null','0','null','20160430'),('105','1','null','null','0','null','20160430'),('106','1','null','null','0','null','20160101'),('107','1','8','0.9','0','20160420','null'),('108','1','9','0.9','0','20160531','20160615'),('101','1','10','0.9','0','20160531','20160616'),('104','1','11','0.9','0','20160515','20160515')]
 records += [('109','1','20','20:5','0','20160101','null') for _ in range(4)]
 with duckdb.connect(':memory:') as c:
  c.execute('CREATE TABLE raw_offline(source_file VARCHAR,source_row_number BIGINT,record_id VARCHAR,User_id VARCHAR,Merchant_id VARCHAR,Coupon_id VARCHAR,Discount_rate VARCHAR,Distance VARCHAR,Date_received VARCHAR,Date VARCHAR)');c.executemany('INSERT INTO raw_offline VALUES(?,?,?,?,?,?,?,?,?,?)',[('fixture.csv',i,f'fixture.csv:{i}',*r) for i,r in enumerate(records,1)])
  build_layers(c,{'history_start':'2016-01-01','training_observation_end':'2016-06-30','redemption_days_inclusive':[0,15]});c.execute("ATTACH ':memory:' AS source");c.execute('CREATE TABLE source.fact_offline_records AS SELECT * FROM fact_offline_records');c.execute('CREATE TABLE source.fact_coupon_receipts AS SELECT * FROM fact_coupon_receipts')
  config_table(c,{'history_start':'2016-01-01','segmentation_asof':'2016-05-01','evaluation_receipt_start':'2016-05-01','evaluation_receipt_end_inclusive':'2016-05-31','evaluation_observation_end':'2016-06-15'})
  c.execute((WORK/'sql/08_user_features_asof.sql').read_text());rule_table(c,{'recent_days_max':35,'high_consumption_min':3,'high_receipt_min':4});c.execute((WORK/'sql/09_user_segments.sql').read_text());c.execute((WORK/'sql/10_user_segment_evaluation.sql').read_text())
  def feature(user):
   cur=c.execute('SELECT * FROM user_segments WHERE user_id=?',[user]);return dict(zip([x[0] for x in cur.description],cur.fetchone()))
  r=feature('101');check('Apr15 receipt matures before May1; Apr30 redemption visible',r['history_mature_receipt_records']==1 and r['history_redeemed15_records']==1)
  r=feature('102');check('Apr16 receipt excluded from historical rate even after early redemption',r['history_mature_receipt_records']==0 and r['history_redemption_rate15'] is None and r['history_consumption_records']==1)
  r=feature('103');check('May1 consumption does not enter historical RF',r['history_consumption_records']==0 and r['recency_days'] is None and r['history_redeemed15_records']==0)
  r=feature('104');check('exact duplicates retained in RF frequency but active days deduplicated',r['history_consumption_records']==3 and r['history_consumption_active_days']==1)
  check('recent high frequency correctly labeled',r['segment_id']=='recent_high_frequency')
  check('recent low frequency separate',feature('105')['segment_id']=='recent_low_frequency')
  check('old consumption separate',feature('106')['segment_id']=='not_recent_consumption')
  check('known receipt-only history is not unknown',feature('107')['segment_id']=='no_observed_consumption')
  r=feature('108');check('future-cohort unknown keeps history flag false and rate null',r['segment_id']=='unknown_history' and not r['has_observed_history'] and r['history_redemption_rate15'] is None)
  check('high receipt zero-redemption priority before no consumption',feature('109')['segment_id']=='high_receipt_zero_redemption')
  check('May31 + day15 June15 succeeds',c.execute("SELECT evaluation_redeemed15 FROM evaluation_receipts WHERE user_id='108'").fetchone()[0])
  check('day16 June16 cannot enter observation or numerator',not c.execute("SELECT evaluation_redeemed15 FROM evaluation_receipts WHERE user_id='101'").fetchone()[0])
  check('labels mutually exclusive one row per user/asof',c.execute('SELECT count(*)=count(DISTINCT user_id) FROM user_segments').fetchone()[0])
  check('receipt join does not expand',c.execute('SELECT count(*) FROM evaluation_receipts').fetchone()[0]==3)
  check('zero May-receipt segments remain zero/null',c.execute("SELECT mature_receipt_records,record_redemption_rate15 FROM segment_evaluation WHERE segment_id='high_receipt_zero_redemption'").fetchone()==(0,None))
  check('segment_user_evaluation has all 6 segments',c.execute('SELECT count(*) FROM segment_user_evaluation').fetchone()[0]==6)
  check('zero May-receipt segments in segment_user_evaluation remain zero/null',c.execute("SELECT evaluation_users,mature_receipt_records,mean_within_user_redemption_rate15,receipts_per_user FROM segment_user_evaluation WHERE segment_id='high_receipt_zero_redemption'").fetchone()==(0,0,None,None))
  bootstrap(c,replicates=20,seed=20261014)
  check('bootstrap intervals cover all 6 segments x 3 metrics',c.execute('SELECT count(*) FROM segment_bootstrap_intervals').fetchone()[0]==18)
  check('grouped user and record counts reconcile',c.execute('SELECT sum(mature_receipt_records),sum(redeemed15_records),sum(mature_receipt_users) FROM segment_evaluation').fetchone()==(3,2,3))
  before=c.execute('SELECT * FROM historical_user_features ORDER BY user_id').fetchall()
  c.execute("UPDATE source.fact_offline_records SET consumed_date=DATE '2030-01-01' WHERE consumed_date>=DATE '2016-05-01'");c.execute('DROP TABLE user_features_asof');c.execute('DROP TABLE segmentation_universe');c.execute('DROP TABLE historical_user_features');c.execute('DROP TABLE historical_events');c.execute((WORK/'sql/08_user_features_asof.sql').read_text())
  check('altering future consumption cannot change any historical feature',before==c.execute('SELECT * FROM historical_user_features ORDER BY user_id').fetchall())
 return {'passed':True,'checks':checks,'scope':'synthetic fixture only; no synthetic rows in project outputs'}
if __name__=='__main__':print(run_tests())
