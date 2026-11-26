from contextlib import contextmanager
import os
from datetime import datetime
from pathlib import Path
import sqlite3


BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("EHR_DB_PATH", str(BASE_DIR / "health_records.db")))
UPLOAD_DIR = Path(os.environ.get("EHR_UPLOAD_DIR", str(BASE_DIR / "data" / "uploads")))


def now():
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_no TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    gender TEXT NOT NULL,
    birth_date TEXT,
    phone TEXT,
    address TEXT,
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS encounters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL,
    encounter_date TEXT NOT NULL,
    encounter_type TEXT NOT NULL,
    complaint TEXT,
    diagnosis TEXT,
    treatment TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    FOREIGN KEY(patient_id) REFERENCES patients(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS ehrs (
    ehr_id TEXT PRIMARY KEY,
    patient_id INTEGER NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    FOREIGN KEY(patient_id) REFERENCES patients(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS clinical_templates (
    template_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    archetype_id TEXT,
    description TEXT,
    definition TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS compositions (
    uid TEXT PRIMARY KEY,
    ehr_id TEXT NOT NULL,
    template_id TEXT NOT NULL,
    name TEXT NOT NULL,
    current_version INTEGER NOT NULL,
    data TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    FOREIGN KEY(ehr_id) REFERENCES ehrs(ehr_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS composition_versions (
    composition_uid TEXT NOT NULL,
    version INTEGER NOT NULL,
    data TEXT NOT NULL,
    audit TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    PRIMARY KEY (composition_uid, version),
    FOREIGN KEY(composition_uid) REFERENCES compositions(uid) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL,
    composition_uid TEXT,
    filename TEXT NOT NULL,
    content_type TEXT,
    size INTEGER NOT NULL,
    sha256 TEXT,
    category TEXT,
    description TEXT,
    stored_path TEXT NOT NULL,
    extracted_text TEXT,
    uploaded_at TEXT NOT NULL,
    FOREIGN KEY(patient_id) REFERENCES patients(id) ON DELETE CASCADE
);
"""


def _migrate(db):
    """为旧数据库补充新增列，保证升级时不丢失既有数据。"""
    patient_cols = {row["name"] for row in db.execute("PRAGMA table_info(patients)")}
    if "status" not in patient_cols:
        db.execute("ALTER TABLE patients ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
    if "updated_at" not in patient_cols:
        db.execute("ALTER TABLE patients ADD COLUMN updated_at TEXT")
    encounter_cols = {row["name"] for row in db.execute("PRAGMA table_info(encounters)")}
    if "updated_at" not in encounter_cols:
        db.execute("ALTER TABLE encounters ADD COLUMN updated_at TEXT")


def init_db():
    from . import openehr

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.executescript(SCHEMA)
        _migrate(db)
        from .ingest import init_ingest
        init_ingest(db)
        openehr.seed_templates(db)
        if db.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 0:
            _seed_demo(db)
        for row in db.execute("SELECT id FROM patients").fetchall():
            openehr.ensure_ehr(db, row["id"])
        if db.execute("SELECT COUNT(*) FROM compositions").fetchone()[0] == 0:
            _seed_compositions(db, openehr)


def _seed_compositions(db, openehr):
    first = db.execute("SELECT id FROM patients ORDER BY id").fetchone()
    if first is not None:
        openehr.create_composition(
            db, first["id"], "vital_signs.v1",
            {"systolic": "142", "diastolic": "88", "heart_rate": "76", "temperature": "36.5", "spo2": "97"},
            composer="示例数据", note="晨间测量",
        )


def _seed_demo(db):
    ts = now()
    db.execute(
        "INSERT INTO patients (patient_no,name,gender,birth_date,phone,address,notes,created_at) VALUES (?,?,?,?,?,?,?,?)",
        ("P2026001", "李明", "男", "1965-04-12", "13800000001", "济南市历下区", "示例档案", ts),
    )
    patient_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute(
        "INSERT INTO encounters (patient_id,encounter_date,encounter_type,complaint,diagnosis,treatment,notes,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (patient_id, "2026-09-18", "门诊", "近期血压偏高", "原发性高血压（待复诊确认）", "建议规律监测血压，低盐饮食", "示例就诊记录", ts),
    )
    db.execute(
        "INSERT INTO patients (patient_no,name,gender,birth_date,phone,address,notes,created_at) VALUES (?,?,?,?,?,?,?,?)",
        ("P2026002", "王芳", "女", "1972-11-03", "13800000002", "济南市市中区", "示例档案", ts),
    )
