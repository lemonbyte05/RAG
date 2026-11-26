"""Small, deterministic CSV pipeline. Identity links must be explicitly confirmed.

导入时通过 ``app.dictionary`` 把来源数值换算成统一标准单位，并在 COMPOSITION 的
``normalization`` 中保留原始值、原始单位、标准值和标准单位；原始行始终留在
``raw_records``，来源信息留在 ``record_origins``。
"""
import csv
import hashlib
import io
import json
from datetime import datetime
from . import dictionary, openehr
from .db import BASE_DIR, now


def rules():
    return json.loads((BASE_DIR / 'config/mappings.json').read_text(encoding='utf-8'))


def validate_rules(templates=None):
    """校验 mappings.json 与模板/数据字典一致，配置错误在启动时即暴露。"""
    templates = templates or openehr.TEMPLATES
    for source, rule in rules().items():
        if not isinstance(rule, dict):
            raise ValueError('来源 %s 的映射配置必须是对象' % source)
        template_id = rule.get('template')
        template = templates.get(template_id)
        if template is None:
            raise ValueError('来源 %s 使用了未知模板：%s' % (source, template_id))
        fields = {field['key']: field for field in template.get('fields', [])}
        for key, column in (rule.get('fields') or {}).items():
            if key not in fields:
                raise ValueError('来源 %s 的字段 %s 不在模板 %s 中' % (source, key, template_id))
            if not str(column or '').strip():
                raise ValueError('来源 %s 的字段 %s 缺少来源列名' % (source, key))
        for key, config in (rule.get('metrics') or {}).items():
            field = fields.get(key)
            if field is None or field.get('kind') != 'number':
                raise ValueError('来源 %s 的指标配置 %s 不是数值型模板字段' % (source, key))
            metric = dictionary.by_template_field(template_id, key)
            if metric is None:
                raise ValueError('来源 %s 的字段 %s 缺少统一指标定义' % (source, key))
            if not isinstance(config, dict):
                raise ValueError('来源 %s 的指标配置 %s 必须是对象' % (source, key))
            code = config.get('code') or metric['code']
            if code != metric['code']:
                raise ValueError('来源 %s 的字段 %s 指标编码 %s 与字典 %s 不一致' % (source, key, code, metric['code']))
            dictionary.resolve_unit(code, config.get('source_unit') or metric['standard_unit'])
    return True


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
      recorded_at TEXT, updated_at TEXT,
      UNIQUE(source_id, external_record_id));
    ''')
    columns = {row['name'] for row in db.execute('PRAGMA table_info(record_origins)')}
    if 'recorded_at' not in columns:
        db.execute('ALTER TABLE record_origins ADD COLUMN recorded_at TEXT')
    if 'updated_at' not in columns:
        db.execute('ALTER TABLE record_origins ADD COLUMN updated_at TEXT')
    for key, rule in rules().items():
        db.execute('INSERT OR IGNORE INTO source_systems VALUES (?,?)', (key, rule['name']))
    validate_rules()


def bind(db, source, external_id, patient_id):
    if source not in rules() or not external_id.strip():
        raise ValueError('来源或外部患者编号无效')
    if not db.execute('SELECT 1 FROM patients WHERE id=?', (patient_id,)).fetchone():
        raise ValueError('患者不存在')
    old = db.execute('SELECT patient_id FROM patient_external_ids WHERE source_id=? AND external_id=?', (source, external_id.strip())).fetchone()
    if old and old[0] != patient_id:
        raise ValueError('此外部编号已绑定另一患者，不能覆盖')
    db.execute('INSERT OR IGNORE INTO patient_external_ids VALUES (?,?,?)', (source, external_id.strip(), patient_id))


def _normalize_values(rule, row):
    """把来源行转换为模板字段值，返回 (values, normalization)。"""
    template = openehr.TEMPLATES[rule['template']]
    values = {key: row[column].strip() for key, column in rule['fields'].items()}
    normalization = {}
    for field in template.get('fields', []):
        key = field['key']
        if field.get('kind') != 'number' or not values.get(key):
            continue
        metric = dictionary.by_template_field(rule['template'], key)
        if metric is None:
            raise ValueError('字段 %s 缺少统一指标定义' % field.get('label', key))
        config = (rule.get('metrics') or {}).get(key) or {}
        if config.get('code') and config['code'] != metric['code']:
            raise ValueError('字段 %s 的指标编码与数据字典不一致' % field.get('label', key))
        unit = config.get('source_unit') or metric['standard_unit']
        raw_value = values[key]
        try:
            standard = dictionary.convert_to_standard(raw_value, metric['code'], unit)
        except ValueError as exc:
            raise ValueError('%s：%s' % (field.get('label', key), exc)) from exc
        values[key] = dictionary.format_standard(standard, metric['code'])
        normalization[key] = {
            'metric_code': metric['code'],
            'raw_value': raw_value,
            'raw_unit': unit,
            'standard_value': values[key],
            'standard_unit': metric['standard_unit'],
            'plausible': dictionary.plausible_status(standard, metric['code']),
        }
    return values, normalization


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
    recorded_at = now()
    job = db.execute('INSERT INTO ingest_jobs(source_id,filename,mapping_json,created_at) VALUES (?,?,?,?)',
                     (source, filename, json.dumps(rule, ensure_ascii=False), recorded_at)).lastrowid
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
                values, normalization = _normalize_values(rule, row)
                if not any(values.values()):
                    raise ValueError('记录没有有效内容')
                extra = {'normalization': normalization} if normalization else None
                uid = openehr.create_composition(db, patient[0], rule['template'], values, composer=rule['name'], note='CSV 导入', extra=extra)
                db.execute(
                    'INSERT INTO record_origins(composition_uid,source_id,external_id,external_record_id,observed_at,raw_record_id,recorded_at,updated_at) VALUES (?,?,?,?,?,?,?,?)',
                    (uid, source, ext, rid, observed, raw, recorded_at, recorded_at),
                )
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
        records.append({
            'id': comp['uid'], 'name': comp['name'],
            'observed_at': provenance.get('observed_at', comp['committed_at']),
            'recorded_at': provenance.get('recorded_at', comp['committed_at']),
            'updated_at': comp['committed_at'],
            'time_basis': 'event' if provenance.get('observed_at') else 'recorded',
            'version': comp['current_version'], 'source': provenance.get('source_id', 'manual'),
            'normalization': comp['data'].get('normalization'),
            'provenance': provenance, 'items': comp['entries'],
        })
    for row in db.execute('SELECT * FROM encounters WHERE patient_id=?', (patient_id,)):
        records.append({
            'id': 'encounter-' + str(row['id']), 'name': '就诊记录', 'observed_at': row['encounter_date'],
            'recorded_at': row['created_at'], 'updated_at': row['updated_at'] or row['created_at'],
            'time_basis': 'event', 'version': 1, 'source': 'manual', 'normalization': None,
            'provenance': {},
            'items': [{'key': k, 'label': label, 'value': row[k], 'unit': None} for k, label in [('complaint', '主诉'), ('diagnosis', '诊断'), ('treatment', '医嘱'), ('notes', '备注')] if row[k]],
        })
    return sorted(records, key=lambda r: (r['observed_at'], r['id']), reverse=True)
