"""Contract regressions for merchant selection and coupon volume presentation.
These are static/data checks; they deliberately make no mouse/visual claims.
"""
from pathlib import Path
import csv,json,xml.etree.ElementTree as E,zipfile
WORK=Path(__file__).resolve().parents[1]
def run_tests():
    root=E.parse(WORK/'tableau/O2O三页分析看板.twb').getroot();checks=[]
    def check(name,ok):
        checks.append({'name':name,'passed':bool(ok)})
    check('24_worksheets_3_dashboards',len(root.findall('.//worksheet'))==24 and len(root.findall('.//dashboard'))==3)
    actions=root.find('actions')
    check('six_parameter_actions_no_legacy_brush',len(actions)==6 and not root.findall('.//command[@name="tsc:brush"]') and len(root.findall('.//edit-parameter-action'))==6)
    action=root.find('.//edit-parameter-action[@name="[Action_Merchant_Selection]"]')
    # Locate by target if action names are held on the parent action node.
    text=E.tostring(actions,encoding='unicode')
    check('merchant_action_uses_raw_category', 'SelectedMerchantID' in text and '[none:category:nk]' in text and 'Action_Merchant_Selection' in text)
    for scope in ['overview','may']:
        ds=root.find(f'./datasources/datasource[@name="federated.o2o_{scope}"]')
        formula=ds.find('column[@name="[scope_match]"]/calculation').get('formula')
        check(scope+'_selection_does_not_filter_scenario','SelectedMerchantID' not in formula)
        rate=ds.find('column[@name="[rate15]"]/calculation').get('formula')
        check(scope+'_ratio_of_sums','SUM([redeemed15_records])/SUM([receipt_records])' in rate)
    ws=root.find('.//worksheet[@name="商家·5月领券量与核销率"]')
    check('independent_selected_overlay',len([p for p in ws.findall('.//panes/pane') if p.get('y-axis-name')])==2 and 'selected_rate' in ws.find('.//rows').text)
    check('overlay_axes_synchronized',ws.find('.//encoding[@synchronized="true"]') is not None)
    dash=root.find('.//dashboard[@name="02 券方案与距离"]')
    names={z.get('name') for z in dash.findall('.//zone')}
    check('page2_dynamic_kpis_and_volume_views',{'领券记录','15日核销','总体核销率','券类型·领券记录数','券类型·15日核销记录数','券类型·15日核销率','券方案·领取规模与核销率'}.issubset(names))
    check('page2_retains_dimensions',{'满减门槛·核销率（标签=领券量）','名义优惠比例·核销率（标签=领券量）','距离编码·核销率（标签=领券量）','主流券方案与长尾分组·核销率'}.issubset(names))
    check('fixed_reference_caption_explicit',all(r.get('label','') in ('', '全期所有券基准 6.11%') for r in root.findall('.//reference-line') if abs(float(r.get('value','0'))-64395/1053282)<1e-9))
    rows=list(csv.DictReader((WORK/'tableau/data/may_charts.csv').open()))
    for view in ['merchant','candidate']:
        selected=[r for r in rows if r['scenario']=='5月全部' and r['view_id']==view and r['category']=='3381']
        check(view+'_exactly_one_3381_at_53_5',len(selected)==1 and int(selected[0]['receipt_records'])==53 and int(selected[0]['redeemed15_records'])==5)
    check('21_candidates_retained',sum(r['scenario']=='5月全部' and r['view_id']=='candidate' for r in rows)==21)
    sw=root.find('.//worksheet[@name="券方案·领取规模与核销率"]')
    check('scheme_selected_overlay',len([p for p in sw.findall('.//panes/pane') if p.get('y-axis-name')])==2 and 'selected_rate' in sw.find('.//rows').text)
    check('scheme_action_isolated', 'SelectedSchemeID' in text and 'Action_Scheme_Selection' in text)
    scene_actions=[a for a in actions if a.find('params/param[@name="target-parameter"]').get('value') in ('[Parameters].[OverviewScope]','[Parameters].[MayScope]')]
    check('four_numeric_scene_actions_reset_zero',len(scene_actions)==4 and all(a.find('clear-option').get('value')=='i:0' and 'action_scope_code' in a.find('params/param[@name="source-field"]').get('value') for a in scene_actions))
    check('right_segment_user_share_action',any(a.find('source').get('worksheet')=='历史分层·5月核销用户占比' for a in scene_actions))
    check('23_aliased_integer_scenes',sum(len(c.findall('members/member')) for c in root.findall('./datasources/datasource[@name="Parameters"]/column') if c.get('name') in ('[OverviewScope]','[MayScope]') and c.get('datatype')=='integer')==23)
    with zipfile.ZipFile(WORK/'tableau/O2O三页分析看板.twbx') as z:
        packaged=E.fromstring(z.read(next(n for n in z.namelist() if n.endswith('.twb'))))
        def signature(r):
            return sorted((a.get('name'),tuple(sorted((p.get('name'),p.get('value')) for p in a.findall('params/param'))),a.find('source').get('worksheet')) for a in r.find('actions'))
        check('package_matches_authoring_selection',signature(packaged)==signature(root))
    result={'status':'PASS' if all(x['passed'] for x in checks) else 'FAIL','checks_count':len(checks),'checks':checks,'verification_level':'static structure and actual exported data; no GUI assertions'}
    (WORK/'qa/stage4/merchant_coupon_fix/presentation_regression.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    if result['status']!='PASS':raise AssertionError(result)
    return result
if __name__=='__main__':print(json.dumps(run_tests(),ensure_ascii=False))
