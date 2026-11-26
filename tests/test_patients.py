import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db, patients


class PatientServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(db, 'DB_PATH', self.root / 'test.db'),
                        patch.object(db, 'UPLOAD_DIR', self.root / 'uploads')]
        for p in self.patches:
            p.start()
        db.init_db()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_validation_rejects_bad_input(self):
        cases = [
            ('', '张三', '男', ''),
            ('A001', '', '男', ''),
            ('A001', '张三', '机器人', ''),
            ('A001', '张三', '男', '2026/01/01'),
            ('A001', '张三', '男', '2999-01-01'),
        ]
        for patient_no, name, gender, birth_date in cases:
            with self.subTest(patient_no=patient_no, name=name, gender=gender, birth_date=birth_date):
                with self.assertRaises(ValueError):
                    patients.validate_patient(patient_no, name, gender, birth_date)
        with self.assertRaises(ValueError):
            patients.validate_encounter('bad', '门诊')
        with self.assertRaises(ValueError):
            patients.parse_filters(created_from='2026-05-01', created_to='2026-01-01')

    def test_create_update_and_encounter_maintenance(self):
        with db.get_db() as conn:
            pid = patients.create_patient(conn, patients.validate_patient('A001', '张三', '男', '1950-01-01'))
            patients.set_status(conn, pid, 'inactive')
            self.assertEqual(patients.get_patient(conn, pid)['status'], 'inactive')
            active = patients.list_patients(conn, patients.parse_filters(q='A001', status='active'))
            self.assertEqual(len(active), 0)
            inactive = patients.list_patients(conn, patients.parse_filters(q='A001', status='inactive'))
            self.assertEqual([r['patient_no'] for r in inactive], ['A001'])

            patients.update_patient(conn, pid, patients.validate_patient('A001', '张三改', '女', '1950-01-01', phone='13800000000'))
            row = patients.get_patient(conn, pid)
            self.assertEqual((row['name'], row['gender']), ('张三改', '女'))
            self.assertIsNotNone(row['updated_at'])

            patients.create_encounter(conn, pid, patients.validate_encounter('2026-01-01', '门诊', complaint='头晕'))
            eid = conn.execute('SELECT id FROM encounters WHERE patient_id=?', (pid,)).fetchone()[0]
            patients.update_encounter(conn, pid, eid, patients.validate_encounter('2026-01-02', '体检', diagnosis='稳定'))
            row = conn.execute('SELECT * FROM encounters WHERE id=?', (eid,)).fetchone()
            self.assertEqual((row['encounter_date'], row['diagnosis']), ('2026-01-02', '稳定'))
            self.assertIsNotNone(row['updated_at'])

    def test_encounter_cannot_be_edited_through_another_patient(self):
        with db.get_db() as conn:
            owner = patients.create_patient(conn, patients.validate_patient('A001', '张三', '男'))
            other = patients.create_patient(conn, patients.validate_patient('A002', '李四', '女'))
            patients.create_encounter(conn, owner, patients.validate_encounter('2026-01-01', '门诊'))
            eid = conn.execute('SELECT id FROM encounters WHERE patient_id=?', (owner,)).fetchone()[0]
            with self.assertRaises(ValueError):
                patients.update_encounter(conn, other, eid, patients.validate_encounter('2026-01-01', '门诊'))

    def test_search_escapes_wildcards(self):
        with db.get_db() as conn:
            patients.create_patient(conn, patients.validate_patient('A100', '张三', '男'))
            patients.create_patient(conn, patients.validate_patient('A200', '李四', '女'))
            found = patients.list_patients(conn, patients.parse_filters(q='%', status='all'))
            self.assertEqual(len(found), 0)
            found = patients.list_patients(conn, patients.parse_filters(q='A1', status='all'))
            self.assertEqual([r['patient_no'] for r in found], ['A100'])


class LegacyMigrationTests(unittest.TestCase):
    def test_legacy_patient_and_encounter_tables_are_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'legacy.db'
            conn = sqlite3.connect(path)
            conn.execute(
                'CREATE TABLE patients (id INTEGER PRIMARY KEY AUTOINCREMENT, patient_no TEXT NOT NULL UNIQUE, '
                'name TEXT NOT NULL, gender TEXT NOT NULL, birth_date TEXT, phone TEXT, address TEXT, notes TEXT, created_at TEXT NOT NULL)'
            )
            conn.execute(
                'CREATE TABLE encounters (id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id INTEGER NOT NULL, '
                'encounter_date TEXT NOT NULL, encounter_type TEXT NOT NULL, complaint TEXT, diagnosis TEXT, '
                'treatment TEXT, notes TEXT, created_at TEXT NOT NULL)'
            )
            conn.execute("INSERT INTO patients(patient_no,name,gender,created_at) VALUES ('L001','旧档案','男','2026-01-01T00:00:00')")
            conn.commit()
            conn.close()
            with patch.object(db, 'DB_PATH', path), patch.object(db, 'UPLOAD_DIR', root / 'uploads'):
                db.init_db()
                with db.get_db() as migrated:
                    row = migrated.execute("SELECT * FROM patients WHERE patient_no='L001'").fetchone()
                    self.assertEqual(row['status'], 'active')
                    self.assertIsNone(row['updated_at'])
                    cols = {r['name'] for r in migrated.execute('PRAGMA table_info(encounters)')}
                    self.assertIn('updated_at', cols)


if __name__ == '__main__':
    unittest.main()
