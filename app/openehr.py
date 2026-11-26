"""简化的 OpenEHR 风格数据模型。

双层建模：参考模型（通用结构：EHR / COMPOSITION / OBSERVATION）保存在本模块，
临床模板（archetype/template，针对老年康养场景定制）保存在 ``TEMPLATES``，
并以 JSON 持久化到 ``clinical_templates`` 表。所有函数接收外部传入的 sqlite 连接，
避免与 db 模块形成循环依赖。
"""

from datetime import datetime
from uuid import uuid4
import json
import base64
import hashlib
from pathlib import Path


REFERENCE_MODEL = {
    "name": "openEHR Reference Model (simplified)",
    "version": "1.0",
    "root": "EHR",
    "classes": {
        "EHR": ["ehr_id", "subject", "status", "compositions"],
        "COMPOSITION": ["uid", "template_id", "name", "archetype_id", "language", "composer", "committed_at", "content"],
        "OBSERVATION": ["archetype_node_id", "name", "items"],
        "ITEM": ["key", "label", "value", "unit"],
    },
}

TEMPLATES = {
    "vital_signs.v1": {
        "template_id": "vital_signs.v1",
        "name": "生命体征观测",
        "archetype_id": "openEHR-EHR-OBSERVATION.vital_signs.v1",
        "description": "老年康养场景常规生命体征观测（OBSERVATION）",
        "fields": [
            {"key": "systolic", "label": "收缩压", "unit": "mmHg", "kind": "number"},
            {"key": "diastolic", "label": "舒张压", "unit": "mmHg", "kind": "number"},
            {"key": "heart_rate", "label": "心率", "unit": "次/分", "kind": "number"},
            {"key": "temperature", "label": "体温", "unit": "℃", "kind": "number"},
            {"key": "spo2", "label": "血氧饱和度", "unit": "%", "kind": "number"},
            {"key": "blood_glucose", "label": "血糖", "unit": "mmol/L", "kind": "number"},
            {"key": "weight", "label": "体重", "unit": "kg", "kind": "number"},
        ],
    },
    "general_info.v1": {
        "template_id": "general_info.v1",
        "name": "康养基础信息",
        "archetype_id": "openEHR-EHR-EVALUATION.general_info.v1",
        "description": "老年人康养基础评估信息（EVALUATION）",
        "fields": [
            {"key": "height", "label": "身高", "unit": "cm", "kind": "number"},
            {"key": "blood_type", "label": "血型", "unit": "", "kind": "text"},
            {"key": "allergy", "label": "过敏史", "unit": "", "kind": "text"},
            {"key": "chronic", "label": "慢性病史", "unit": "", "kind": "text"},
            {"key": "self_care", "label": "生活自理能力", "unit": "", "kind": "text"},
        ],
    },
    "encounter.v1": {
        "template_id": "encounter.v1",
        "name": "就诊记录",
        "archetype_id": "openEHR-EHR-COMPOSITION.encounter.v1",
        "description": "门诊/住院/随访等就诊事件记录",
        "fields": [
            {"key": "encounter_type", "label": "就诊类型", "unit": "", "kind": "text"},
            {"key": "complaint", "label": "主诉", "unit": "", "kind": "text"},
            {"key": "diagnosis", "label": "诊断", "unit": "", "kind": "text"},
            {"key": "treatment", "label": "处理/医嘱", "unit": "", "kind": "text"},
            {"key": "notes", "label": "备注", "unit": "", "kind": "text"},
        ],
    },
}


def _now():
    return datetime.now().isoformat(timespec="seconds")


def seed_templates(db):
    for tid, tmpl in TEMPLATES.items():
        db.execute(
            "INSERT OR REPLACE INTO clinical_templates (template_id,name,archetype_id,description,definition) VALUES (?,?,?,?,?)",
            (tid, tmpl["name"], tmpl["archetype_id"], tmpl["description"], json.dumps(tmpl, ensure_ascii=False)),
        )


def get_templates():
    return [TEMPLATES[tid] for tid in TEMPLATES]


def ensure_ehr(db, patient_id):
    row = db.execute("SELECT ehr_id FROM ehrs WHERE patient_id=?", (patient_id,)).fetchone()
    if row is not None:
        return row["ehr_id"]
    ehr_id = uuid4().hex
    db.execute(
        "INSERT INTO ehrs (ehr_id,patient_id,status,created_at) VALUES (?,?,?,?)",
        (ehr_id, patient_id, "active", _now()),
    )
    return ehr_id


def _build_items(template, values):
    items = []
    for field in template["fields"]:
        value = str(values.get(field["key"], "")).strip()
        if value == "":
            continue
        items.append({
            "key": field["key"],
            "label": field["label"],
            "unit": field.get("unit") or None,
            "kind": field.get("kind", "text"),
            "value": value,
        })
    return items


def _compose_data(template, values):
    return {
        "_type": "COMPOSITION",
        "template_id": template["template_id"],
        "name": template["name"],
        "archetype_id": template["archetype_id"],
        "content": [{
            "_type": "OBSERVATION" if template["template_id"] == "vital_signs.v1" else "EVALUATION",
            "archetype_node_id": template["archetype_id"],
            "name": template["name"],
            "items": _build_items(template, values),
        }],
    }


def create_composition(db, patient_id, template_id, values, composer="系统录入", note="", extra=None):
    template = TEMPLATES.get(template_id)
    if template is None:
        raise ValueError("未知的临床模板: %s" % template_id)
    ehr_id = ensure_ehr(db, patient_id)
    uid = uuid4().hex
    ts = _now()
    data = _compose_data(template, values)
    if extra:
        data.update(extra)
    audit = {"composer": composer or "系统录入", "note": note or "", "committed_at": ts}
    db.execute(
        "INSERT INTO compositions (uid,ehr_id,template_id,name,current_version,data,committed_at) VALUES (?,?,?,?,?,?,?)",
        (uid, ehr_id, template_id, template["name"], 1, json.dumps(data, ensure_ascii=False), ts),
    )
    db.execute(
        "INSERT INTO composition_versions (composition_uid,version,data,audit,committed_at) VALUES (?,?,?,?,?)",
        (uid, 1, json.dumps(data, ensure_ascii=False), json.dumps(audit, ensure_ascii=False), ts),
    )
    return uid


def update_composition(db, uid, values, composer="系统录入", note=""):
    row = db.execute("SELECT * FROM compositions WHERE uid=?", (uid,)).fetchone()
    if row is None:
        raise ValueError("记录不存在: %s" % uid)
    template = TEMPLATES.get(row["template_id"])
    if template is None:
        raise ValueError("模板缺失: %s" % row["template_id"])
    version = row["current_version"] + 1
    ts = _now()
    data = _compose_data(template, values)
    previous = json.loads(row["data"])
    for key in ("provenance", "normalization"):
        if previous.get(key):
            data[key] = previous[key]
    audit = {"composer": composer or "系统录入", "note": note or "", "committed_at": ts}
    db.execute(
        "UPDATE compositions SET current_version=?, data=?, committed_at=? WHERE uid=?",
        (version, json.dumps(data, ensure_ascii=False), ts, uid),
    )
    db.execute(
        "INSERT INTO composition_versions (composition_uid,version,data,audit,committed_at) VALUES (?,?,?,?,?)",
        (uid, version, json.dumps(data, ensure_ascii=False), json.dumps(audit, ensure_ascii=False), ts),
    )
    return version


def _decode_composition(row, db):
    data = json.loads(row["data"])
    version_count = db.execute(
        "SELECT COUNT(*) FROM composition_versions WHERE composition_uid=?", (row["uid"],)
    ).fetchone()[0]
    return {
        "uid": row["uid"],
        "template_id": row["template_id"],
        "name": row["name"],
        "current_version": row["current_version"],
        "committed_at": row["committed_at"],
        "version_count": version_count,
        "data": data,
        "entries": data["content"][0]["items"],
    }


def list_compositions(db, patient_id):
    ehr = db.execute("SELECT ehr_id FROM ehrs WHERE patient_id=?", (patient_id,)).fetchone()
    if ehr is None:
        return []
    rows = db.execute(
        "SELECT * FROM compositions WHERE ehr_id=? ORDER BY committed_at DESC, rowid DESC",
        (ehr["ehr_id"],),
    ).fetchall()
    return [_decode_composition(row, db) for row in rows]


def get_composition(db, uid):
    row = db.execute("SELECT * FROM compositions WHERE uid=?", (uid,)).fetchone()
    return _decode_composition(row, db) if row is not None else None


def composition_history(db, uid):
    rows = db.execute(
        "SELECT version,data,audit,committed_at FROM composition_versions WHERE composition_uid=? ORDER BY version DESC",
        (uid,),
    ).fetchall()
    history = []
    for row in rows:
        history.append({
            "version": row["version"],
            "committed_at": row["committed_at"],
            "audit": json.loads(row["audit"]),
            "entries": json.loads(row["data"])["content"][0]["items"],
        })
    return history


def composition_form_fields(db, composition):
    template = TEMPLATES.get(composition["template_id"])
    if template is None:
        return []
    current = {item["key"]: item["value"] for item in composition["entries"]}
    fields = []
    for field in template["fields"]:
        row = dict(field)
        row["value"] = current.get(field["key"], "")
        fields.append(row)
    return fields


def export_ehr(db, patient_id):
    subject = db.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
    if subject is None:
        raise ValueError("患者不存在: %s" % patient_id)
    ehr = db.execute("SELECT * FROM ehrs WHERE patient_id=?", (patient_id,)).fetchone()
    compositions = []
    if ehr is not None:
        for row in db.execute("SELECT * FROM compositions WHERE ehr_id=? ORDER BY committed_at", (ehr["ehr_id"],)).fetchall():
            compositions.append({
                "uid": row["uid"],
                "template_id": row["template_id"],
                "name": row["name"],
                "current_version": row["current_version"],
                "versions": [
                    {"version": v["version"], "committed_at": v["committed_at"], "audit": json.loads(v["audit"]), "data": json.loads(v["data"])}
                    for v in db.execute(
                        "SELECT version,data,audit,committed_at FROM composition_versions WHERE composition_uid=? ORDER BY version",
                        (row["uid"],),
                    ).fetchall()
                ],
            })
    for comp in compositions:
        origin = db.execute('SELECT * FROM record_origins WHERE composition_uid=?', (comp['uid'],)).fetchone()
        if origin:
            provenance = dict(origin)
            raw = db.execute('SELECT r.*,j.mapping_json,j.filename FROM raw_records r JOIN ingest_jobs j ON j.id=r.job_id WHERE r.id=?', (origin['raw_record_id'],)).fetchone()
            if raw:
                provenance['original_record'] = dict(raw)
            for version in comp['versions']:
                version['data']['provenance'] = provenance
    attachments = []
    for row in db.execute(
        "SELECT * FROM attachments WHERE patient_id=? ORDER BY id",
        (patient_id,),
    ).fetchall():
        item = dict(row)
        item["content_base64"] = base64.b64encode(Path(item.pop("stored_path")).read_bytes()).decode("ascii")
        item.pop("id", None)
        item.pop("patient_id", None)
        attachments.append(item)
    return {
        "_type": "EHR_EXPORT",
        "format": "openEHR-simplified/1.0",
        "exported_at": _now(),
        "subject": {
            "patient_no": subject["patient_no"],
            "name": subject["name"],
            "gender": subject["gender"],
            "birth_date": subject["birth_date"],
            "phone": subject["phone"],
            "address": subject["address"],
            "notes": subject["notes"],
        },
        "ehr": {"ehr_id": ehr["ehr_id"] if ehr else None, "status": ehr["status"] if ehr else None},
        "compositions": compositions,
        "attachments": attachments,
        "encounters": [dict(r) for r in db.execute("SELECT * FROM encounters WHERE patient_id=?", (patient_id,))],
    }


def import_ehr(db, bundle):
    from .attachments import save_attachment
    if not isinstance(bundle, dict) or bundle.get('format') != 'openEHR-simplified/1.0':
        raise ValueError('不支持的档案格式')
    subject = bundle.get('subject')
    if not isinstance(subject, dict) or not isinstance(subject.get('name'), str) or not subject['name'].strip():
        raise ValueError('缺少患者姓名')
    # Validate the entire bundle before writing database rows or attachment files.
    prepared = []
    attachments = []
    try:
        for comp in bundle.get('compositions', []):
            versions = comp['versions']
            if not versions:
                raise ValueError('记录缺少版本，导入已取消')
            parsed = []
            for v in versions:
                data = json.loads(v['data']) if isinstance(v['data'], str) else v['data']
                audit = json.loads(v['audit']) if isinstance(v.get('audit'), str) else v.get('audit', {})
                if not isinstance(data, dict) or not isinstance(data['content'][0]['items'], list):
                    raise ValueError('记录内容格式不正确')
                number = int(v['version'])
                if number < 1:
                    raise ValueError('版本号必须为正整数')
                parsed.append((number, data, audit, v['committed_at']))
            parsed.sort(key=lambda v:v[0])
            if len({v[0] for v in parsed}) != len(parsed):
                raise ValueError('重复版本号')
            prepared.append((comp, parsed))
        for a in bundle.get('attachments', []):
            if 'content_base64' not in a:
                raise ValueError('附件缺少文件内容，不能无损导入')
            content = base64.b64decode(a['content_base64'], validate=True)
            if len(content) != a['size'] or hashlib.sha256(content).hexdigest() != a['sha256']:
                raise ValueError('附件大小或校验和不匹配')
            attachments.append((a,content))
        encounters = bundle.get('encounters', [])
        for e in encounters:
            datetime.fromisoformat(e['encounter_date'])
            if not e.get('encounter_type'):
                raise ValueError('就诊类型缺失')
    except (KeyError, TypeError, IndexError, AttributeError) as exc:
        raise ValueError('档案结构不完整，未导入') from exc
    patient_no = str(subject.get('patient_no') or '').strip()
    if not patient_no or db.execute('SELECT 1 FROM patients WHERE patient_no=?',(patient_no,)).fetchone():
        patient_no = 'IMP' + uuid4().hex[:12].upper()
    ts = _now()
    patient_id = db.execute(
        'INSERT INTO patients(patient_no,name,gender,birth_date,phone,address,notes,created_at) VALUES (?,?,?,?,?,?,?,?)',
        (patient_no,subject['name'],subject.get('gender') or '其他',subject.get('birth_date'),subject.get('phone'),subject.get('address'),subject.get('notes'),ts)).lastrowid
    ehr_id = ensure_ehr(db,patient_id)
    uid_map = {}
    for comp,versions in prepared:
        uid = uuid4().hex
        uid_map[comp['uid']] = uid
        latest = versions[-1]
        db.execute('INSERT INTO compositions VALUES (?,?,?,?,?,?,?)',(uid,ehr_id,comp['template_id'],comp['name'],latest[0],json.dumps(latest[1],ensure_ascii=False),latest[3]))
        for number,data,audit,committed in versions:
            db.execute('INSERT INTO composition_versions VALUES (?,?,?,?,?)',(uid,number,json.dumps(data,ensure_ascii=False),json.dumps(audit,ensure_ascii=False),committed))
    for e in encounters:
        db.execute('INSERT INTO encounters(patient_id,encounter_date,encounter_type,complaint,diagnosis,treatment,notes,created_at) VALUES (?,?,?,?,?,?,?,?)',
                   (patient_id,e['encounter_date'],e['encounter_type'],e.get('complaint'),e.get('diagnosis'),e.get('treatment'),e.get('notes'),e.get('created_at') or ts))
    created_paths = []
    try:
        for a,content in attachments:
            aid = save_attachment(db,patient_id,a['filename'],content,a.get('content_type'),a.get('category'),a.get('description'))
            created_paths.append(Path(db.execute('SELECT stored_path FROM attachments WHERE id=?',(aid,)).fetchone()[0]))
            db.execute('UPDATE attachments SET extracted_text=?,uploaded_at=?,composition_uid=? WHERE id=?',
                       (a.get('extracted_text'),a.get('uploaded_at') or ts,uid_map.get(a.get('composition_uid')),aid))
    except Exception:
        for path in created_paths:
            path.unlink(missing_ok=True)
        raise
    return patient_id
