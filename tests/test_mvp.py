import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app import db, ingest, openehr, attachments, llm


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(db, 'DB_PATH', self.root/'test.db'),
                        patch.object(db, 'UPLOAD_DIR', self.root/'uploads'),
                        patch.object(attachments, 'UPLOAD_DIR', self.root/'uploads')]
        for p in self.patches: p.start()
        db.init_db()
        with db.get_db() as conn:
            for source, ext in [('hospital','H001'),('examination','E001'),('followup','C001')]:
                ingest.bind(conn,source,ext,1)

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def load(self, conn, source):
        return ingest.import_csv(conn,source,source+'.csv',(db.BASE_DIR/'examples'/ (source+'.csv')).read_bytes())

    def test_three_sources_idempotency_context_and_persistence(self):
        with db.get_db() as conn:
            for source in ['hospital','examination','followup']:
                self.assertEqual(self.load(conn,source)['imported'],1)
                self.assertEqual(self.load(conn,source)['duplicate'],1)
            records = ingest.timeline(conn,1)
            self.assertEqual(len([r for r in records if r['source']!='manual']),3)
            self.assertEqual(len(llm.assemble_context(conn,1)['structured_records']),len(records))
        with db.get_db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM record_origins').fetchone()[0],3)
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute('SELECT 1')

    def test_errors_conflicts_and_partial_success(self):
        text='person_id,exam_id,exam_date,sbp,dbp,temp\nE001,A,2026/10/01,120,80,36\nE001,B,bad,120,80,36\nUNKNOWN,C,2026/10/01,120,80,36\nE001,D,2026/10/01,nan,80,36\n'
        with db.get_db() as conn:
            result=ingest.import_csv(conn,'examination','mixed.csv',text.encode())
            self.assertEqual((result['imported'],result['failed']),(1,3))
            result=ingest.import_csv(conn,'examination','conflict.csv',text.replace('120','121').encode())
            self.assertEqual(result['failed'],4)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM record_origins').fetchone()[0],1)
            with self.assertRaises(ValueError): ingest.bind(conn,'hospital','H001',2)

    def test_unit_conversion_normalization_and_times(self):
        configured=ingest.rules()
        configured['examination']['fields']['blood_glucose']='glu'
        configured['examination']['metrics']['temperature']={'code':'TEMP','source_unit':'°F'}
        configured['examination']['metrics']['blood_glucose']={'code':'GLU','source_unit':'mg/dL'}
        text='person_id,exam_id,exam_date,sbp,dbp,temp,glu\nE001,M001,2026/10/01,120,80,98.6,90\n'
        with patch.object(ingest,'rules',return_value=configured), db.get_db() as conn:
            result=ingest.import_csv(conn,'examination','unit.csv',text.encode())
            self.assertEqual(result['imported'],1)
            record=next(r for r in ingest.timeline(conn,1) if r['source']=='examination')
            items={i['key']:i['value'] for i in record['items']}
            self.assertEqual(items['temperature'],'37.0')
            self.assertEqual(items['blood_glucose'],'5.0')
            temp=record['normalization']['temperature']
            self.assertEqual(temp['metric_code'],'TEMP')
            self.assertEqual((temp['raw_value'],temp['raw_unit'],temp['standard_value'],temp['standard_unit']),('98.6','°F','37.0','℃'))
            self.assertEqual(temp['plausible'],'ok')
            self.assertEqual(record['normalization']['blood_glucose']['raw_unit'],'mg/dL')
            self.assertEqual(record['observed_at'],'2026-10-01T00:00:00')
            self.assertTrue(record['recorded_at'])
            origin=conn.execute('SELECT * FROM record_origins WHERE external_record_id=?',('M001',)).fetchone()
            self.assertTrue(origin['recorded_at']);self.assertTrue(origin['updated_at'])
            again=ingest.import_csv(conn,'examination','unit.csv',text.encode())
            self.assertEqual(again['duplicate'],1)
            record=next(r for r in ingest.timeline(conn,1) if r['source']=='examination')
            self.assertEqual(record['normalization']['temperature']['standard_value'],'37.0')

    def test_normalization_errors_and_rule_validation(self):
        good=ingest.rules()
        with patch.object(ingest,'rules',return_value=good):
            self.assertTrue(ingest.validate_rules())
        bad=ingest.rules()
        bad['examination']['metrics']['temperature']={'code':'TEMP','source_unit':'kg'}
        with patch.object(ingest,'rules',return_value=bad):
            with self.assertRaises(ValueError):
                ingest.validate_rules()
        with patch.object(ingest,'rules',return_value=bad), db.get_db() as conn:
            text='person_id,exam_id,exam_date,sbp,dbp,temp\nE001,BAD1,2026/10/01,120,80,36.5\n'
            result=ingest.import_csv(conn,'examination','badunit.csv',text.encode())
            self.assertEqual(result['failed'],1)
            self.assertIn('不支持单位',result['errors'][0]['error'])
        bad=ingest.rules()
        bad['examination']['metrics']['temperature']={'code':'BP.SYS','source_unit':'℃'}
        with patch.object(ingest,'rules',return_value=bad), db.get_db() as conn:
            text='person_id,exam_id,exam_date,sbp,dbp,temp\nE001,BAD2,2026/10/01,120,80,36.5\n'
            result=ingest.import_csv(conn,'examination','badcode.csv',text.encode())
            self.assertEqual(result['failed'],1)
            self.assertIn('指标编码与数据字典不一致',result['errors'][0]['error'])
        with patch.object(ingest,'rules',return_value=good), db.get_db() as conn:
            text='person_id,exam_id,exam_date,sbp,dbp,temp\nE001,BAD3,2026/10/01,120,80,abc\n'
            result=ingest.import_csv(conn,'examination','badvalue.csv',text.encode())
            self.assertEqual(result['failed'],1)
            self.assertIn('不是有效数字',result['errors'][0]['error'])

    def test_exchange_versions_files_legacy_encounters_and_provenance(self):
        with db.get_db() as conn:
            self.load(conn,'hospital')
            uid=conn.execute('SELECT composition_uid FROM record_origins').fetchone()[0]
            openehr.update_composition(conn,uid,{'diagnosis':'修订记录'})
            aid=attachments.save_attachment(conn,1,'中文报告.txt','虚构附件内容'.encode())
            conn.execute('UPDATE attachments SET composition_uid=? WHERE id=?',(uid,aid))
            exported=openehr.export_ehr(conn,1)
            patient=openehr.import_ehr(conn,exported)
            restored=openehr.export_ehr(conn,patient)
            self.assertEqual(len(restored['encounters']),len(exported['encounters']))
            self.assertEqual(restored['attachments'][0]['sha256'],exported['attachments'][0]['sha256'])
            self.assertEqual(restored['attachments'][0]['content_base64'],exported['attachments'][0]['content_base64'])
            self.assertEqual([c['versions'] for c in restored['compositions']],[c['versions'] for c in exported['compositions']])
            self.assertTrue(any(r['source']=='hospital' for r in ingest.timeline(conn,patient)))
            bad=copy.deepcopy(exported);bad['compositions'][0]['versions']=[]
            count=conn.execute('SELECT COUNT(*) FROM patients').fetchone()[0]
            with self.assertRaises(ValueError): openehr.import_ehr(conn,bad)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM patients').fetchone()[0],count)

    def test_remote_disabled_by_default(self):
        with patch.dict(os.environ,{'EHR_ALLOW_REMOTE_LLM':'0','EHR_LLM_BASE_URL':'https://example.invalid','EHR_LLM_MODEL':'test'}):
            self.assertIsNone(llm._call_remote('test','test'))


if __name__ == '__main__': unittest.main()
