"""患者档案与就诊记录的校验、查询和维护逻辑。

路由层只负责接收表单与渲染页面，具体的输入校验和数据库读写集中在此模块，
便于测试，也避免 ``main.py`` 继续膨胀。
"""

from datetime import date

from .db import now


GENDERS = ("男", "女", "其他")
STATUS_LABELS = {"active": "在用", "inactive": "已停用"}
STATUS_VALUES = ("active", "inactive", "all")
ENCOUNTER_TYPES = ("门诊", "住院", "体检", "电话随访")


def _clean(value, limit, label):
    text = (value or "").strip()
    if len(text) > limit:
        raise ValueError("%s长度不能超过 %d 个字符" % (label, limit))
    return text


def _clean_date(value, label):
    text = _clean(value, 10, label)
    if not text:
        return ""
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("%s格式应为 YYYY-MM-DD" % label) from exc
    return text


def validate_patient(patient_no, name, gender, birth_date="", phone="", address="", notes=""):
    patient_no = _clean(patient_no, 64, "患者编号")
    name = _clean(name, 64, "姓名")
    gender = _clean(gender, 8, "性别")
    birth_date = _clean_date(birth_date, "出生日期")
    phone = _clean(phone, 32, "联系电话")
    address = _clean(address, 200, "地址")
    notes = _clean(notes, 2000, "备注")
    if not patient_no or not name:
        raise ValueError("患者编号和姓名不能为空")
    if gender not in GENDERS:
        raise ValueError("性别取值无效")
    if birth_date and date.fromisoformat(birth_date) > date.today():
        raise ValueError("出生日期不能晚于今天")
    return {
        "patient_no": patient_no,
        "name": name,
        "gender": gender,
        "birth_date": birth_date or None,
        "phone": phone,
        "address": address,
        "notes": notes,
    }


def validate_encounter(encounter_date, encounter_type, complaint="", diagnosis="", treatment="", notes=""):
    encounter_date = _clean_date(encounter_date, "就诊日期")
    encounter_type = _clean(encounter_type, 32, "就诊类型")
    if not encounter_date or not encounter_type:
        raise ValueError("就诊日期和就诊类型不能为空")
    return {
        "encounter_date": encounter_date,
        "encounter_type": encounter_type,
        "complaint": _clean(complaint, 2000, "主诉"),
        "diagnosis": _clean(diagnosis, 2000, "诊断"),
        "treatment": _clean(treatment, 2000, "处理/医嘱"),
        "notes": _clean(notes, 2000, "备注"),
    }


def parse_filters(q="", gender="", status="active", created_from="", created_to=""):
    created_from = _clean_date(created_from, "创建开始日期")
    created_to = _clean_date(created_to, "创建结束日期")
    if created_from and created_to and created_from > created_to:
        raise ValueError("创建开始日期不能晚于结束日期")
    return {
        "q": _clean(q, 64, "搜索关键词"),
        "gender": gender if gender in GENDERS else "",
        "status": status if status in STATUS_VALUES else "active",
        "created_from": created_from,
        "created_to": created_to,
    }


def list_patients(db, filters):
    clauses, args = [], []
    if filters["q"]:
        escaped = filters["q"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("(p.name LIKE ? ESCAPE '\\' OR p.patient_no LIKE ? ESCAPE '\\')")
        args.extend(["%" + escaped + "%"] * 2)
    if filters["gender"]:
        clauses.append("p.gender=?")
        args.append(filters["gender"])
    if filters["status"] != "all":
        clauses.append("p.status=?")
        args.append(filters["status"])
    if filters["created_from"]:
        clauses.append("substr(p.created_at,1,10)>=?")
        args.append(filters["created_from"])
    if filters["created_to"]:
        clauses.append("substr(p.created_at,1,10)<=?")
        args.append(filters["created_to"])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT p.*, COUNT(e.id) AS encounter_count FROM patients p "
        "LEFT JOIN encounters e ON e.patient_id=p.id" + where +
        " GROUP BY p.id ORDER BY p.created_at DESC, p.id DESC"
    )
    return db.execute(sql, args).fetchall()


def get_patient(db, patient_id):
    return db.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()


def create_patient(db, fields):
    db.execute(
        "INSERT INTO patients (patient_no,name,gender,birth_date,phone,address,notes,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            fields["patient_no"], fields["name"], fields["gender"], fields["birth_date"],
            fields["phone"], fields["address"], fields["notes"], "active", now(),
        ),
    )
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def update_patient(db, patient_id, fields):
    cursor = db.execute(
        "UPDATE patients SET patient_no=?,name=?,gender=?,birth_date=?,phone=?,address=?,notes=?,updated_at=? WHERE id=?",
        (
            fields["patient_no"], fields["name"], fields["gender"], fields["birth_date"],
            fields["phone"], fields["address"], fields["notes"], now(), patient_id,
        ),
    )
    if cursor.rowcount == 0:
        raise ValueError("患者不存在或已删除")


def set_status(db, patient_id, status):
    if status not in ("active", "inactive"):
        raise ValueError("档案状态取值无效")
    cursor = db.execute(
        "UPDATE patients SET status=?, updated_at=? WHERE id=?",
        (status, now(), patient_id),
    )
    if cursor.rowcount == 0:
        raise ValueError("患者不存在或已删除")


def create_encounter(db, patient_id, fields):
    db.execute(
        "INSERT INTO encounters (patient_id,encounter_date,encounter_type,complaint,diagnosis,treatment,notes,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            patient_id, fields["encounter_date"], fields["encounter_type"], fields["complaint"],
            fields["diagnosis"], fields["treatment"], fields["notes"], now(),
        ),
    )


def update_encounter(db, patient_id, encounter_id, fields):
    cursor = db.execute(
        "UPDATE encounters SET encounter_date=?,encounter_type=?,complaint=?,diagnosis=?,treatment=?,notes=?,updated_at=? WHERE id=? AND patient_id=?",
        (
            fields["encounter_date"], fields["encounter_type"], fields["complaint"],
            fields["diagnosis"], fields["treatment"], fields["notes"], now(),
            encounter_id, patient_id,
        ),
    )
    if cursor.rowcount == 0:
        raise ValueError("就诊记录不存在或不属于该患者")
