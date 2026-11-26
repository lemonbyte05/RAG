"""Small, deterministic CSV pipeline. Identity links must be explicitly confirmed."""
import csv
import hashlib
import io
import json
import math
from datetime import datetime
from pathlib import Path
from . import openehr
from .db import BASE_DIR, now


def rules():
    return json.loads((BASE_DIR / 'config/mappings.json').read_text(encoding='utf-8'))


def init_ingest(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS source_systems (id TEXT PRIMARY KEY, name TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS patient_external_ids (
      source_id TEXT NOT NULL REFERENCES source_systems(id), external_id TEXT NOT NULL,
      patient_id INTEGER NOT NULL REFERENCES patients(id), PRIMARY KEY(source_id, external_id));
    CREATE TABLE IF NOT EXISTS ingest_jobs (
      id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, filename TEXT NOT NULL,
      mapping_json TEXT NOT NULL, created_at TEXT NOT NULL, result TEXT);
    CREATE TABLE IF NOT EXISTS raw_records (
      id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES ingest_jobs(id),
      line_number INTEGER NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
      status TEXT NOT NULL, error TEXT, composition_uid TEXT);
    CREATE TABLE IF NOT EXISTS record_origins (
      composition_uid TEXT PRIMARY KEY REFERENCES compositions(uid),
      source_id TEXT NOT NULL, external_id TEXT NOT NULL, external_record_id TEXT NOT NULL,
      observed_at TEXT NOT NULL, raw_record_id INTEGER REFERENCES raw_records(id),
      UNIQUE(source_id, external_record_id));
    ''')
    for key, rule in rules().items():
        db.execute('INSERT OR IGNORE INTO source_systems VALUES (?,?)', (key, rule['name']))


def bind(db, source, external_id, patient_id):
    if source not in rules() or not external_id.strip():
        raise ValueError('来源或外部患者编号无效')
    if not db.execute('SELECT 1 FROM patients WHERE id=?', (patient_id,)).fetchone():
        raise ValueError('患者不存在')
    old = db.execute('SELECT patient_id FROM patient_external_ids WHERE source_id=? AND external_id=?', (source, external_id.strip())).fetchone()
    if old and old[0] != patient_id:
        raise ValueError('此外部编号已绑定另一患者，不能覆盖')
    db.execute('INSERT OR IGNORE INTO patient_external_ids VALUES (?,?,?)', (source, external_id.strip(), patient_id))


def import_csv(db, source, filename, content):
    rule = rules().get(source)
    if not rule:
        raise ValueError('未知来源')
    try:
        reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig')), strict=True)
        headers = reader.fieldnames or []
        required = [rule[k] for k in ('patient_id', 'record_id', 'date')] + list(rule['fields'].values())
        if len(set(headers)) != len(headers) or any(h not in headers for h in required):
            raise ValueError('CSV 表头不符合所选来源的映射配置')
        rows = []
        for row in reader:
            rows.append((reader.line_num, row))
            if len(rows) > 5000:
                raise ValueError('单次最多导入 5000 行')
    except (UnicodeError, csv.Error) as exc:
        raise ValueError('需要 UTF-8 编码、格式正确的 CSV') from exc
    if not rows:
        raise ValueError('CSV 没有数据行')
    job = db.execute('INSERT INTO ingest_jobs(source_id,filename,mapping_json,created_at) VALUES (?,?,?,?)',
                     (source, filename, json.dumps(rule, ensure_ascii=False), now())).lastrowid
    result = {'job_id': job, 'imported': 0, 'duplicate': 0, 'failed': 0, 'errors': []}
    for line, row in rows:
        payload = json.dumps(row, ensure_ascii=False, sort_keys=False)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        raw = db.execute('INSERT INTO raw_records(job_id,line_number,payload,digest,status) VALUES (?,?,?,?,?)',
                         (job, line, payload, digest, 'pending')).lastrowid
        db.execute('SAVEPOINT import_row')
        try:
            if None in row or any(v is None for v in row.values()):
                raise ValueError('列数与表头不一致')
            ext, rid = row[rule['patient_id']].strip(), row[rule['record_id']].strip()
            if not ext or not rid:
                raise ValueError('患者编号和记录编号不能为空')
            patient = db.execute('SELECT patient_id FROM patient_external_ids WHERE source_id=? AND external_id=?', (source, ext)).fetchone()
            if not patient:
                raise ValueError('外部患者编号尚未绑定：' + ext)
            old = db.execute('SELECT r.digest,o.composition_uid FROM record_origins o JOIN raw_records r ON r.id=o.raw_record_id WHERE o.source_id=? AND o.external_record_id=?', (source, rid)).fetchone()
            if old:
                if old['digest'] != digest:
                    raise ValueError('相同来源记录编号内容发生变化，请核查；未覆盖旧记录')
                status, uid = 'duplicate', old['composition_uid']
            else:
                observed = datetime.strptime(row[rule['date']].strip(), rule['date_format']).isoformat(timespec='seconds')
                values = {key: row[col].strip() for key, col in rule['fields'].items()}
                for field in openehr.TEMPLATES[rule['template']]['fields']:
                    key = field['key']
                    if field['kind'] == 'number' and values.get(key):
                        number = float(values[key]) * rule.get('factors', {}).get(key, 1)
                        if not math.isfinite(number):
                            raise ValueError('数值必须有限')
                        values[key] = format(number, '.6g')
                if not any(values.values()):
                    raise ValueError('记录没有有效内容')
                uid = openehr.create_composition(db, patient[0], rule['template'], values, composer=rule['name'], note='CSV 导入')
                db.execute('INSERT INTO record_origins VALUES (?,?,?,?,?,?)', (uid, source, ext, rid, observed, raw))
                status = 'imported'
            db.execute('UPDATE raw_records SET status=?,composition_uid=? WHERE id=?', (status, uid, raw))
            result[status] += 1
        except ValueError as exc:
            db.execute('ROLLBACK TO import_row')
            db.execute('UPDATE raw_records SET status=?,error=? WHERE id=?', ('failed', str(exc), raw))
            result['failed'] += 1
            result['errors'].append({'line': line, 'error': str(exc)})
        finally:
            db.execute('RELEASE import_row')
    db.execute('UPDATE ingest_jobs SET result=? WHERE id=?', (json.dumps(result, ensure_ascii=False), job))
    return result


def timeline(db, patient_id):
    if not db.execute('SELECT 1 FROM patients WHERE id=?', (patient_id,)).fetchone():
        raise ValueError('患者不存在')
    records = []
    for comp in openehr.list_compositions(db, patient_id):
        origin = db.execute('SELECT * FROM record_origins WHERE composition_uid=?', (comp['uid'],)).fetchone()
        provenance = dict(origin) if origin else comp['data'].get('provenance', {})
        records.append({'id': comp['uid'], 'name': comp['name'], 'observed_at': provenance.get('observed_at', comp['committed_at']),
                        'time_basis': 'event' if provenance.get('observed_at') else 'recorded',
                        'updated_at': comp['committed_at'], 'version': comp['current_version'],
                        'source': provenance.get('source_id', 'manual'), 'provenance': provenance, 'items': comp['entries']})
    for row in db.execute('SELECT * FROM encounters WHERE patient_id=?', (patient_id,)):
        records.append({'id': 'encounter-' + str(row['id']), 'name': '就诊记录', 'observed_at': row['encounter_date'],
                        'time_basis': 'event', 'updated_at': row['created_at'], 'version': 1, 'source': 'manual', 'provenance': {},
                        'items': [{'key': k, 'label': label, 'value': row[k], 'unit': None} for k, label in [('complaint','主诉'),('diagnosis','诊断'),('treatment','医嘱'),('notes','备注')] if row[k]]})
    return sorted(records, key=lambda r: (r['observed_at'], r['id']), reverse=True)
