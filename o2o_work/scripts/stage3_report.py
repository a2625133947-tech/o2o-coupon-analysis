"""Observed segmentation findings and proposed experiments; no contact execution."""
def rows(c,sql):
 q=c.execute(sql);return [dict(zip([d[0] for d in q.description],r)) for r in q.fetchall()]
NAMES={'unknown_history':'未知历史','high_receipt_zero_redemption':'历史多领券零核销','no_observed_consumption':'有历史但无已观察消费','recent_high_frequency':'近期高频消费','recent_low_frequency':'近期低频消费','not_recent_consumption':'非近期消费'}
def pct(n):return '不可计算' if n is None else f'{n:.4%}'
def write_report(c,out,qa,details,rules,independent,sensitivity,boot):
 order=['unknown_history','high_receipt_zero_redemption','no_observed_consumption','recent_high_frequency','recent_low_frequency','not_recent_consumption'];evaluation={r['segment_id']:r for r in rows(c,'SELECT * FROM segment_evaluation')};users={r['segment_id']:r for r in rows(c,'SELECT * FROM segment_user_evaluation')}
 lines=['# 阶段3：历史分层、后续评价与触达建议','',f'运行：{details["run_id"]}。历史事件期[2016-01-01,2016-05-01)，分层时点2016-05-01；后续评价2016年5月领券，消费观察至6月15日，核销0—15日两端包含。原始重复保留，没有金额，不称完整RFM。','', '## 历史分布与冻结规则','',f'历史已观察用户{independent["historical_users"]:,}。有历史消费用户{rules["distribution"]["recency_days"]["users"]:,}；最近消费间隔Q50={rules["distribution"]["recency_days"]["q50"]:g}天，消费记录频次Q75={rules["distribution"]["history_consumption_records"]["q75"]:g}条。有成熟历史领券用户{rules["distribution"]["history_mature_receipt_records"]["users"]:,}，成熟领券频次Q90={rules["distribution"]["history_mature_receipt_records"]["q90"]:g}条；历史核销率Q90={rules["distribution"]["history_redemption_rate15"]["q90"]:g}。完整Q25/Q50/Q75/Q90见feature_distribution.csv。阈值只从历史分布导出，未使用5月核销率优化阈值。','',f'按以下优先级分层，前面的规则优先命中：1）无已观察历史→未知历史；2）成熟历史领券≥{rules["high_receipt_min"]}且历史15日核销为0→历史多领券零核销；3）历史消费记录为0→有历史但无已观察消费；4）最近消费间隔≤{rules["recent_days_max"]}且消费频次≥{rules["high_consumption_min"]}→近期高频；5）最近消费间隔≤{rules["recent_days_max"]}且消费频次不足{rules["high_consumption_min"]}→近期低频；6）其余有消费历史用户→非近期消费。规则配置见segmentation_rules.json及rule_catalog.csv。','历史多领券零核销可以包含普通消费历史，不等于从未消费。近期高频/低频是在高领券零核销规则排除后剩余的RF群体；这一优先级明确保留，不能把它们当全部近期用户。','', '## 日期可见性与未知历史','',f'历史消费{independent["historical_totals"]["history_consumption_records"]:,}条、历史领券{independent["historical_totals"]["history_receipt_records"]:,}条。历史完整15日观察的领券{independent["historical_totals"]["history_mature_receipt_records"]:,}条、核销{independent["historical_totals"]["history_redeemed15_records"]:,}条。最晚纳入历史核销率的领券日是4月15日；4月16—30日的领券可计领券频次，但不进入该率。即使未成熟券在4月底已核销，也不混入成熟率分子。','',f'分层表共{independent["universe_users"]:,}用户，是历史用户与5月领券评价用户的并集。未知历史用户只为回溯评价补入，不能声称他们是5月1日已知的可触达名单，也不能称平台新用户。特征中的0表示在指定样本/历史期未观察事件；历史核销率无成熟分母时为NULL。未知历史组5月覆盖100%是并集构造的结果，不能和其他组的自然领券覆盖率作业务优劣比较。','', '## 5月后续评价','', '|互斥群体|分层用户总量|5月领券用户|领券记录|15日核销记录|记录率|核销用户|用户核销占比|','|---|---:|---:|---:|---:|---:|---:|---:|']
 for k in order:
  r=evaluation[k];lines.append(f'|{NAMES[k]}|{r["universe_users"]:,}|{r["mature_receipt_users"]:,}|{r["mature_receipt_records"]:,}|{r["redeemed15_records"]:,}|{pct(r["record_redemption_rate15"])}|{r["redeemed15_users"]:,}|{pct(r["redemption_user_share15"])}|')
 lines += ['',f'合计：5月领券{independent["may_receipt_records"]:,}条、15日核销{independent["may_redeemed15_records"]:,}条；领券用户{independent["may_receipt_users"]:,}、至少核销一次用户{independent["may_redeemed15_users"]:,}。总记录率{pct(independent["may_redeemed15_records"]/independent["may_receipt_records"])}，用户占比{pct(independent["may_redeemed15_users"]/independent["may_receipt_users"])}。六组记录和用户分子分母分别对齐总体，关联不扩行。','', '## 记录率、用户占比与用户均权率','', '|群体|每用户平均领券|记录核销率|核销用户占比|用户均权核销率|','|---|---:|---:|---:|---:|']
 for k in order:
  r=users[k];rpu = '不可计算' if r["receipts_per_user"] is None else f'{r["receipts_per_user"]:.3f}'
  lines.append(f'|{NAMES[k]}|{rpu}|{pct(r["record_redemption_rate15"])}|{pct(r["redemption_user_share15"])}|{pct(r["mean_within_user_redemption_rate15"])}|')
 lines += ['', '记录率以领券量给用户隐含不同权重；用户核销占比只问“至少核销一条”，用户均权核销率则先算每用户核销量/领券量再按用户平均，三者不可替代。用户均权率保留sum_within_user_redemption_rate15与evaluation_users，未把用户均权率冒充总体记录率。',f'按用户为单位进行{boot["replicates"]}次固定种子重抽样，保留用户内多次领券相关性；95%百分位区间见segment_bootstrap_intervals.csv。按每用户(n,y)精确类别进行多项式重抽样与逐用户重抽样等价。区间条件于已观察5月领券用户，假设用户之间独立，未修正多重比较，不证明因果、增量或样本代表性。','', '## 初步触达建议（仅方案，不执行触达）','']
 suggestions={
 'recent_high_frequency':('优先验证增量与过度补贴风险',f'后续记录率{pct(evaluation["recent_high_frequency"]["record_redemption_rate15"])}、用户占比{pct(evaluation["recent_high_frequency"]["redemption_user_share15"])}，用户均权率{pct(users["recent_high_frequency"]["mean_within_user_redemption_rate15"])}，三种口径均较高。可优先用小规模随机分组比较不触达、提醒与券方案；高自然核销并不代表最高营销增量，不能直接给更多补贴。'),
 'recent_low_frequency':('验证提醒或下一次消费场景',f'记录率{pct(evaluation["recent_low_frequency"]["record_redemption_rate15"])}、用户占比{pct(evaluation["recent_low_frequency"]["redemption_user_share15"])}，可做同观察期提醒实验；需要对照和打扰指标，不把低频命名成低价值。'),
 'high_receipt_zero_redemption':('先诊断既往障碍，再测试方案',f'历史零核销组后续记录率{pct(evaluation["high_receipt_zero_redemption"]["record_redemption_rate15"])}、用户占比{pct(evaluation["high_receipt_zero_redemption"]["redemption_user_share15"])}，并非永远不核销。检查券门槛、距离和商家构成后小规模试验，不能仅按领券次数继续发券。'),
 'not_recent_consumption':('验证唤回假设',f'记录率{pct(evaluation["not_recent_consumption"]["record_redemption_rate15"])}、用户占比{pct(evaluation["not_recent_consumption"]["redemption_user_share15"])}，非近期只是本样本内35天以上没有消费，不能称平台流失。唤回测试应保留不触达对照，并补充成本。'),
 'no_observed_consumption':('补充覆盖信息与低成本探索',f'记录率{pct(evaluation["no_observed_consumption"]["record_redemption_rate15"])}、用户占比{pct(evaluation["no_observed_consumption"]["redemption_user_share15"])}；在本历史窗口未观察消费，不等于从未消费或没有购买力。避免依据脱敏样本作个人价值判断。'),
 'unknown_history':('先补充历史覆盖，首次可见后再评估',f'记录率{pct(evaluation["unknown_history"]["record_redemption_rate15"])}、用户占比{pct(evaluation["unknown_history"]["redemption_user_share15"])}；不是平台新用户。在5月1日并无这些用户的已观察名单，不能倒推为当时可执行名单；首次可见时按实际账号覆盖重新判断。')}
 import csv
 with (out/'触达建议.csv').open('w',encoding='utf-8',newline='') as f:
  wr=csv.writer(f);wr.writerow(['segment_id','segment_name','proposal','evidence_and_limits','execution_status'])
  for k in order:
   action,reason=suggestions[k];wr.writerow([k,NAMES[k],action,reason,'未执行；待随机对照验证']);lines+= [f'- **{NAMES[k]}：{action}。** {reason}']
 lines += ['', '上述建议没有收入、补贴成本、实际触达曝光或随机对照支持，不能计算ROI或排序真实营销增量。用户群体的券、商家、距离和后续活动构成可能不同；这里没有完成分层之间的全部结构控制。未来真实实施时需重新核实用户名单与授权，并补充成本、频控和退订/打扰指标。','', '## 重复敏感性、追溯与验收','',f'使用相同冻结规则，在7字段完全去重的数据上重新计算，{sensitivity["segment_changes"]:,}/{sensitivity["label_users_compared"]:,}个用户标签改变。主口径仍保留重复，敏感性明细与转移矩阵见dedup_sensitivity.json、dedup_segment_transitions.csv；不能因相同字段就认定业务重复。','', 'user_case_features.csv为每组一名匿名样例；user_case_history_events.csv只列5月1日前可见的领券/消费事件并带record_id回查源行。未知历史样例没有历史事件属于正确结果，不伪造解释。','', '验收包括：所有用户特征与标签从原CSV独立复算；用户/asof唯一；六组互斥；历史事件、最近消费严格早于时点；4月16日起未成熟券不进入历史核销率；评价仅5月领券且核销观察不超过6月15日；每用户结局与分组分别对账；关联行数不增加。另把全部未来日期改至2030、最终核销标签反转和最终状态覆盖，所有历史特征指纹保持不变。','', f'来源：08_user_features_asof.sql、09_user_segments.sql、10_user_segment_evaluation.sql；输入和代码指纹见manifest.json；实际检查与日志见{qa.name}对应qa运行目录。阶段4 Tableau、线上增强与对外发布未实施；人工学习复现未验证。']
 (out/'分层评价与触达建议.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
 rule_md = f'''# 阶段3分层规则（按顺序命中）

固定时点{rules["asof_date"]}；历史事件早于该日，历史核销率只用最晚4月15日领券。实际历史分布导出{rules["recent_days_max"]}天近期阈值（最近消费间隔中位数）、{rules["high_consumption_min"]}条消费高频阈值（消费频次Q75）、{rules["high_receipt_min"]}条成熟领券阈值（成熟领券频次Q90）。未用5月结局调整阈值。

|优先级|群体|条件|
|---|---|---|
|1|未知历史|1—4月样本未观察到领券或消费事件|
|2|历史多领券零核销|已观察历史；成熟领券≥{rules["high_receipt_min"]}且历史15日核销0|
|3|有历史但无已观察消费|未命中前规则，历史消费记录0|
|4|近期高频消费|未命中前规则，最近消费间隔≤{rules["recent_days_max"]}天且消费记录≥{rules["high_consumption_min"]}|
|5|近期低频消费|未命中前规则，最近消费间隔≤{rules["recent_days_max"]}天且消费记录<{rules["high_consumption_min"]}|
|6|非近期消费|未命中前规则，有历史消费且间隔>{rules["recent_days_max"]}天|

一用户一时点只有一行、一个群体；第2组也可有普通消费，RF组排除更早命中者。无成熟分母时历史核销率NULL。未知历史不是平台新用户，回溯补入的5月用户不是5月1日已知名单。频次是源记录，不是订单；无金额，不称完整RFM。

配置见segmentation_rules.json，分布见feature_distribution.csv，完整特征/标签见user_features_asof.csv、user_segments.csv。
'''
 (out/'分层规则.md').write_text(rule_md, encoding='utf-8')
