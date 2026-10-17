"""Prepare truthful stage5 documents for an isolated, newly computed project.

Rebind existing findings to new stage2/3 result paths; do not manufacture new
findings, native acceptance, personal work or business impact.
"""
from pathlib import Path
import argparse
import json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-work', type=Path, required=True)
    parser.add_argument('--target-work', type=Path, required=True)
    args = parser.parse_args()
    source, target = args.source_work.resolve(), args.target_work.resolve()
    if source == target:
        raise ValueError('Target must be an isolated project, not the source project')
    pointers = {i: json.loads((target / f'qa/stage{i}/latest.json').read_text()) for i in (2, 3, 4)}
    substitutions = {str(source): str(target)}
    for i in (2, 3):
        old = json.loads((source / f'qa/stage{i}/latest.json').read_text())
        substitutions[old['run_id']] = pointers[i]['run_id']
    def rebind(text):
        for old, new in substitutions.items():
            text = text.replace(old, new)
        return text
    (target / 'docs').mkdir(exist_ok=True)
    (target / 'qa/stage5').mkdir(parents=True, exist_ok=True)
    ledger = json.loads(rebind((source / 'qa/stage5/findings_evidence.json').read_text()))
    ledger['replay_binding'] = {'source_work': str(source), 'target_work': str(target),
                              'stage2_run': pointers[2]['run_id'], 'stage3_run': pointers[3]['run_id'],
                              'scope': 'Existing findings and limitations rebound to new computed results; no new marketing experiment or personal certification'}
    (target / 'qa/stage5/findings_evidence.json').write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + '\n')
    note = '> 本文为既有发现的隔离重跑副本，来源路径绑定本次阶段2/3运行。数值与原结果的完整对账须另存复现证据；不新增因果、营销收益或原生验收结论。\n\n'
    (target / 'docs/结论与建议.md').write_text(note + rebind((source / 'docs/结论与建议.md').read_text()))
    status = pointers[4]['stage4']
    (target / 'README.md').write_text(f'''# O2O隔离重建项目

本目录由修复后的实际入口重新生成。阶段4当前状态 **{status}**，以本目录latest和原生证据为准；数据准备不等于完整原生验收。本人独立复现未验证，未公开发布。

成熟领券1,053,282，0—15日核销64,395，记录率6.1137473155%。5月评价21,580/215,348。保留重复、未知距离和分子分母。看板24张工作表、3页、6项参数动作，23个预设替换范围，不支持任意叠加。

- [发现与限制](docs/结论与建议.md)
- [复现说明](docs/复现与交付说明.md)
- [筛选对账](tableau/filter_expected.csv)

没有金额、成本、曝光和随机实验，不能计算ROI或宣称因果提升；线上关联、预测、营销发送和公开发布未实施。
''')
    (target / 'docs/复现与交付说明.md').write_text('''# 隔离重建与交付说明

实际命令为本目录run.sh stage0→stage1→stage2→stage3→stage4。阶段4默认只生成CSV、Hyper、TWB/TWBX，绝不启动或关闭Tableau。数据通过时stage4仍为PARTIAL，完整原生证据经stage4 --accept-native-evidence登记后才可PASS。

阶段5登记既有发现与本次来源，允许数据准备版PARTIAL，不编造原生PASS。run.sh stage5 --verify只读核当前快照；--verify-archive RUN_ID只核历史冻结文档，明确不核当前来源。

原始CSV通过input_lock授权的只读符号链接复用。Python依赖见requirements-stage3.lock.txt；Tableau工具和连接种子由stage4_dependencies发现或通过环境变量配置；另一台机器未验证。本文不表示独立下载、授权或本人独立掌握。
''')
    print(json.dumps({'prepared': str(target), 'stage4': status, 'stage5_registration': 'NOT_RUN'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
