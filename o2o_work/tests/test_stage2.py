"""Boundary and sparse-overlap checks for the actual stage2 SQL."""
from pathlib import Path
import sys
import duckdb
WORK=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(WORK/'scripts'))

def run_tests():
 from stage2 import setup,band
 checks=[]
 def check(name,ok):
  if not ok:raise AssertionError(name)
  checks.append(name)
 with duckdb.connect(':memory:') as c:
  c.execute("ATTACH ':memory:' AS source")
  c.execute('CREATE TABLE source.fact_coupon_receipts(user_id VARCHAR,merchant_id VARCHAR,coupon_type VARCHAR,discount_rate VARCHAR,threshold DOUBLE,reduction DOUBLE,nominal_discount_fraction DOUBLE,distance_group VARCHAR,receipt_month VARCHAR,received_date DATE,mature15 BOOLEAN,redeemed15_mature BOOLEAN,lag_days INT)')
  rows=[('u1','m1','direct_discount','0.95',None,None,1-.95,'unknown','2016-01','2016-01-31',True,True,15),('u1','m1','direct_discount','0.95',None,None,1-.95,'unknown','2016-01','2016-01-31',True,False,16),('u2','m2','threshold_reduction','100:10',100,10,.1,'0','2016-06','2016-06-15',True,True,0),('u3','m2','threshold_reduction','100:10',100,10,.1,'10','2016-06','2016-06-15',True,False,None)]
  c.executemany('INSERT INTO source.fact_coupon_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',rows)
  c.execute('CREATE TABLE source.fact_offline_records(record_state VARCHAR,consumed_date DATE,is_coupon_receipt BOOLEAN)');c.execute("INSERT INTO source.fact_offline_records VALUES ('ordinary_consumption','2016-01-01',FALSE)")
  c.executemany('INSERT INTO source.fact_offline_records VALUES (?,?,?)',[('receipt','2016-02-15' if r[-1] is not None else None,True) for r in rows])
  # Reuse setup's actual view logic; source is already attached for synthetic fixture.
  c.execute('CREATE TABLE stage2_config(small_sample_receipts INT,same_day_monthly_max_day INT,comparison_min_receipts_each_stratum INT)');c.execute('INSERT INTO stage2_config VALUES (100,15,30)')
  # Extract only the actual CREATE VIEW text via a captured setup call to avoid mirrored implementation.
  class Capture:
   def __init__(self):self.calls=[]
   def execute(self,sql,args=None):self.calls.append((sql,args))
  cap=Capture();setup(cap,{'small_sample_receipts':100,'same_day_monthly_max_day':15,'comparison_min_receipts_each_stratum':30},'/unused-fixture')
  c.execute(cap.calls[-1][0])
  for sql in ['01_overview.sql','02_receipt_cohort.sql','03_coupon.sql','04_distance.sql','05_merchant.sql','06_redemption_lag.sql']:c.execute((WORK/'sql'/sql).read_text())
  check('inclusive day0 and day15; day16 excluded',c.execute('SELECT redeemed15_records FROM overview').fetchone()[0]==2)
  check('ordinary consumption excluded from receipt denominator',c.execute('SELECT receipt_records FROM overview').fetchone()[0]==4)
  check('repeat user counted distinctly',c.execute('SELECT receipt_users FROM overview').fetchone()[0]==3)
  check('month stays receipt month across consumption month',c.execute("SELECT redeemed15_records FROM receipt_cohort WHERE receipt_month='2016-01'").fetchone()[0]==1)
  check('June partial month marked',c.execute("SELECT period_scope FROM receipt_cohort WHERE receipt_month='2016-06'").fetchone()[0]=='partial_month_1_to_15')
  check('calendar-day1_to15 actually filters out Jan31',c.execute('SELECT count(*) FROM receipt_cohort_days1_15').fetchone()[0]==1)
  check('unknown distance category retained',c.execute("SELECT receipt_records FROM distance_metrics WHERE distance_group='unknown'").fetchone()[0]==2)
  check('float boundary0.95 remains le5pct',c.execute("SELECT nominal_discount_band FROM coupon_discount_band_metrics WHERE coupon_type='direct_discount'").fetchone()[0]=='le_5pct')
  check('lag includes empty day bins',c.execute('SELECT count(*) FROM redemption_lag').fetchone()[0]==16)
  check('day7 uses all receipts, not successes',c.execute('SELECT cumulative_redemption_rate15 FROM redemption_lag WHERE lag_day=7').fetchone()[0]==.25)
  check('day15 equals overview',c.execute('SELECT cumulative_redeemed15_records FROM redemption_lag WHERE lag_day=15').fetchone()[0]==2)
  c.execute('CREATE TABLE comparison_specs(comparison_id VARCHAR,dimension VARCHAR,group_a VARCHAR,group_b VARCHAR,controls VARCHAR,selection_reason VARCHAR)')
  c.execute("INSERT INTO comparison_specs VALUES ('no_type_overlap','coupon_type','direct_discount','threshold_reduction','test','test'),('sparse_distance','distance','0','10','test','test'),('both_absent','distance','99','98','test','test'),('one_absent','distance','0','99','test','test')")
  c.execute((WORK/'sql/07_comparable_review.sql').read_text())
  check('no common support does not manufacture zero-rate estimate',c.execute("SELECT included_strata,a_standardized_rate15,b_standardized_rate15 FROM comparison_summary WHERE comparison_id='no_type_overlap' AND support_rule='common_min1'").fetchone()==(0,None,None))
  check('no support preserves full population and records exclusions',c.execute("SELECT a_all_receipts,b_all_receipts,a_common_receipts,b_common_receipts FROM comparison_summary WHERE comparison_id='no_type_overlap' AND support_rule='common_min1'").fetchone()==(2,2,0,0))
  check('one common distance stratum has pooled denominator2',c.execute("SELECT included_strata,standard_weight_denominator,a_standardized_rate15,b_standardized_rate15 FROM comparison_summary WHERE comparison_id='sparse_distance' AND support_rule='common_min1'").fetchone()==(1,2,1.,0.))
  check('min30 sparse stratum excluded and empty results null',c.execute("SELECT included_strata,a_standardized_rate15 FROM comparison_summary WHERE comparison_id='sparse_distance' AND support_rule='common_min30_each'").fetchone()==(0,None))
  check('both_absent retained with zero counts and null rates',c.execute("SELECT count(*),min(a_all_receipts),min(b_all_receipts),max(a_all_rate15),max(b_all_rate15) FROM comparison_summary WHERE comparison_id='both_absent'").fetchone()==(2,0,0,None,None))
  check('one_absent returns zero count and null coverage for absent side',c.execute("SELECT a_all_receipts,b_all_receipts,a_coverage,b_coverage FROM comparison_summary WHERE comparison_id='one_absent' AND support_rule='common_min1'").fetchone()==(1,0,0.0,None))
  cap2=Capture();setup(cap2,{'small_sample_receipts':100,'same_day_monthly_max_day':15,'comparison_min_receipts_each_stratum':30,'nominal_discount_bands':{'custom_all':[0,1]}},'/unused-fixture')
  c.execute('DROP VIEW receipts');c.execute(cap2.calls[-1][0])
  check('configured nominal_discount_bands consumed by SQL and Python',set(r[0] for r in c.execute('SELECT DISTINCT nominal_discount_band FROM receipts').fetchall())=={'custom_all'} and band(0.2,{'custom_all':[0,1]})=='custom_all')
  bands={'low':[0,.075],'high':[.075,1]}
  cap3=Capture();setup(cap3,{'small_sample_receipts':100,'same_day_monthly_max_day':15,'comparison_min_receipts_each_stratum':30,'nominal_discount_bands':bands},'/unused-fixture')
  c.execute('DROP VIEW receipts');c.execute(cap3.calls[-1][0])
  c.execute('UPDATE source.fact_coupon_receipts SET nominal_discount_fraction=.073 WHERE user_id=\'u1\'')
  check('three-decimal threshold agrees SQL and independent Python',c.execute("SELECT DISTINCT nominal_discount_band FROM receipts WHERE user_id='u1'").fetchall()==[('low',)] and band(.073,bands)=='low' and band(.075,bands)=='low' and band(.075000001,bands)=='high')
  for bad in [{},{'a':[0,.2],'b':[.3,1]},{'a':[0,.8],'b':[.7,1]},{'a':[0,float('inf')]},{'a':[0,.9]}]:
   try:band(.1,bad)
   except ValueError:pass
   else:raise AssertionError('invalid bands accepted: '+str(bad))
  check('empty gapped overlapping nonfinite incomplete bands rejected',True)
 return {'passed':True,'checks':checks,'scope':'synthetic only; not mixed into results'}
if __name__=='__main__':print(run_tests())
