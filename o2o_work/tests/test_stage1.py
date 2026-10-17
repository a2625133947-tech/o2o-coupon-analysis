"""Semantic contract tests; fixtures are never inserted into the production DB."""
from pathlib import Path
import sys
import math
import csv
import tempfile
import duckdb

WORK=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(WORK/'scripts'))

def run_tests():
    from stage1 import build_layers, import_raw, FIELDS
    rows=[
      ('001','01','null','null','','null','20160101'),
      ('002','01','1','0.9','null','20160131','20160215'),
      ('003','01','2','100:10','0','20160131','20160216'),
      ('004','01','3','20:5','10','20160615','20160615'),
      ('005','01','4','0.8','1','20160615','null'),
      ('005','01','4','0.8','1','20160615','null'),
      ('006','01','5','10:5','2','20160616','null'),
      ('007','01','6','100:10','3','20160102','20160101'),
      ('008','01','7','100:10','4','20160230','null'),
      ('009','01','8','garbage','11','20160101','null'),
      ('null','01','9','20:30','5','20160101','null'),
      ('010','01','10','0.95','0','null','20160101'),
      ('002','01','1','0.9','null','20160131','20160214'),
      ('011','01','11','0.5','null','2016011','null'),
      ('012','01','12','9'*400+':'+'9'*400,'0','20160101','null'),
      ('abc','01','13','0.9','0','20160101','null'),
      ('014','bad','14','0.9','0','20160101','null'),
      ('015','01','bad','0.9','0','20160101','null'),
    ]
    with duckdb.connect(':memory:') as con:
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'fixture.csv'
            with p.open('w',newline='') as f:
                writer=csv.writer(f);writer.writerow(FIELDS);writer.writerows(rows)
            import_raw(con,p,'fixture.csv')
        if con.execute('SELECT User_id FROM raw_offline WHERE source_row_number=1').fetchone()[0]!='001':
            raise AssertionError('CSV import failed to preserve original string ID')
        build_layers(con,{'history_start':'2016-01-01','training_observation_end':'2016-06-30','redemption_days_inclusive':[0,15]})
        def get(i):
            c=con.execute('SELECT * FROM fact_offline_records WHERE source_row_number=?',[i])
            return dict(zip([d[0] for d in c.description],c.fetchone()))
        names=[]
        def check(name,ok):
            if not ok:raise AssertionError(name)
            names.append(name)
        check('CSV import preserves empty and literal null strings',con.execute('SELECT Distance, Coupon_id FROM raw_offline WHERE source_row_number=1').fetchone()==('', 'null'))
        a=get(1);check('ordinary consumption excluded from receipts',not a['is_coupon_receipt'] and a['record_state']=='ordinary_consumption')
        a=get(2);check('cross-month inclusive day15 is redeemed',a['lag_days']==15 and a['redeemed_within15'])
        check('string ID preserves leading zeros',a['user_id']=='002')
        check('unknown distance remains unknown',a['distance_code'] is None and a['distance_group']=='unknown')
        check('0.9 means 10 percent nominal discount',math.isclose(a['nominal_discount_fraction'],0.1) and a['threshold'] is None)
        check('ISO weekday Sunday=7',a['receipt_weekday']==7)
        a=get(3);check('day16 consumed outside redemption numerator',a['lag_days']==16 and not a['redeemed_within15'] and a['record_state']=='consumed_after15')
        check('100:10 threshold and nominal fraction',a['threshold']==100 and a['reduction']==10 and math.isclose(a['nominal_discount_fraction'],0.1))
        a=get(4);check('day0 redeemed and last mature training receipt',a['lag_days']==0 and a['redeemed_within15'] and a['mature15'])
        check('distance10 stays raw code',a['distance_code']=='10')
        a=get(5);check('unredeemed receipt is valid with missing date',a['record_state']=='receipt_without_consumption' and not a['redeemed_within15'])
        a=get(6);check('duplicate retained with rank2 and original row5',a['exact_duplicate_rank']==2 and a['first_source_row_number']==5 and a['exact_duplicate_group_size']==2)
        check('dedup does not delete same receipt with different consumption date',con.execute('SELECT count(*) FROM fact_coupon_receipts_dedup WHERE user_id=\'002\'').fetchone()[0]==2)
        check('all raw rows retained',con.execute('SELECT count(*) FROM fact_offline_records').fetchone()[0]==len(rows))
        check('only exact duplicate removed in sensitivity',con.execute('SELECT count(*) FROM fact_offline_records_dedup').fetchone()[0]==len(rows)-1)
        check('June16 receipt is immature at June30 cutoff',not get(7)['mature15'])
        check('negative lag cannot redeem',get(8)['record_state']=='invalid_event' and not get(8)['redeemed_within15'])
        check('invalid calendar date is flagged not silently accepted',get(9)['invalid_received_date'] and not get(9)['is_coupon_receipt'])
        check('malformed date width is flagged',get(14)['invalid_received_date'])
        reasons=set(con.execute('SELECT source_row_number, reason FROM offline_anomalies').fetchall())
        check('invalid discount and distance are traceable',(10,'invalid_discount') in reasons and (10,'invalid_distance_code') in reasons)
        check('missing main ID and discount range are traceable',(11,'missing_user_id') in reasons and (11,'invalid_discount') in reasons)
        check('coupon without receipt date is traceable',(12,'coupon_receipt_mismatch') in reasons)
        check('normal missing date and unknown distance not anomalies',not any(row in (1,2,4,5,6) for row,reason in reasons))
        check('anomalous rows excluded from coupon receipts and marked invalid_event',all(not get(r)['is_coupon_receipt'] and get(r)['record_state']=='invalid_event' for r in (8,9,10,11,12,14,15,16,17,18)) and con.execute('SELECT count(*) FROM fact_coupon_receipts JOIN offline_anomalies USING (record_id)').fetchone()[0]==0)
        a=get(15);check('400-digit discount overflow is invalid_discount with null fraction and logged',a['coupon_type']=='invalid_discount' and a['nominal_discount_fraction'] is None and (15,'invalid_discount') in reasons)
        check('invalid numeric ID formats excluded with traceable reasons',all(not get(r)['is_coupon_receipt'] and (r,reason) in reasons for r,reason in [(16,'invalid_user_id'),(17,'invalid_merchant_id'),(18,'invalid_coupon_id')]))
    return {'passed':True,'fixture_records':len(rows),'checks':names,'scope':'synthetic contract tests, not real dataset metrics'}

if __name__=='__main__':
    import json
    print(json.dumps(run_tests(),ensure_ascii=False,indent=2))
