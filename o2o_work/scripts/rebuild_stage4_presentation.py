"""Rebuild only the native workbook, reusing verified stage1-3 and stage4 extracts.
This does not imply runtime / mouse acceptance. Never publishes to Tableau Public.
"""
from pathlib import Path
import json,xml.etree.ElementTree as E
from stage4_workbook import build_twb_tree, write_twbx, validate_twb_against_tableau_xsd
WORK=Path(__file__).resolve().parents[1]
def main():
    t=WORK/'tableau';q=WORK/'qa/stage4/merchant_coupon_fix';q.mkdir(exist_ok=True)
    latest=json.loads((t/'latest.json').read_text());m=json.loads((Path(latest['results_dir'])/'manifest.json').read_text());m['results_dir']=str(t)
    root,specs=build_twb_tree(m,t/'data',t/'data')
    E.ElementTree(root).write(t/'O2O三页分析看板.twb',encoding='utf-8',xml_declaration=True)
    write_twbx(t/'O2O三页分析看板.twbx',m,t/'data')
    for p in [t/'worksheet_specs.json',q/'worksheet_specs.json']:p.write_text(json.dumps(specs,ensure_ascii=False,indent=2)+'\n')
    v=validate_twb_against_tableau_xsd([t/'O2O三页分析看板.twb']);(q/'xsd_validation.json').write_text(json.dumps(v,ensure_ascii=False,indent=2));print(v)
def refresh_docs():
    import duckdb
    from datetime import datetime
    from stage4_docs import write_stage4_docs
    t=WORK/'tableau';q=WORK/'qa/stage4/merchant_coupon_fix'
    c=duckdb.connect(':memory:')
    def read(p):
        result=c.execute(f"SELECT * FROM read_csv_auto('{p}')")
        keys=[d[0] for d in result.description]
        return [dict(zip(keys,r)) for r in result.fetchall()]
    ov=read(t/'data/overview_charts.csv');may=read(t/'data/may_charts.csv')
    views={v:sorted([r for r in ov if r['scenario']=='全部' and r['view_id']==v],key=lambda r:r['category_order']) for v in ['month','lag','discount_band','scheme']}
    views.update({v:sorted([r for r in may if r['scenario']=='5月全部' and r['view_id']==v],key=lambda r:r['category_order']) for v in ['segment','candidate']})
    details=json.loads((t/'latest.json').read_text());details['completed_at']=datetime.now().astimezone().isoformat()
    write_stage4_docs(t,q,details,read(t/'filter_expected.csv'),json.loads((t/'worksheet_specs.json').read_text()),views)
    c.close()
if __name__=='__main__':
    main()
    refresh_docs()
