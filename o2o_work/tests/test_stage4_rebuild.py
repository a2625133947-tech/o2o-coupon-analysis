"""Regression checks for safe preparation and evidence-bound native acceptance."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import stage4
import stage4_tableau_runtime as runtime
import stage4_dependencies as deps


class Stage4RebuildTests(unittest.TestCase):
    def test_integer_alias_and_numeric_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / 'in.twb', Path(tmp) / 'out.twb'
            src.write_text('''<workbook><datasources><datasource name="Parameters">
<column name="[OverviewScope]" datatype="integer"><calculation/><members><member alias="全部" value="0"/><member alias="3月" value="3"/></members></column>
<column name="[MayScope]" datatype="integer"><calculation/><members><member alias="5月全部" value="0"/><member alias="近期高频消费" value="4"/></members></column>
</datasource></datasources></workbook>''')
            runtime.set_workbook_parameters(src, dst, '3月', 4)
            cols = ET.parse(dst).findall('.//column')
            self.assertEqual([c.get('value') for c in cols], ['3', '4'])
            self.assertEqual([c.find('calculation').get('formula') for c in cols], ['3', '4'])
            with self.assertRaises(ValueError):
                runtime.set_workbook_parameters(src, dst, '未来月份', 0)

    def test_legacy_ui_has_no_side_effect(self):
        with patch.object(runtime.subprocess, 'run') as run:
            with self.assertRaises(RuntimeError):
                runtime.run_tableau_session('anything')
            with self.assertRaises(RuntimeError):
                runtime.verify_and_embed_tableau_runtime('a', 'b', [])
            run.assert_not_called()

    def test_native_evidence_rejects_stale_and_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            workbook = folder / 'book.twbx'
            workbook.write_bytes(b'generated-workbook')
            shot = folder / 'native.png'
            shot.write_bytes(b'\x89PNG\r\n\x1a\n' + b'evidence')
            evidence = {'workbook_sha256': stage4.digest(workbook),
                        'checks': dict.fromkeys(stage4.NATIVE_CHECKS, 'PASS'),
                        'evidence_files': [{'path': 'native.png', 'sha256': stage4.digest(shot)}]}
            path = folder / 'evidence.json'
            def save():
                path.write_text(json.dumps(evidence))
            save()
            self.assertEqual(stage4.validate_native_evidence(path, workbook)['checks'], evidence['checks'])
            evidence['checks']['clear_reset'] = 'NOT_RUN'
            save()
            with self.assertRaises(ValueError):
                stage4.validate_native_evidence(path, workbook)
            evidence['checks']['clear_reset'] = 'PASS'
            evidence['workbook_sha256'] = 'old'
            save()
            with self.assertRaises(ValueError):
                stage4.validate_native_evidence(path, workbook)
            evidence['workbook_sha256'] = stage4.digest(workbook)
            save()
            shot.write_bytes(b'tampered')
            with self.assertRaises(ValueError):
                stage4.validate_native_evidence(path, workbook)

    def test_acceptance_binds_top_delivery_and_keeps_run_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            qa = work / 'qa/stage4/runs/new'
            qa.mkdir(parents=True)
            (work / 'tableau').mkdir()
            top = work / 'tableau/delivery.twbx'
            top.write_bytes(b'top-uuid')
            run = work / 'tableau/run.twbx'
            run.write_bytes(b'run-uuid')
            evidence_path = work / 'evidence.json'
            shot = work / 'native.png'
            shot.write_bytes(b'\x89PNG\r\n\x1a\n' + b'evidence')
            evidence = {'workbook_sha256': stage4.digest(top),
                        'checks': dict.fromkeys(stage4.NATIVE_CHECKS, 'PASS'),
                        'evidence_files': [{'path': str(shot), 'sha256': stage4.digest(shot)}]}
            evidence_path.write_text(json.dumps(evidence))
            pointer = work / 'qa/stage4/latest.json'
            details = dict.fromkeys(('data_preparation', 'hyper_extract_generation',
                                     'workbook_xsd_validation'), 'PASS')
            details.update(stage4='PARTIAL', results_dir=str(run.parent), qa_dir=str(qa),
                           source_stage1_run='source-run-unchanged',
                           workbooks={'top_twbx': str(top), 'run_twbx': str(run)})
            pointer.write_text(json.dumps(details))
            with patch.object(stage4, 'WORK', work), patch.object(stage4, 'update_state'), patch('builtins.print'):
                self.assertEqual(stage4.accept_native_evidence(evidence_path), 0)
            accepted = json.loads(pointer.read_text())
            self.assertEqual(accepted['final_acceptance'], 'PASS')
            self.assertEqual(accepted['accepted_workbook_path'], str(top.resolve()))
            self.assertEqual(accepted['native_workbook_sha256'], stage4.digest(top))
            self.assertEqual(accepted['source_stage1_run'], 'source-run-unchanged')
            self.assertEqual(run.read_bytes(), b'run-uuid')

    def test_configured_missing_tool_fails_clearly(self):
        with patch.dict('os.environ', {'O2O_HYPERD': '/no/such/hyperd'}):
            with self.assertRaisesRegex(FileNotFoundError, 'O2O_HYPERD'):
                deps.resolve_tool('hyperd')


if __name__ == '__main__':
    unittest.main()
