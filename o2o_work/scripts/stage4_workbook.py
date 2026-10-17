"""Author native Tableau workbooks (.twb and .twbx) with Hyper extracts, Reference Lines, Log Axis, Dashboard Actions, and XSD validation."""
from pathlib import Path
import copy
import csv
import json
import subprocess
import sys
import uuid
import xml.etree.ElementTree as E
import zipfile
from stage4_dependencies import resolve_tableau_dependencies

WORK = Path(__file__).resolve().parents[1]


def el(parent, tag, text=None, **attrs):
    e = E.SubElement(parent, tag, {k.replace('_', '-'): str(v) for k, v in attrs.items()})
    e.text = text
    return e


def build_twb_tree(manifest, data_dir, hyper_dir=None):
    """Build a schema-compliant Tableau 18.1 (2026.2) workbook ElementTree."""
    data_dir = str(data_dir)
    hyper_dir = str(hyper_dir) if hyper_dir is not None else data_dir
    proto = E.parse(resolve_tableau_dependencies()['probe']).getroot()
    root = E.Element(
        'workbook',
        {
            'original-version': '18.1',
            'version': '18.1',
            'source-platform': 'mac',
            'source-build': '2026.2.3 (20262.26.0912.1023)',
            'xmlns:user': 'http://www.tableausoftware.com/xml/user',
        },
    )
    mf = el(root, 'document-format-change-manifest')
    flags = sorted(
        {
            n.tag
            for n in proto.find('document-format-change-manifest')
            if 'Fingerprint' not in n.tag
        }
        | {'ParameterAction', 'ParameterActionClearSelection', 'SetMembershipControl', 'SortTagCleanup'}
    )
    for tag in flags:
        el(mf, tag)

    prefs = el(root, 'preferences')
    el(prefs, 'preference', name='ui.encoding.shelf.height', value=24)
    el(prefs, 'preference', name='ui.shelf.height', value=26)

    sources = el(root, 'datasources')
    params = el(
        sources,
        'datasource',
        name='Parameters',
        inline='true',
        version='18.1',
        hasconnection='false',
    )
    el(params, 'aliases', enabled='yes')
    param_cols = {}
    overview_extra_members = ['2016-01', '2016-02', '2016-03', '2016-04', '2016-05', '2016-06（1—15日）', '3381']
    may_extra_members = ['3381']
    scope_codes = {scope:{c[0]:i for i,c in enumerate(manifest['scenario_contract'][scope])} for scope in ('overview','may')}
    for name, caption, choices, extras in [
        ('OverviewScope', '场景1、2页', manifest['scenario_contract']['overview'], overview_extra_members),
        ('MayScope', '5月场景', manifest['scenario_contract']['may'], may_extra_members),
        ('SelectedMerchantID', '选中商家ID', [('', '')], []),
        ('SelectedSchemeID', '选中券方案', [('', '')], []),
    ]:
        numeric = name in ('OverviewScope','MayScope')
        default = '0' if numeric else '"' + choices[0][0] + '"'
        col = el(
            params,
            'column',
            caption=caption,
            datatype='integer' if numeric else 'string',
            name=f'[{name}]',
            param_domain_type='any' if name in ('SelectedMerchantID', 'SelectedSchemeID') else 'list',
            role='measure',
            type='quantitative' if numeric else 'nominal',
            value=default,
        )
        calc = el(col, 'calculation', formula=default)
        calc.set('class', 'tableau')
        if numeric:
            aliases = el(col, 'aliases')
            for i, c in enumerate(choices):
                el(aliases, 'alias', key=i, value=c[0])
            members = el(col, 'members')
            for i,c in enumerate(choices): el(members, 'member', value=i, alias=c[0])
            param_cols[name] = col
            continue
        # An unrestricted string parameter has no enumerated domain.
        if name in ('SelectedMerchantID', 'SelectedSchemeID'):
            param_cols[name] = col
            continue
        members = el(col, 'members')
        seen_m = set()
        for label in [c[0] for c in choices] + extras:
            if label not in seen_m:
                seen_m.add(label)
                el(members, 'member', value='"' + label + '"')
        param_cols[name] = col

    dtype = {
        'scenario': 'string',
        'view_id': 'string',
        'category': 'string',
        'sample_note': 'string',
        'category_order': 'integer',
        'receipt_records': 'integer',
        'redeemed15_records': 'integer',
        'receipt_users': 'integer',
        'redeemed15_users': 'integer',
        'cumulative_redeemed': 'integer',
        'successful_receipts': 'integer',
    }
    captions = {
        'category': '类别',
        'receipt_records': '领券记录数',
        'redeemed15_records': '15日核销记录数',
        'receipt_users': '领券用户数',
        'redeemed15_users': '核销用户数',
        'cumulative_redeemed': '累计核销记录数',
        'successful_receipts': '15日成功核销总数',
        'sample_note': '样本与口径提示',
        'scenario': '合法筛选范围',
        'category_order': '类别顺序',
        'view_id': '视图粒度',
    }
    dscols = {}
    dsnames = {}
    actual_csv_dir = Path(manifest['results_dir']) / 'data'

    for scope, param in [('overview', 'OverviewScope'), ('may', 'MayScope')]:
        name = 'federated.o2o_' + scope
        dsnames[scope] = name
        ds = el(
            sources,
            'datasource',
            caption='线下总体与券分析' if scope == 'overview' else '固定5月用户与商家',
            inline='true',
            name=name,
            version='18.1',
        )
        oid = f'{scope}_charts.csv_' + uuid.uuid5(uuid.NAMESPACE_DNS, f'o2o.{scope}.charts').hex.upper()
        connname = 'textscan.o2o_' + scope
        connection = el(ds, 'connection')
        connection.set('class', 'federated')
        ncs = el(connection, 'named-connections')
        nc = el(ncs, 'named-connection', caption=scope + '_charts', name=connname)
        c = el(
            nc,
            'connection',
            directory=data_dir,
            filename=scope + '_charts.csv',
            password='',
            server='',
        )
        c.set('class', 'textscan')

        with (actual_csv_dir / f'{scope}_charts.csv').open(encoding='utf-8') as f:
            fields = next(csv.reader(f))

        rel = el(
            connection,
            'relation',
            connection=connname,
            name=scope + '_charts.csv',
            table=f'[{scope}_charts#csv]',
            type='table',
        )
        cols = el(rel, 'columns', character_set='UTF-8', header='yes', locale='zh_CN', separator=',')
        for i, field in enumerate(fields):
            el(cols, 'column', datatype=dtype[field], name=field, ordinal=i)

        metadata = el(connection, 'metadata-records')
        cap = el(metadata, 'metadata-record')
        cap.set('class', 'capability')
        el(cap, 'remote-name')
        el(cap, 'remote-type', '0')
        el(cap, 'parent-name', f'[{scope}_charts.csv]')
        el(cap, 'remote-alias')
        el(cap, 'aggregation', 'Count')
        el(cap, 'contains-null', 'true')
        attrs = el(cap, 'attributes')
        for k_attr, v_attr in [
            ('character-set', '"UTF-8"'),
            ('collation', '"zh_Hans_CN"'),
            ('currency', '"¥"'),
            ('field-delimiter', '","'),
            ('header-row', '"true"'),
            ('locale', '"zh_CN"'),
            ('single-char', '""'),
        ]:
            el(attrs, 'attribute', v_attr, datatype='string', name=k_attr)

        for i, field in enumerate(fields):
            typ = dtype[field]
            rec = el(metadata, 'metadata-record')
            rec.set('class', 'column')
            for tag, text in [
                ('remote-name', field),
                ('remote-type', '129' if typ == 'string' else '20'),
                ('local-name', f'[{field}]'),
                ('parent-name', f'[{scope}_charts.csv]'),
                ('remote-alias', field),
                ('ordinal', str(i)),
                ('local-type', typ),
                ('aggregation', 'Count' if typ == 'string' else 'Sum'),
                ('contains-null', 'true'),
            ]:
                el(rec, tag, text)
            if typ == 'string':
                el(rec, 'collation', flag='0', name='LZH_RCN')
            el(rec, 'object-id', f'[{oid}]')

        el(ds, 'aliases', enabled='yes')
        el(
            ds,
            'column',
            caption=f'{scope}_charts.csv',
            datatype='table',
            name=f'[__tableau_internal_object_id__].[{oid}]',
            role='measure',
            type='quantitative',
        )
        columns = {}
        for field in fields:
            typ = dtype[field]
            col = el(
                ds,
                'column',
                caption=captions[field],
                datatype=typ,
                name=f'[{field}]',
                role='dimension' if typ == 'string' else 'measure',
                type='nominal' if typ == 'string' else 'quantitative',
            )
            if typ == 'integer':
                col.set('default-format', 'n#,##0')
            columns[field] = col

        if scope == 'overview':
            aliases = el(columns['category'], 'aliases')
            for raw, short in [('其余达标方案（≥100条合并）','其余达标(≥100条)'), ('低样本方案汇总（<100条合并）','低样本(<100条)')]:
                el(aliases, 'alias', key='"'+raw+'"', value=short)

        code_map = scope_codes[scope]
        def case_code(field, mapping):
            return 'CASE ['+field+'] ' + ' '.join('WHEN "'+label+'" THEN '+str(code) for label,code in mapping.items()) + ' ELSE -1 END'
        scope_formula = '('+case_code('scenario',code_map)+') = [Parameters].['+param+']'
        action_map = dict(code_map)
        if scope == 'overview':
            action_map.update({f'2016-{i:02d}':code_map[f'{i}月'] for i in range(1,7)})
            action_map['2016-06（1—15日）']=code_map['6月']
        action_code_formula = case_code('category', action_map)

        selection_param = 'SelectedMerchantID' if scope == 'may' else 'SelectedSchemeID'
        for field, caption, formula, typ, role in [
            ('action_scope_code','场景动作编号',action_code_formula,'integer','dimension'),
            ('merchant_selection', '商家选择状态', f'IF [Parameters].[{selection_param}] = "" THEN "全部商家" ELSEIF [category] = [Parameters].[{selection_param}] THEN "选中商家" ELSE "其他商家" END', 'string', 'dimension'),
            ('selected_rate', '选中商家核销率', f'IF MIN([category]) = [Parameters].[{selection_param}] AND SUM([receipt_records]) > 0 THEN SUM([redeemed15_records])/SUM([receipt_records]) END', 'real', 'measure'),
            ('selected_label', '选中商家标签', f'IF [category] = [Parameters].[{selection_param}] THEN [category] ELSE "" END', 'string', 'dimension'),
            ('selection_size', '选中标记大小', f'MAX(IF [category] = [Parameters].[{selection_param}] AND [Parameters].[{selection_param}] <> "" THEN 9 ELSE 1 END)', 'real', 'measure'),
            ('merchant_id', '商家ID', 'IF [view_id] = "merchant" OR [view_id] = "candidate" THEN [category] END', 'string', 'dimension'),
            (
                'rate15',
                '15日核销率',
                'IF SUM([receipt_records]) > 0 THEN SUM([redeemed15_records])/SUM([receipt_records]) END',
                'real',
                'measure',
            ),
            (
                'user_share',
                '15日核销用户占比',
                'IF SUM([receipt_users]) > 0 THEN SUM([redeemed15_users])/SUM([receipt_users]) END',
                'real',
                'measure',
            ),
            (
                'cumulative_rate',
                '累计15日核销率',
                'IF SUM([receipt_records]) > 0 THEN SUM([cumulative_redeemed])/SUM([receipt_records]) END',
                'real',
                'measure',
            ),
            (
                'share_of_successful_redemptions',
                '占15日成功核销比例',
                'IF SUM([successful_receipts]) > 0 THEN SUM([redeemed15_records])/SUM([successful_receipts]) END',
                'real',
                'measure',
            ),
            (
                'scope_match',
                '当前筛选范围',
                scope_formula,
                'boolean',
                'dimension',
            ),
        ]:
            col = el(
                ds,
                'column',
                caption=caption,
                datatype=typ,
                name=f'[{field}]',
                role=role,
                type='quantitative' if role == 'measure' else ('ordinal' if typ == 'integer' else 'nominal'),
            )
            calc = el(col, 'calculation', formula=formula)
            calc.set('class', 'tableau')
            if role == 'measure':
                col.set('default-format', 'n#,##0' if field == 'selection_size' else 'p0.00%')
            columns[field] = col

        el(ds, 'column-instance', column='[merchant_selection]', derivation='None', name='[none:merchant_selection:nk]', pivot='key', type='nominal')
        ext = el(
            ds,
            'extract',
            count='-1',
            enabled='true',
            object_id='',
            units='records',
            user_specific='false',
        )
        econn = el(
            ext,
            'connection',
            access_mode='readonly',
            author_locale='zh_CN',
            dbname=f'{hyper_dir}/{scope}_charts.hyper',
            default_settings='hyper',
            schema='Extract',
            sslmode='',
            tablename='Extract',
            update_time='10/15/2026 08:00:00 AM',
            username='tableau_internal_user',
        )
        econn.set('class', 'hyper')
        el(econn, 'relation', name='Extract', table='[Extract].[Extract]', type='table')
        emeta = el(econn, 'metadata-records')
        for i, field in enumerate(fields):
            typ = dtype[field]
            rec = el(emeta, 'metadata-record')
            rec.set('class', 'column')
            for tag, text in [
                ('remote-name', field),
                ('remote-type', '129' if typ == 'string' else '20'),
                ('local-name', f'[{field}]'),
                ('parent-name', '[Extract]'),
                ('remote-alias', field),
                ('ordinal', str(i)),
                ('family', f'{scope}_charts.csv'),
                ('local-type', typ),
                ('aggregation', 'Count' if typ == 'string' else 'Sum'),
                ('contains-null', 'true'),
            ]:
                el(rec, tag, text)
            if typ == 'string':
                el(rec, 'collation', flag='0', name='LZH_RCN')
            el(rec, 'object-id', f'[{oid}]')

        el(ds, 'layout', dim_ordering='alphabetic', measure_ordering='alphabetic', show_structure='true')
        # Tableau writes categorical palettes on the datasource, with unqualified
        # instance names. Pane palettes were accepted but painted every mark alike.
        ds_mark = el(el(ds, 'style'), 'style-rule', element='mark')
        palette = el(ds_mark, 'encoding', attr='color', field='[none:merchant_selection:nk]', type='palette')
        for label, color in [('全部商家','#3266a8'), ('其他商家','#c8cdd3'), ('选中商家','#e86b18')]:
            el(el(palette, 'map', to=color), 'bucket', '"'+label+'"')

        sv = el(ds, 'semantic-values')
        el(sv, 'semantic-value', key='[Country].[Name]', value='"中国"')
        el(ds, 'date-options', start_of_week='monday')

        graph = el(ds, 'object-graph')
        objs = el(graph, 'objects')
        obj = el(objs, 'object', caption=scope + '_charts.csv', id=oid)
        props = el(obj, 'properties', context='')
        rel2 = el(
            props,
            'relation',
            connection=connname,
            name=scope + '_charts.csv',
            table=f'[{scope}_charts#csv]',
            type='table',
        )
        cols2 = el(rel2, 'columns', character_set='UTF-8', header='yes', locale='zh_CN', separator=',')
        for i, field in enumerate(fields):
            el(cols2, 'column', datatype=dtype[field], name=field, ordinal=i)
        props_ext = el(obj, 'properties', context='extract')
        el(props_ext, 'relation', name='Extract', table='[Extract].[Extract]', type='table')

        dscols[scope] = columns

    # Insert <actions> after <datasources> and before <worksheets> per WorkbookFile-CT XSD
    actions_el = el(root, 'actions')
    action_specs = []
    # A parameter action consumes the actual category ID, avoiding the legacy
    # tsc:brush selector on a derived field (observed empty selector / all grey).
    for pact_name, pact_caption, dash_name, ws_name, ds_key, target_param, reset_val in [
        (
            '[Action_Scheme_Selection]',
            '选中券方案：仅改变散点，取消选择清空',
            '02 券方案与距离',
            '主流券方案与长尾分组·核销率',
            'overview',
            '[Parameters].[SelectedSchemeID]',
            's::',
        ),
        (
            '[Action_Merchant_Selection]',
            '选中商家：仅改变散点颜色大小标签，取消选择清空',
            '03 用户与商家诊断',
            '候选商家（前20+3381）·核销率',
            'may',
            '[Parameters].[SelectedMerchantID]',
            's::',
        ),
        (
            '[Action4_Param_Month]',
            '点击月份切换第1、2页；取消选择恢复“全部”',
            '01 总体与领券月份',
            '领券月份·核销率',
            'overview',
            '[Parameters].[OverviewScope]',
            'i:0',
        ),
        (
            '[Action5_Param_CouponType]',
            '点击券类型切换第1、2页；取消选择恢复“全部”',
            '02 券方案与距离',
            '券类型·15日核销率',
            'overview',
            '[Parameters].[OverviewScope]',
            'i:0',
        ),
        (
            '[Action7_Param_SegmentUserShare]',
            '点击用户占比分层切换第3页，取消选择复位',
            '03 用户与商家诊断',
            '历史分层·5月核销用户占比',
            'may',
            '[Parameters].[MayScope]',
            'i:0',
        ),
        (
            '[Action6_Param_Segment]',
            '点击用户分层切换第3页；取消选择恢复“5月全部”',
            '03 用户与商家诊断',
            '历史分层·5月记录核销率',
            'may',
            '[Parameters].[MayScope]',
            'i:0',
        ),
    ]:
        pa = el(actions_el, 'edit-parameter-action', caption=pact_caption, name=pact_name)
        el(pa, 'activation', type='on-select')
        el(pa, 'source', dashboard=dash_name, type='sheet', worksheet=ws_name)
        el(pa, 'agg-type', type='attr')
        el(pa, 'clear-option', type='assign-fixed-value', value=reset_val)
        p_node = el(pa, 'params')
        el(p_node, 'param', name='source-field', value=f'[{dsnames[ds_key]}].[none:' + ('action_scope_code' if target_param in ('[Parameters].[OverviewScope]', '[Parameters].[MayScope]') else 'category') + ':nk]')
        el(p_node, 'param', name='target-parameter', value=target_param)
        action_specs.append(
            {
                'name': pact_name,
                'caption': pact_caption,
                'type': 'edit-parameter-action',
                'dashboard': dash_name,
                'source_worksheet': ws_name,
                'target_parameter': target_param,
                'clear_value': reset_val,
            }
        )

    sheets = el(root, 'worksheets')
    specs = []

    def worksheet(
        name,
        scope,
        view,
        measure='rate15',
        kind='Bar',
        dim=True,
        axis='horizontal',
        label_field=None,
        detail=False,
        log_x=False,
        reflines=None,
        formula_desc='SUM(redeemed15_records)/SUM(receipt_records)',
    ):
        ds = dsnames[scope]
        columns = dscols[scope]
        param = 'OverviewScope' if scope == 'overview' else 'MayScope'
        ws = el(sheets, 'worksheet', name=name)
        lo = el(ws, 'layout-options')
        ft = el(el(lo, 'title'), 'formatted-text')
        el(ft, 'run', name, fontsize=11)
        tab = el(ws, 'table')
        v = el(tab, 'view')
        dss = el(v, 'datasources')
        el(dss, 'datasource', name=ds)
        el(dss, 'datasource', name='Parameters')
        dep = el(v, 'datasource-dependencies', datasource=ds)
        for col in columns.values():
            dep.append(copy.deepcopy(col))
        refs = {}
        for f, col in columns.items():
            isdim = col.get('role') == 'dimension'
            der = 'None' if isdim else ('User' if col.find('calculation') is not None else 'Sum')
            prefix = {'None': 'none', 'User': 'usr', 'Sum': 'sum'}[der]
            typ = 'nk' if isdim else 'qk'
            inst = f'[{prefix}:{f}:{typ}]'
            refs[f] = f'[{ds}].{inst}'
            el(
                dep,
                'column-instance',
                column=f'[{f}]',
                derivation=der,
                name=inst,
                pivot='key',
                type=('ordinal' if col.get('datatype') == 'integer' else 'nominal') if isdim else 'quantitative',
            )
        pd = el(v, 'datasource-dependencies', datasource='Parameters')
        pd.append(copy.deepcopy(param_cols[param]))
        pd.append(copy.deepcopy(param_cols['SelectedMerchantID' if scope == 'may' else 'SelectedSchemeID']))
        for field, value in [('view_id', '"' + view + '"'), ('scope_match', 'true')]:
            fil = el(v, 'filter', column=refs[field])
            fil.set('class', 'categorical')
            el(fil, 'groupfilter', function='member', level=refs[field].split('].', 1)[1], member=value)
        if view in ('merchant', 'scheme') and detail:
            el(v, 'alphabetic-sort', column=refs['merchant_selection'], direction='ASC')
        if dim:
            el(v, 'computed-sort', column=refs['category'], direction='ASC', using=refs['category_order'])
        slices = el(v, 'slices')
        for field in ['view_id', 'scope_match']:
            el(slices, 'column', refs[field])
        el(v, 'aggregation', value='true')

        style = el(tab, 'style')
        rule = el(style, 'style-rule', element='worksheet')
        el(rule, 'format', attr='display-field-labels', scope='rows', value='false')
        el(rule, 'format', attr='display-field-labels', scope='cols', value='false')
        rule = el(style, 'style-rule', element='cell')
        el(rule, 'format', attr='font-family', value='PingFang SC')
        el(rule, 'format', attr='font-size', value=10)
        if log_x:
            ax_rule = el(style, 'style-rule', element='axis')
            enc_ax = el(
                ax_rule,
                'encoding',
                attr='space',
                field=refs['receipt_records'],
                field_type='quantitative',
                scale='log',
                scope='cols',
                type='space',
            )
            enc_ax.set('class', '0')
        if reflines:
            ref_rule = el(style, 'style-rule', element='refline')
            for idx_r, rspec in enumerate(reflines):
                rid = f'refline{idx_r}'
                el(ref_rule, 'format', attr='fill-above', id=rid, value='#00000000')
                el(ref_rule, 'format', attr='fill-below', id=rid, value='#00000000')
                el(ref_rule, 'format', attr='stroke-color', id=rid, value=rspec.get('color', '#c85200'))
                el(ref_rule, 'format', attr='stroke-size', id=rid, value='1')
                el(ref_rule, 'format', attr='line-pattern-only', id=rid, value='dashed')

        panes = el(tab, 'panes')
        pane = el(panes, 'pane', selection_relaxation_option='selection-relaxation-allow')
        el(el(pane, 'view'), 'breakdown', value='auto')
        mark = el(pane, 'mark')
        mark.set('class', kind)
        enc = el(pane, 'encodings')

        lod_fields = [
            'receipt_records',
            'redeemed15_records',
            'receipt_users',
            'redeemed15_users',
            'rate15',
            'user_share',
            'sample_note',
        ]
        if view == 'lag':
            lod_fields.extend(['cumulative_redeemed', 'successful_receipts', 'cumulative_rate', 'share_of_successful_redemptions'])
        if view in ('merchant', 'candidate'):
            lod_fields.append('merchant_id')
        if name in ('领券月份·核销率','券类型·15日核销率','历史分层·5月记录核销率','历史分层·5月核销用户占比'): lod_fields.append('action_scope_code')
        seen_lod = set()
        for f in lod_fields:
            if f not in seen_lod:
                seen_lod.add(f)
                el(enc, 'lod', column=refs[f])

        if view in ('merchant', 'scheme') and detail:
            el(enc, 'color', column=refs['merchant_selection'])
            el(enc, 'text', column=refs['selected_label'])
        if not dim and not detail:
            el(enc, 'text', column=refs[measure])
        if detail:
            el(enc, 'lod', column=refs['category'])
        if label_field:
            el(enc, 'text', column=refs[label_field])

        # Reference lines go after <encodings> and before <customized-tooltip> per PaneSpecification-G XSD
        if reflines:
            for idx_r, rspec in enumerate(reflines):
                rid = f'refline{idx_r}'
                ax_col = refs[rspec['field']]
                el(
                    pane,
                    'reference-line',
                    axis_column=ax_col,
                    enable_instant_analytics='true',
                    formula='constant',
                    id=rid,
                    label='' if view in ('segment', 'candidate', 'scheme') else rspec['label'],
                    label_type='custom',
                    scope='per-table',
                    value=str(rspec['value']),
                    value_column=ax_col,
                    z_order=str(idx_r + 1),
                )

        # IMPORTANT: XSD PaneSpecification-G requires customized-tooltip BEFORE customized-label!
        tt = el(el(pane, 'customized-tooltip'), 'formatted-text')
        if view == 'lag' and measure == 'redeemed15_records':
            tt_text = (
                '等待天数（lag_days）：<' + refs['category'] + '>\n'
                '当日15日核销记录数（分子）：<' + refs['redeemed15_records'] + '>\n'
                '15日成功核销总数（等待分布分母）：<' + refs['successful_receipts'] + '>\n'
                '占15日成功核销比例：<' + refs['share_of_successful_redemptions'] + '>\n'
                '成熟领券记录总数：<' + refs['receipt_records'] + '>\n'
                '当日核销/成熟领券：<' + refs['rate15'] + '>\n'
                '截至当日累计核销记录数：<' + refs['cumulative_redeemed'] + '>\n'
                '<' + refs['sample_note'] + '>'
            )
        elif view == 'lag' and measure == 'cumulative_rate':
            tt_text = (
                '等待天数（lag_days）：<' + refs['category'] + '>\n'
                '截至当日累计核销记录数（分子）：<' + refs['cumulative_redeemed'] + '>\n'
                '成熟领券记录总数（分母）：<' + refs['receipt_records'] + '>\n'
                '累计15日核销率：<' + refs['cumulative_rate'] + '>\n'
                '当日新增核销记录数：<' + refs['redeemed15_records'] + '>\n'
                '15日成功核销总数：<' + refs['successful_receipts'] + '>\n'
                '<' + refs['sample_note'] + '>'
            )
        elif view == 'segment' and measure == 'user_share':
            tt_text = (
                '<' + refs['category'] + '>\n'
                '5月核销用户数（分子）：<' + refs['redeemed15_users'] + '>\n'
                '5月领券用户数（分母）：<' + refs['receipt_users'] + '>\n'
                '15日核销用户占比：<' + refs['user_share'] + '>\n'
                '5月15日核销记录数：<' + refs['redeemed15_records'] + '>\n'
                '5月成熟领券记录数：<' + refs['receipt_records'] + '>\n'
                '15日记录核销率：<' + refs['rate15'] + '>\n'
                '<' + refs['sample_note'] + '>'
            )
        else:
            tt_text = (
                ('<' + refs['category'] + '>\n' if (dim or detail) else '')
                + '领券记录数（分母）：<' + refs['receipt_records'] + '>\n'
                + '15日核销记录数（分子）：<' + refs['redeemed15_records'] + '>\n'
                + '15日核销率：<' + refs['rate15'] + '>\n'
                + '领券独立用户数：<' + refs['receipt_users'] + '>\n'
                + '15日核销独立用户数：<' + refs['redeemed15_users'] + '>\n'
                + '15日核销用户占比：<' + refs['user_share'] + '>\n'
                + '<' + refs['sample_note'] + '>'
            )
        el(tt, 'run', tt_text)

        if view in ('merchant', 'scheme') and detail:
            lab = el(el(pane, 'customized-label'), 'formatted-text')
            el(lab, 'run', '<' + refs['selected_label'] + '>', fontsize=12, bold='true', fontcolor='#a34400')
        if (not dim and not detail) or label_field:
            lab = el(el(pane, 'customized-label'), 'formatted-text')
            target_label = measure if (not dim and not detail) else label_field
            el(lab, 'run', '<' + refs[target_label] + '>', fontsize=15 if not dim else 8)

        pr = el(el(pane, 'style'), 'style-rule', element='mark')
        if view != 'merchant':
            el(pr, 'format', attr='color', value='#3266a8')
        el(
            pr,
            'format',
            attr='mark-labels-show',
            value='true' if label_field or (view == 'merchant' and detail) or (not dim and not detail) else 'false',
        )
        if detail:
            el(pr, 'format', attr='size', value='1.0' if view == 'merchant' else '0.48')
            el(pr, 'format', attr='mark-transparency', value='255' if view == 'merchant' else '170')
            el(pr, 'format', attr='has-stroke', value='true')
            el(pr, 'format', attr='stroke-color', value='#ffffff')
            if view == 'merchant':
                el(pr, 'format', attr='mark-labels-cull', value='false')
        else:
            el(pr, 'format', attr='size', value='0.5')

        if detail:
            r = refs['rate15']
            co = refs['receipt_records']
        elif dim:
            r = refs['category'] if axis == 'horizontal' else refs[measure]
            co = refs[measure] if axis == 'horizontal' else refs['category']
        else:
            r = ''
            co = ''
        if view in ('merchant', 'scheme') and detail:
            # Second synchronized axis paints the selected merchant after the full
            # distribution, so dense low-volume marks cannot cover the selection.
            pane.set('id', '1')
            pane.set('y-axis-name', refs['rate15'])
            overlay = copy.deepcopy(pane)
            overlay.set('id', '2')
            overlay.set('y-axis-name', refs['selected_rate'])
            for ref in overlay.findall('reference-line'):
                overlay.remove(ref)
            overlay_enc = overlay.find('encodings')
            for channel in list(overlay_enc):
                if channel.tag == 'size':
                    overlay_enc.remove(channel)
            overlay_style = overlay.find('style')
            overlay.remove(overlay_style)
            om = el(el(overlay, 'style'), 'style-rule', element='mark')
            for attr, value in [('color','#e86b18'),('size','1.1'),('mark-transparency','255'),('mark-labels-show','true'),('mark-labels-cull','false'),('has-stroke','true'),('stroke-color','#a34400')]:
                el(om,'format',attr=attr,value=value)
            panes.append(overlay)
            for f in pr.findall('format'):
                if f.get('attr') == 'size': f.set('value','0.36')
                if f.get('attr') == 'mark-labels-show': f.set('value','false')
            wr = el(style,'style-rule',element='worksheet')
            el(wr,'format',attr='show-null-value-warning',value='false')
            ar = el(style,'style-rule',element='axis')
            ae = el(ar,'encoding',attr='space',field=refs['selected_rate'],field_type='quantitative',fold='true',scope='rows',synchronized='true',type='space')
            ae.set('class','0')
            el(ar,'format',attr='display',field=refs['selected_rate'],scope='rows',value='false')
            r = '(' + refs['rate15'] + ' + ' + refs['selected_rate'] + ')'
        el(tab, 'rows', r)
        el(tab, 'cols', co)
        el(ws, 'simple-id', uuid='{' + str(uuid.uuid5(uuid.NAMESPACE_DNS, f'ws.{name}')).upper() + '}')

        specs.append(
            {
                'sheet': name,
                'datasource': f'{scope}_charts.csv / {scope}_charts.hyper',
                'scope': scope,
                'view_id': view,
                'measure': measure,
                'mark': kind,
                'dimension': 'category' if dim else ('category (detail)' if detail else None),
                'axis': axis + (' (log X)' if log_x else ''),
                'label': label_field if label_field else (measure if not dim and not detail else None),
                'filter': f'view_id="{view}" AND scope_match=True ({param})',
                'sort': 'category_order ASC' if dim else None,
                'reference_lines': [r['label'] for r in (reflines or [])],
                'rate_formula': formula_desc,
            }
        )
        return name

    k1 = [
        worksheet(n, 'overview', 'kpi', f, kind='Text', dim=False, formula_desc=fd)
        for n, f, fd in [
            ('领券记录', 'receipt_records', 'SUM(receipt_records)'),
            ('领券用户', 'receipt_users', 'SUM(receipt_users)（按场景精确去重预计算，禁止跨组相加）'),
            ('15日核销', 'redeemed15_records', 'SUM(redeemed15_records)'),
            ('总体核销率', 'rate15', 'IF SUM(receipt_records)>0 THEN SUM(redeemed15_records)/SUM(receipt_records) END'),
        ]
    ]
    monthv = worksheet(
        '领券月份·记录量',
        'overview',
        'month',
        'receipt_records',
        axis='vertical',
        label_field='receipt_records',
        formula_desc='SUM(receipt_records)（按领券月份归属；6月标为1—15日部分月份）',
    )
    monthr = worksheet(
        '领券月份·核销率',
        'overview',
        'month',
        'rate15',
        axis='vertical',
        label_field='rate15',
        reflines=[{'field': 'rate15', 'value': 0.06113747315533732, 'label': '全期所有券基准 6.11%', 'color': '#c85200'}],
        formula_desc='IF SUM(receipt_records)>0 THEN SUM(redeemed15_records)/SUM(receipt_records) END',
    )
    lag = worksheet(
        '核销等待天数·成功记录',
        'overview',
        'lag',
        'redeemed15_records',
        axis='vertical',
        formula_desc='柱高=SUM(redeemed15_records)；等待分布占比=SUM(redeemed15_records)/SUM(successful_receipts)（分母仅限15日成功核销记录）',
    )
    cum = worksheet(
        '第0—15日·累计核销率',
        'overview',
        'lag',
        'cumulative_rate',
        kind='Line',
        axis='vertical',
        formula_desc='IF SUM(receipt_records)>0 THEN SUM(cumulative_redeemed)/SUM(receipt_records) END（分母为成熟领券记录总数；第15日对齐总体核销率）',
    )
    types = worksheet(
        '券类型·15日核销率',
        'overview',
        'coupon_type',
        'rate15',
        label_field='rate15',
    )
    type_volume = worksheet('券类型·领券记录数', 'overview', 'coupon_type', 'receipt_records', label_field='receipt_records', formula_desc='SUM(receipt_records)')
    type_redeemed = worksheet('券类型·15日核销记录数', 'overview', 'coupon_type', 'redeemed15_records', label_field='redeemed15_records', formula_desc='SUM(redeemed15_records)')
    scheme_scatter = worksheet('券方案·领取规模与核销率', 'overview', 'scheme', 'rate15', kind='Circle', dim=False, detail=True, log_x=True, formula_desc='X=SUM(receipt_records)对数轴；Y=SUM(redeemed15_records)/SUM(receipt_records)；前12+其余达标合并+低样本合并，非最佳券排名')
    threshold = worksheet(
        '满减门槛·核销率（标签=领券量）',
        'overview',
        'threshold',
        'rate15',
        label_field='receipt_records',
    )
    bands = worksheet(
        '名义优惠比例·核销率（标签=领券量）',
        'overview',
        'discount_band',
        'rate15',
        label_field='receipt_records',
    )
    dist = worksheet(
        '距离编码·核销率（标签=领券量）',
        'overview',
        'distance',
        'rate15',
        label_field='receipt_records',
    )
    schemes = worksheet(
        '主流券方案与长尾分组·核销率',
        'overview',
        'scheme',
        'rate15',
        label_field='receipt_records',
        reflines=[{'field': 'rate15', 'value': 0.06113747315533732, 'label': '全期所有券基准 6.11%', 'color': '#c85200'}],
        formula_desc='SUM(redeemed15_records)/SUM(receipt_records)（展示领券量前12主流方案+其余≥100条达标方案合并+9种<100条低样本方案合并，完整45种见配套表）',
    )

    k3 = [
        worksheet(n, 'may', 'kpi', f, kind='Text', dim=False, formula_desc=fd)
        for n, f, fd in [
            ('5月领券记录', 'receipt_records', 'SUM(receipt_records)（固定2016-05评价期）'),
            ('5月领券用户', 'receipt_users', 'SUM(receipt_users)（固定2016-05按场景精确去重预计算）'),
            ('5月核销记录', 'redeemed15_records', 'SUM(redeemed15_records)（观察至2016-06-15）'),
            ('5月核销率', 'rate15', 'IF SUM(receipt_records)>0 THEN SUM(redeemed15_records)/SUM(receipt_records) END'),
        ]
    ]
    seg = worksheet(
        '历史分层·5月记录核销率',
        'may',
        'segment',
        'rate15',
        label_field='receipt_records',
        reflines=[{'field': 'rate15', 'value': 0.10020989282463733, 'label': '5月整体 10.02%', 'color': '#c85200'}],
        formula_desc='IF SUM(receipt_records)>0 THEN SUM(redeemed15_records)/SUM(receipt_records) END（标签=领券记录数）',
    )
    segu = worksheet(
        '历史分层·5月核销用户占比',
        'may',
        'segment',
        'user_share',
        label_field='receipt_users',
        reflines=[{'field': 'user_share', 'value': 0.10010846206185706, 'label': '5月整体 10.01%', 'color': '#c85200'}],
        formula_desc='IF SUM(receipt_users)>0 THEN SUM(redeemed15_users)/SUM(receipt_users) END（标签=领券用户数）',
    )
    merchant = worksheet(
        '商家·5月领券量与核销率',
        'may',
        'merchant',
        'rate15',
        kind='Circle',
        dim=False,
        detail=True,
        log_x=True,
        reflines=[
            {'field': 'rate15', 'value': 0.10020989282463733, 'label': '5月核销率 10.02%', 'color': '#c85200'},
            {'field': 'receipt_records', 'value': 100, 'label': '样本阈值 100条', 'color': '#5c6068'},
        ],
        formula_desc='X轴=SUM(receipt_records)（对数刻度），Y轴=SUM(redeemed15_records)/SUM(receipt_records)，含100条样本量与10.02%核销率双参考线，细分=merchant_id',
    )
    merchantt = worksheet(
        '候选商家（前20+3381）·核销率',
        'may',
        'candidate',
        'rate15',
        label_field='receipt_records',
        reflines=[{'field': 'rate15', 'value': 0.10020989282463733, 'label': '5月整体 10.02%', 'color': '#c85200'}],
        formula_desc='SUM(redeemed15_records)/SUM(receipt_records)（涵盖5月领券量前20名及全期最大基准商家3381共21家，工具提示含结构诊断）',
    )

    dashboards = el(root, 'dashboards')
    dashboard_specs = []

    def dashboard(name, title, scope, kpis, charts, note):
        dash = el(dashboards, 'dashboard', name=name)
        lo = el(dash, 'layout-options')
        ft = el(el(lo, 'title'), 'formatted-text')
        el(ft, 'run', title, fontsize=14)
        el(dash, 'style')
        el(dash, 'size', minwidth=1200, maxwidth=1200, minheight=840, maxheight=840)
        dss = el(dash, 'datasources')
        el(dss, 'datasource', name='Parameters')
        param = 'OverviewScope' if scope == 'overview' else 'MayScope'
        dep = el(dash, 'datasource-dependencies', datasource='Parameters')
        dep.append(copy.deepcopy(param_cols[param]))
        if scope == 'may':
            dep.append(copy.deepcopy(param_cols['SelectedMerchantID']))
        zones = el(dash, 'zones')
        base = el(zones, 'zone', h=100000, id=1, type_v2='layout-basic', w=100000, x=0, y=0)

        def zone(zid, x, y, w, h, **kw):
            return el(base, 'zone', id=zid, x=x, y=y, w=w, h=h, **kw)

        zone(2, 1000, 500, 96000, 4500, type_v2='title')
        text = zone(3, 1000, 5000, 96000, 4300, type_v2='text')
        el(el(text, 'formatted-text'), 'run', note, fontsize=9, fontcolor='#505965')
        zone(4, 1000, 9400, 68000 if scope == 'may' else 96000, 4500, type_v2='paramctrl', param='[Parameters].[' + param + ']', mode='compact')
        if scope == 'may':
            zone(5, 70000, 9400, 27000, 4500, type_v2='paramctrl', param='[Parameters].[SelectedMerchantID]', mode='typein')
        z = 10
        if kpis:
            for i, name_ in enumerate(kpis):
                zone(z, 1000 + i * (96000 // len(kpis)), 14500, (96000 // len(kpis)) - 800, 10500, name=name_)
                z += 1
            top = 26000
        else:
            top = 15000
        for name_, x, y, ww, hh in charts:
            zone(z, x, top + y, ww, hh, name=name_)
            z += 1
        el(dash, 'simple-id', uuid='{' + str(uuid.uuid5(uuid.NAMESPACE_DNS, f'dash.{name}')).upper() + '}')
        dashboard_specs.append(
            {
                'dashboard': name,
                'title': title,
                'scope': scope,
                'parameter': param,
                'size': '1200x840',
                'sheets': kpis + [x[0] for x in charts],
            }
        )

    dashboard(
        '01 总体与领券月份',
        'O2O｜总体与领券月份表现',
        'overview',
        k1,
        [
            (monthv, 1000, 0, 47000, 31000),
            (monthr, 49500, 0, 47000, 31000),
            (lag, 1000, 32000, 47000, 40000),
            (cum, 49500, 32000, 47000, 40000),
        ],
        '场景互斥：选择新月份或券类型会替换原场景，不叠加；仅“5月·直接折扣”为预设交集。点击右上月份柱切换；同图空白可复位，也可下拉选“全部”。领券01-01至06-15，观察至06-30；6月为部分月份，重复保留。',
    )
    dashboard(
        '02 券方案与距离',
        'O2O｜券规模、核销贡献与效率',
        'overview',
        [k1[0], k1[2], k1[3]],
        [
            (type_volume, 1000, 0, 31000, 19000),
            (type_redeemed, 33500, 0, 31000, 19000),
            (types, 66000, 0, 31000, 19000),
            (threshold, 1000, 20000, 31000, 25000),
            (bands, 33500, 20000, 31000, 25000),
            (dist, 66000, 20000, 31000, 25000),
            (scheme_scatter, 1000, 46000, 47000, 27000),
            (schemes, 49500, 46000, 47500, 27000),
        ],
        '与第1页共享互斥场景，选择会替换、不叠加；仅“5月·直接折扣”为预设交集。上排分别为领券量、15日核销量、核销率；下排点击右侧方案条形：左侧橙点+标签定位；点击空白清除，不改变KPI。橙线=全期所有券基准6.11%，不会随场景改变；低样本不评最佳。',
    )
    dashboard(
        '03 用户与商家诊断',
        'O2O｜用户与商家诊断（固定5月评价期）',
        'may',
        k3,
        [
            (seg, 1000, 0, 47000, 27000),
            (segu, 49500, 0, 47000, 27000),
            (merchant, 1000, 28000, 47000, 44000),
            (merchantt, 49500, 28000, 47000, 44000),
        ],
        '固定2016-05-01分层；5月领券观察至6月15日，与前两页独立。左右分层图均可点击筛选、同图空白复位；点击右下商家条形，橙色大点+ID标记选中商家，取消选择清空；不会改变KPI。橙线：记录率10.02%／用户占比10.01%。散点X为对数；少于100条为低样本；右下完整21家。',
    )

    windows = el(root, 'windows', source_height=30)
    for sp in specs:
        win = el(windows, 'window', name=sp['sheet'], maximized='false')
        win.set('class', 'worksheet')
        cards = el(win, 'cards')
        edge = el(cards, 'edge', name='top')
        for typ in ['columns', 'rows', 'title']:
            el(el(edge, 'strip', size=30), 'card', type=typ)
        edge = el(cards, 'edge', name='left')
        strip = el(edge, 'strip', size=170)
        for typ in ['pages', 'filters', 'marks']:
            el(strip, 'card', type=typ)
        el(win, 'simple-id', uuid='{' + str(uuid.uuid5(uuid.NAMESPACE_DNS, f'win.ws.{sp["sheet"]}')).upper() + '}')

    ordered_dashboards = [dashboard_specs[1], dashboard_specs[2], dashboard_specs[0]]
    for idx, ds in enumerate(ordered_dashboards):
        is_default = idx == len(ordered_dashboards) - 1
        win = el(windows, 'window', name=ds['dashboard'], maximized='true' if is_default else 'false')
        win.set('class', 'dashboard')
        vp = el(win, 'viewpoints')
        for name in ds['sheets']:
            ztype = 'entire-view'
            el(el(vp, 'viewpoint', name=name), 'zoom', type=ztype)
        el(win, 'active', id=-1)
        el(win, 'simple-id', uuid='{' + str(uuid.uuid5(uuid.NAMESPACE_DNS, f'win.dash.{ds["dashboard"]}')).upper() + '}')

    E.indent(root)
    return root, {'worksheets': specs, 'dashboards': dashboard_specs, 'actions': action_specs}


def write_twbx(twbx_path, manifest, source_data_dir):
    """Write a self-contained packaged workbook (.twbx) with embedded Data/*.hyper and Data/*.csv."""
    root, _ = build_twb_tree(manifest, data_dir='Data', hyper_dir='Data')
    twb_bytes = E.tostring(root, encoding='utf-8', xml_declaration=True)
    source_data_dir = Path(source_data_dir)
    with zipfile.ZipFile(twbx_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('O2O三页分析看板.twb', twb_bytes)
        for fn in ['overview_charts.hyper', 'may_charts.hyper', 'overview_charts.csv', 'may_charts.csv']:
            zf.write(source_data_dir / fn, arcname=f'Data/{fn}')


def validate_twb_against_tableau_xsd(twb_paths):
    """Validate .twb files against Tableau Public 2026.2.3 embedded XSD Schema (#20, #9, #10)."""
    dependencies = resolve_tableau_dependencies()
    code = r"""
import json, re, sys
from pathlib import Path
import lxml.etree as ET

rcc = Path(sys.argv[1]).read_bytes()
schemas = [m.group(0).decode("utf-8") for m in re.finditer(rb"<xs:schema\b.*?</xs:schema>", rcc, re.S)]

def preprocess_xsd(xsd_text, features):
    token_re = re.compile(r"<!--\?(IF|ELSE|END)(?:\s+(!?[A-Za-z0-9_]+))?\s*-->")
    pos = 0
    out = []
    stack = [True]
    for m in token_re.finditer(xsd_text):
        if all(stack):
            out.append(xsd_text[pos:m.start()])
        op, feat = m.group(1), m.group(2)
        if op == "IF":
            cond = (feat[1:] not in features) if feat.startswith("!") else (feat in features)
            stack.append(cond)
        elif op == "ELSE":
            stack[-1] = not stack[-1]
        elif op == "END":
            stack.pop()
        pos = m.end()
    if all(stack):
        out.append(xsd_text[pos:])
    res = "".join(out)
    res = re.sub(r"<xs:choice>\s*</xs:choice>", "", res)
    res = res.replace(
        '<xs:import namespace="http://www.tableausoftware.com/xml/user"/>',
        '<xs:import namespace="http://www.tableausoftware.com/xml/user" schemaLocation="user.xsd"/>',
    ).replace(
        '<xs:import namespace="http://www.w3.org/XML/1998/namespace"/>',
        '<xs:import namespace="http://www.w3.org/XML/1998/namespace" schemaLocation="xml.xsd"/>',
    )
    return res

class LocalResolver(ET.Resolver):
    def resolve(self, url, pubid, context):
        if "xml.xsd" in url:
            return self.resolve_string(schemas[9], context)
        if "user.xsd" in url:
            return self.resolve_string(schemas[10], context)
        return None

results = {}
for p_str in sys.argv[2:]:
    p = Path(p_str)
    text = p.read_text(encoding="utf-8")
    text = re.sub(r"_\.fcp\.[A-Za-z0-9_]+\.(?:true|false)\.\.\.", "", text)
    doc = ET.fromstring(text.encode("utf-8"))
    manifest = doc.find("document-format-change-manifest")
    features = {el.tag.split("}")[-1] for el in manifest} if manifest is not None else set()
    xsd_str = preprocess_xsd(schemas[20], features)
    parser = ET.XMLParser()
    parser.resolvers.add(LocalResolver())
    schema = ET.XMLSchema(ET.fromstring(xsd_str.encode("utf-8"), parser))
    ok = schema.validate(doc)
    errs = [str(e) for e in schema.error_log]
    results[str(p)] = {"valid": ok, "error_count": len(errs), "errors": errs[:20], "manifest_features": sorted(features)}
    if not ok:
        raise RuntimeError(f"XSD validation failed for {p}: {errs[:5]}")
print(json.dumps({"status": "PASS", "rcc_path": sys.argv[1], "files": results}, ensure_ascii=False))
"""
    res = subprocess.run(
        [str(dependencies['schema_python']), '-B', '-c', code, str(dependencies['rcc_resource'])] + [str(p) for p in twb_paths],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(res.stdout)


def build_workbooks(out_dir, qa_dir):
    out = Path(out_dir)
    q = Path(qa_dir)
    m = json.loads((out / 'manifest.json').read_text(encoding='utf-8'))
    m['results_dir'] = str(out)

    # 1. Run-scoped workbook pointing to runs/<run_id>/data
    run_root, specs_dict = build_twb_tree(m, data_dir=out / 'data', hyper_dir=out / 'data')
    run_twb = out / 'workbook/O2O三页分析看板.twb'
    E.ElementTree(run_root).write(run_twb, encoding='utf-8', xml_declaration=True)
    run_twbx = out / 'workbook/O2O三页分析看板.twbx'
    write_twbx(run_twbx, m, out / 'data')

    # 2. Top-level fixed-path workbook pointing to o2o_work/tableau/data
    top_data = WORK / 'tableau/data'
    top_root, _ = build_twb_tree(m, data_dir=top_data, hyper_dir=top_data)
    top_twb = WORK / 'tableau/O2O三页分析看板.twb'
    E.ElementTree(top_root).write(top_twb, encoding='utf-8', xml_declaration=True)
    top_twbx = WORK / 'tableau/O2O三页分析看板.twbx'
    write_twbx(top_twbx, m, top_data)

    # 3. Save worksheet_specs.json
    specs_text = json.dumps(specs_dict, ensure_ascii=False, indent=2) + '\n'
    (q / 'worksheet_specs.json').write_text(specs_text, encoding='utf-8')
    (WORK / 'tableau/worksheet_specs.json').write_text(specs_text, encoding='utf-8')

    # 4. Validate against Tableau Public 2026.2.3 embedded XSD Schema
    xsd_report = validate_twb_against_tableau_xsd([run_twb, top_twb])
    (q / 'twb_xsd_validation.json').write_text(
        json.dumps(xsd_report, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    return {
        'run_twb': str(run_twb),
        'run_twbx': str(run_twbx),
        'top_twb': str(top_twb),
        'top_twbx': str(top_twbx),
        'xsd_validation': xsd_report['status'],
        'worksheets_count': len(specs_dict['worksheets']),
        'dashboards_count': len(specs_dict['dashboards']),
        'actions_count': len(specs_dict['actions']),
    }


def main():
    info = json.loads((WORK / 'tableau/latest.json').read_text(encoding='utf-8'))
    res = build_workbooks(info['results_dir'], info['qa_dir'])
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
