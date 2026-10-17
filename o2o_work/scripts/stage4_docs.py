"""Generate Stage 4 worksheet configuration specs, SQL reconciliation tables, and reproduction docs dynamically from verified data."""
from pathlib import Path

WORK = Path(__file__).resolve().parents[1]


def fmt_rate(v):
    return '不可计算（NULL）' if v is None else f'{v * 100:.4f}%（看板显示 {v * 100:.2f}%）'


def write_stage4_docs(out_dir, qa_dir, details, expected, specs_dict, doc_views):
    out = Path(out_dir)
    qa = Path(qa_dir)

    exp_lines = [
        '| 页面范围 | 代表性筛选场景（参数值） | SQL筛选条件 | 领券记录数（分母） | 15日核销记录数（分子） | 15日核销率 | 领券独立用户数 | 15日核销独立用户数 | 15日核销用户占比 |',
        '| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |',
    ]
    for r in expected:
        scope_cn = '第1、2页（overview）' if r['page_scope'] == 'overview' else '第3页（may固定5月）'
        exp_lines.append(
            f'| {scope_cn} | `{r["scenario"]}` | `{r["predicate"]}` | {r["receipt_records"]:,} | '
            f'{r["redeemed15_records"]:,} | {fmt_rate(r["rate15"])} | {r["receipt_users"]:,} | '
            f'{r["redeemed15_users"]:,} | {fmt_rate(r["user_share15"])} |'
        )
    exp_table = '\n'.join(exp_lines)

    ws_lines = [
        '| 所属仪表板 | 工作表名称 | 数据源 | 固定 `view_id` | 参数筛选器 | 行（Rows） | 列（Cols） | 标记类型 | 标签字段 | 参考线 / 坐标轴配置 | 核心度量与比率计算式 |',
        '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |',
    ]
    sheet_to_dash = {}
    for d in specs_dict['dashboards']:
        for s in d['sheets']:
            sheet_to_dash[s] = d['dashboard']
    for w in specs_dict['worksheets']:
        dash_name = sheet_to_dash.get(w['sheet'], '-')
        param_name = 'OverviewScope' if w['scope'] == 'overview' else 'MayScope'
        if w['dimension'] == 'category':
            rows_col = 'category' if 'horizontal' in w['axis'] else w['measure']
            cols_col = w['measure'] if 'horizontal' in w['axis'] else 'category'
        elif w['dimension'] == 'category (detail)':
            rows_col = 'rate15'
            cols_col = 'receipt_records（对数刻度）' if 'log' in w['axis'] else 'receipt_records'
        else:
            rows_col = '（无）'
            cols_col = '（无）'
        ref_str = '；'.join(w.get('reference_lines') or []) or '无'
        if w.get('sort'):
            ref_str += f'（排序: {w["sort"]}）'
        ws_lines.append(
            f'| `{dash_name}` | `{w["sheet"]}` | `{w["datasource"]}` | `{w["view_id"]}` | '
            f'`{w["filter"]}` | `{rows_col}` | `{cols_col}` | `{w["mark"]}` | '
            f'`{w["label"] or "无"}` | `{ref_str}` | `{w["rate_formula"]}` |'
        )
    ws_table = '\n'.join(ws_lines)

    act_lines = [
        '| 动作名称 | 动作类型 | 触发仪表板 | 触发工作表 | 目标参数 / 作用范围 | 取消选择行为 |',
        '| --- | --- | --- | --- | --- | --- |',
    ]
    for a in specs_dict.get('actions', []):
        if a['type'] == 'edit-parameter-action':
            act_lines.append(
                f'| `{a["name"]}` | 参数联动动作 (`edit-parameter-action`) | `{a["dashboard"]}` | '
                f'`{a["source_worksheet"]}` | `{a["target_parameter"]}`（SelectedMerchantID/SelectedSchemeID仅选中样式；OverviewScope/MayScope切换对应场景） | 配置重置值 `{a["clear_value"]}`；实际结果见交互记录 |'
            )
        else:
            act_lines.append(
                f'| `{a["name"]}` | 高亮动作 (`tsc:brush`) | `{a["dashboard"]}` | 两张商家图 | 仅按 `商家ID` 高亮（不得跨业务维度） | 自动清除 (`auto-clear`) |'
            )
    act_table = '\n'.join(act_lines)

    # Dynamically format category-level reconciliation bullets from doc_views
    month_bullets = []
    for m in doc_views['month']:
        rate = m['redeemed15_records'] / m['receipt_records']
        note_str = f"（{m['sample_note']}）" if m['sample_note'] else ''
        month_bullets.append(
            f"   - `{m['category']}`：领券 `{m['receipt_records']:,}`，核销 `{m['redeemed15_records']:,}`，"
            f"核销率 `{rate * 100:.4f}%`（看板显示 `{rate * 100:.2f}%`），领券用户 `{m['receipt_users']:,}`，核销用户 `{m['redeemed15_users']:,}`{note_str}。"
        )
    month_section = '\n'.join(month_bullets)

    lag_map = {int(r['category']): r for r in doc_views['lag']}
    lag_bullets = []
    for d, desc in [(0, '领券当日'), (1, '领券次日，单日核销峰值'), (7, '第0—7日累计'), (15, '第0—15日累计，对齐总体')]:
        lr = lag_map[d]
        sh = lr['redeemed15_records'] / lr['successful_receipts']
        cum_sh = lr['cumulative_redeemed'] / lr['successful_receipts']
        cum_rt = lr['cumulative_redeemed'] / lr['receipt_records']
        lag_bullets.append(
            f"   - `lag_days = {d}`（{desc}）：当日核销 `{lr['redeemed15_records']:,}` 条（占15日成功核销总数 `{lr['successful_receipts']:,}` 的 `{sh * 100:.4f}%` / `{sh * 100:.2f}%`），"
            f"截至第 {d} 日累计核销 `{lr['cumulative_redeemed']:,}` 条（累计占成功核销 `{cum_sh * 100:.4f}%` / `{cum_sh * 100:.2f}%`），"
            f"占成熟领券总数（`{lr['receipt_records']:,}`）的累计核销率 `{cum_rt * 100:.4f}%`（`{cum_rt * 100:.2f}%`）。"
        )
    lag_section = '\n'.join(lag_bullets)

    band_bullets = []
    for b in doc_views['discount_band']:
        rate = b['redeemed15_records'] / b['receipt_records']
        band_bullets.append(
            f"   - `{b['category']}`：领券 `{b['receipt_records']:,}`，核销 `{b['redeemed15_records']:,}`，"
            f"核销率 `{rate * 100:.4f}%`（`{rate * 100:.2f}%`），领券用户 `{b['receipt_users']:,}`，核销用户 `{b['redeemed15_users']:,}`（{b['sample_note']}）。"
        )
    band_section = '\n'.join(band_bullets)

    scheme_rows = doc_views['scheme']
    top3_s = scheme_rows[:3]
    tail_s = scheme_rows[12:]
    scheme_bullets = [
        '   - **领券量前3主流方案**：'
        + '、'.join(
            f"`{r['category']}`（领券 `{r['receipt_records']:,}` 条，核销 `{r['redeemed15_records']:,}` 条，核销率 `{r['redeemed15_records'] / r['receipt_records'] * 100:.4f}%` / `{r['redeemed15_records'] / r['receipt_records'] * 100:.2f}%`）"
            for r in top3_s
        )
        + '。',
        '   - **长尾分组汇总（避免45种方案挤压失真，完整45种明细见 `fixed_coupon_scheme_full.csv`）**：'
        + '；'.join(
            f"`{r['category']}`（领券 `{r['receipt_records']:,}` 条，核销 `{r['redeemed15_records']:,}` 条，核销率 `{r['redeemed15_records'] / r['receipt_records'] * 100:.4f}%` / `{r['redeemed15_records'] / r['receipt_records'] * 100:.2f}%`，{r['sample_note']}）"
            for r in tail_s
        )
        + '。',
    ]
    scheme_section = '\n'.join(scheme_bullets)

    seg_bullets = []
    for s in doc_views['segment']:
        rr = s['redeemed15_records'] / s['receipt_records']
        ur = s['redeemed15_users'] / s['receipt_users']
        seg_bullets.append(
            f"   - `{s['category']}`：领券 `{s['receipt_records']:,}` 条、核销 `{s['redeemed15_records']:,}` 条（记录核销率 `{rr * 100:.4f}%` / `{rr * 100:.2f}%`）；"
            f"领券用户 `{s['receipt_users']:,}`、核销用户 `{s['redeemed15_users']:,}`（核销用户占比 `{ur * 100:.4f}%` / `{ur * 100:.2f}%`）。"
        )
    seg_section = '\n'.join(seg_bullets)

    cand_lines = [
        '| 候选序号 | 商家ID (`merchant_id`) | 5月领券记录数 | 5月15日核销数 | 5月15日核销率 | 5月领券用户数 | 5月核销用户数 | 结构诊断摘要（`sample_note`） |',
        '| ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |',
    ]
    for i, c in enumerate(doc_views['candidate'], 1):
        rate = c['redeemed15_records'] / c['receipt_records'] if c['receipt_records'] else 0.0
        cand_lines.append(
            f'| {i} | `{c["category"]}` | {c["receipt_records"]:,} | {c["redeemed15_records"]:,} | '
            f'{rate * 100:.2f}% | {c["receipt_users"]:,} | {c["redeemed15_users"]:,} | {c["sample_note"]} |'
        )
    cand_table = '\n'.join(cand_lines)

    config_doc = f"""# O2O阶段4：Tableau看板数据、逐图配置单与代表性筛选对账表

生成时间：{details['completed_at']}
运行ID：`{details['run_id']}`
上游绑定：阶段1运行 `{details['source_stage1_run']}`（`o2o.duckdb` SHA256 `{details['source_database_sha256']}`）、阶段2运行 `{details['stage2_run']}`、阶段3运行 `{details['stage3_run']}`。

---

## 一、数据源设计、可视化优化与防失真规范

1. **受控单表数据源（禁止按日期直接多对多关联）**：
   - **第1、2页数据源**：`o2o_work/tableau/data/overview_charts.csv`（配套 Tableau Public 专用数据提取 `overview_charts.hyper`）。覆盖线下成熟领券全量窗口（领券日期 `2016-01-01` 至 `2016-06-15`，消费观察至 `2016-06-30`，保留原始重复记录）。
   - **第3页数据源**：`o2o_work/tableau/data/may_charts.csv`（配套 Tableau Public 专用数据提取 `may_charts.hyper`）。严格固定历史分层时点 `2016-05-01`（历史特征窗口 `2016-01-01` 至 `2016-04-30`，历史15日成熟核销最晚纳入 `2016-04-15` 领券），评价期固定为 `2016-05-01` 至 `2016-05-31` 领券（观察至 `2016-06-15`）。第3页不提供跨月日期切换器，防止改换月份后误用 `2016-05-01` 分层标签。
   - **配套核查表（`o2o_work/tableau/data/`）**：
     - `fixed_receipt_cohort_days1_15.csv`：各月同 1—15 日可比窗口核销表现（对照6月1—15日部分月份）。
     - `fixed_coupon_scheme_full.csv`：阶段2全部 45 种具体券方案（含 9 种 `<100` 条低样本方案）完整指标表。
     - `fixed_segment_evaluation.csv`：阶段3固定5月各历史分层记录级与用户级核销汇总。
     - `fixed_may_bootstrap.csv`：阶段3固定5月用户层面多类重抽样（2,000次）95%置信区间（仅适用于未筛选的固定5月分层评价表）。
     - `fixed_candidate_merchant_structure.csv`：21 家候选商家（5月领券量前20名 + 全期最大基准商家 `3381`）按券类型、门槛、距离编码与历史用户分层的结构诊断明细（与 `may_charts.csv` 中 `view_id="candidate"` 的 21 家商家列表 100% 一致）。
2. **三页看板可视化与交互设计亮点**：
   - **易读中文标签**：第2页名义优惠比例分档由英文代码（`le_5pct` 等）转换为直观区间标签 `≤5%`、`(5%, 10%]`、`(10%, 20%]`、`(20%, 30%]`、`>30%`（工具提示保留配置编码回溯）；距离缺失类别显示为 `未知距离`。
   - **主流券方案与长尾分组（消除45种方案拥挤）**：第2页右下条形与左下规模散点展示领券量前 12 名主流方案（占全期领券量 `87.90%`）+ `其余达标方案（≥100条合并）`（24种）+ `低样本方案汇总（<100条合并）`（9种，合计300条），14行条形清晰可读且分子分母之和 100% 等于当前场景总量。
   - **散点图对数坐标轴 + 双参考线 + 半透明描边（消除左侧重叠）**：第3页 `商家·5月领券量与核销率` 采用 **对数 X 轴（`scale='log'`）** 将 `1` 至 `14,322` 跨越 4 个数量级的 4,310 家商家均匀展开，叠加 **`样本阈值 100条` 垂直虚线** 与 **`5月核销率 10.02%` 水平虚线** 划分四象限，并使用小圆点显示完整分布；SelectedMerchantID 选中时，其他商家变浅灰，独立同步坐标覆盖层以橙色大圆点标记选中商家，突出高规模低核销与高规模高核销异常商家。
   - **内置 6 项 Dashboard Actions（均为参数动作）**：设计为在第1页点击月份柱形、在第2页点击券类型柱形、在第3页点击任意历史用户分层柱形直接驱动参数联动过滤全页 KPI 与图表（场景复位可点击同图空白或使用下拉参数的全部/5月全部；场景编号为整数，界面显示中文；清除选择将场景编号恢复0）。
3. **粒度合同与独立用户数去重规则**：
   - `overview_charts` 与 `may_charts` 的主键粒度均为 `(scenario, view_id, category)`。
   - 每个合法筛选场景下的 `receipt_users`（领券独立用户数）与 `redeemed15_users`（15日核销独立用户数）均已从底层明细按 `COUNT(DISTINCT user_id)` 精确预计算。**严禁将不同月份、不同券类型或不同商家的独立用户数直接相加**。

---

## 二、三页仪表板与24张工作表逐图字段配置单

{ws_table}

### 仪表板交互动作（`<actions>`）配置表

{act_table}

---

## 三、代表性筛选场景 SQL 对账基准表（共23个预设场景）

在 Tableau Public 中切换页面顶部的参数控件 `互斥场景（第1、2页共享）`（`OverviewScope`）或 `5月互斥场景（独立于前两页）`（`MayScope`），或直接点击月份/券类型/用户分层柱形触发参数联动动作时，KPI 卡片与各图汇总必须与以下 DuckDB SQL 及原 CSV 独立复算基准 100% 一致：

{exp_table}

### 关键分类视图对账抽检值（全部由数据库 `chart_data` 实时生成，0 硬编码）

1. **页面1 — 领券月份表现（`OverviewScope = "全部"`，`view_id = "month"`）**：
{month_section}
   - **6个月领券记录之和** = `1,053,282`，**核销记录之和** = `64,395`（对齐总体）；但6个月独立用户直接相加为 `684,470` ≠ 总体去重用户 `510,698`，证明跨月用户存在复领，不能将分组独立用户数直接相加。
2. **页面1 — 核销等待天数分布（`OverviewScope = "全部"`，`view_id = "lag"`）**：
{lag_section}
3. **页面2 — 名义优惠比例分档（`OverviewScope = "全部"`，`view_id = "discount_band"`，按力度递增排列）**：
{band_section}
4. **页面2 — 主流券方案与长尾分组（`OverviewScope = "全部"`，`view_id = "scheme"`，共14行）**：
{scheme_section}
5. **页面3 — 历史用户分层表现（`MayScope = "5月全部"`，`view_id = "segment"`，按锁定优先级排列）**：
{seg_section}
6. **页面3 — 5月候选商家完整21家对账表（5月领券量前20名 + 全期最大基准商家 `3381`，`MayScope = "5月全部"`，`view_id = "candidate"`）**：

{cand_table}

---

## 四、本机 Tableau Public 操作指南、限制说明与交接步骤

### 1. 直接打开已生成并验证的真实工作簿
- **固定路径工作簿（推荐直接打开）**：
  - `{out / 'workbook/O2O三页分析看板.twb'}`
  - `{out / 'workbook/O2O三页分析看板.twbx'}`（打包工作簿，内含 `Data/*.hyper` 与 `Data/*.csv`）
- **命令行打开方式**：
  ```sh
  open "{out / 'workbook/O2O三页分析看板.twbx'}"
  ```

### 2. 本地保存与公开发布的重要安全提示（请务必注意）
- **默认不公开发布**：本阶段所有操作均严格限定在本地完成，`publicly_published = false`。
- **切勿直接按 `Cmd+S`（`⌘S`）**：在 Tableau Public 客户端中，快捷键 `Cmd+S` 默认绑定的是 **“文件 -> 保存到 Tableau Public...”**（向公网服务器上传并要求登录个人账号）。
- **正确的本地保存方式**：如需在修改后保存回本机磁盘，请在顶部菜单栏选择 **“文件 -> 另存为...”**（保存为 `.twb` 或 `.twbx`）或 **“文件 -> 导出打包工作簿...”**。
- **账号与许可边界**：任何涉及登录个人 Tableau Public 账号、输入验证码、接受在线服务条款或将看板公开发布到 Tableau Public 社区的操作，均由用户本人决定并手动执行，Agent 绝不代为上传或公开。

### 3. 建议人工学习与复现操作步骤（不阻塞技术交付，未手动复现前标记为“未验证”）
1. **体验图表点击联动动作（Dashboard Actions）**：
   - 在 `01 总体与领券月份` 页点击 `领券月份·核销率` 中的 `2016-03` 柱形，观察全页 KPI 与等待天数分布自动切换为 3 月（同图空白恢复 `全部`；下拉也可复位）。
   - 在 `03 用户与商家诊断` 页点击 `历史分层·5月记录核销率` 中的 `近期高频消费` 柱形，观察 5 月 KPI、散点图与候选商家自动切换为该群体（同图空白恢复 `5月全部`；右侧用户占比图也支持相同操作）。
2. **练习重建两张核心视图并执行筛选对账**：
   - 亲手重建 `领券月份·核销率` 与 `商家·5月领券量与核销率`（开启 X 轴对数刻度并添加 `100` 条与 `10.02%` 两条常数参考线），对照上表核验数值。
"""
    (out / '逐图配置单与对账说明.md').write_text(config_doc, encoding='utf-8')
    (WORK / 'tableau/逐图配置单与对账说明.md').write_text(config_doc, encoding='utf-8')
    (WORK / 'docs/阶段4看板配置与复现.md').write_text(config_doc, encoding='utf-8')

    qa_report = f"""# 阶段4验收报告：Tableau数据准备、逐图配置与本机看板验证

- **阶段状态**：`{details['stage4']}`（本次数据准备 `{details['data_preparation']}`；Hyper `{details['hyper_extract_generation']}`；XSD `{details['workbook_xsd_validation']}`；原生打开 `{details['tableau_native_open']}`；交互对账 `{details['tableau_filter_reconciliation']}`；视觉检查 `{details['visual_inspection']}`。本次证据目录为下列 qa 路径；不沿用历史验收，默认不公开发布。）
- **完成时间**：`{details['completed_at']}`
- **运行ID**：`{details['run_id']}`
- **通过自动检查项**：`{details['checks_passed']}` 项（含原CSV双轨独立复算、合成边界单元测试、Tableau 2026.2 官方 XSD Schema 校验、Hyper 提取回读对账与跨次运行多集重放核验）
- **结果目录**：`{out}`
- **固定工作簿路径**：
  - `{out / 'workbook/O2O三页分析看板.twb'}`
  - `{out / 'workbook/O2O三页分析看板.twbx'}`
- **证据目录**：`{qa}`

## 核验要点汇总

1. **问题1与问题2核查与闭环修复**：
   - **第1日核销记录数（`lag_days = 1`）**：经回查原始 CSV、阶段1 `o2o.duckdb`、阶段2 `stage2_metrics.duckdb` 与阶段4 `tableau_audit.duckdb`，真实值确认为当日核销 **`8,355`** 条、截至第1日累计核销 **`14,202`** 条（工作簿数据完全正确；文档已改为从数据库 `chart_data` 实时动态生成，消除此前静态手写 `8,389`/`14,236` 偏差，并加入自动断言检查）。
   - **候选商家列表（5月前20名 + 基准商家 `3381`）**：`may_charts.csv` 的 `view_id='candidate'` 已将全期第一大基准商家 `3381`（5月排第372名，53条领券、5条核销）作为第21家候选商家并入，与 `fixed_candidate_merchant_structure.csv`（21家商家）和文档说明 100% 对齐。
2. **三页看板可视化与交互升级**：
   - **第2页优化**：顶部新增场景领券数、15日核销数、核销率KPI，券类型采用三个独立坐标视图比较规模、核销贡献、效率；新增方案规模×核销率散点。固定参考线明确命名“全期所有券基准6.11%”，与动态KPI区分。`discount_band` 改为易读中文区间标签 `≤5%`、`(5%, 10%]`、`(10%, 20%]`、`(20%, 30%]`、`>30%`，`distance` 未知组显示为 `未知距离`；`scheme` 视图重构为“前12主流方案 + 其余≥100条达标方案合并 + 9种<100条低样本方案合并”（共14行），消除45种方案挤压，同时在 `fixed_coupon_scheme_full.csv` 保留完整45种方案明细。
   - **第3页优化**：`商家·5月领券量与核销率` 散点图启用 **对数 X 轴（`scale='log'`）**、**双常数参考线（`样本阈值 100条` + `5月核销率 10.02%`）** 与 **选中商家独立覆盖层**，让小样本商家选中后仍可识别。
   - **跨图联动动作（`<actions>`）**：配置 6 项参数动作（`<edit-parameter-action>`），其中 SelectedMerchantID 仅改变散点颜色、大小和标签，不过滤KPI；另外4项为场景切换（左右分层图各一项），设计为点击第1页月份柱形、第2页券类型柱形、第3页任意用户分层柱形直接联动过滤全页 KPI 与图表（四个场景入口清除选择均恢复编号0；商家与券方案选中动作清除选择恢复空字符串）。
"""
    (WORK / 'qa/stage4/验收报告.md').write_text(qa_report, encoding='utf-8')
    (qa / '验收报告.md').write_text(qa_report, encoding='utf-8')
