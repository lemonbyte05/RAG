from datetime import date, datetime
from pathlib import Path
import sqlite3

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles


BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "health_records.db"
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app = FastAPI(title="电子健康档案 MVP")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with get_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS patients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_no TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                gender TEXT NOT NULL,
                birth_date TEXT,
                phone TEXT,
                address TEXT,
                notes TEXT,
                created_at TEXT NOT NULL
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
                FOREIGN KEY(patient_id) REFERENCES patients(id) ON DELETE CASCADE
            );
            """
        )
        if db.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 0:
            now = datetime.now().isoformat(timespec="seconds")
            db.execute(
                "INSERT INTO patients (patient_no,name,gender,birth_date,phone,address,notes,created_at) VALUES (?,?,?,?,?,?,?,?)",
                ("P2026001", "李明", "男", "1965-04-12", "13800000001", "济南市历下区", "示例档案", now),
            )
            patient_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.execute(
                "INSERT INTO encounters (patient_id,encounter_date,encounter_type,complaint,diagnosis,treatment,notes,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (patient_id, "2026-09-18", "门诊", "近期血压偏高", "原发性高血压（待复诊确认）", "建议规律监测血压，低盐饮食", "示例就诊记录", now),
            )
            db.execute(
                "INSERT INTO patients (patient_no,name,gender,birth_date,phone,address,notes,created_at) VALUES (?,?,?,?,?,?,?,?)",
                ("P2026002", "王芳", "女", "1972-11-03", "13800000002", "济南市市中区", "示例档案", now),
            )


@app.on_event("startup")
def startup():
    init_db()


@app.get("/", response_class=HTMLResponse)
def patient_list(request: Request):
    with get_db() as db:
        patients = db.execute(
            "SELECT p.*, COUNT(e.id) AS encounter_count FROM patients p LEFT JOIN encounters e ON e.patient_id=p.id GROUP BY p.id ORDER BY p.created_at DESC"
        ).fetchall()
    return templates.TemplateResponse("index.html", {"request": request, "patients": patients})


@app.get("/patients/{patient_id}", response_class=HTMLResponse)
def patient_detail(request: Request, patient_id: int):
    with get_db() as db:
        patient = db.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
        encounters = db.execute(
            "SELECT * FROM encounters WHERE patient_id=? ORDER BY encounter_date DESC, id DESC", (patient_id,)
        ).fetchall()
    if patient is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        "patient.html", {"request": request, "patient": patient, "encounters": encounters, "today": date.today().isoformat()}
    )


@app.post("/patients")
def create_patient(
    patient_no: str = Form(...), name: str = Form(...), gender: str = Form(...), birth_date: str = Form(""),
    phone: str = Form(""), address: str = Form(""), notes: str = Form(""),
):
    with get_db() as db:
        db.execute(
            "INSERT INTO patients (patient_no,name,gender,birth_date,phone,address,notes,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (patient_no.strip(), name.strip(), gender, birth_date or None, phone.strip(), address.strip(), notes.strip(), datetime.now().isoformat(timespec="seconds")),
        )
        patient_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    return RedirectResponse(f"/patients/{patient_id}", status_code=303)


@app.post("/patients/{patient_id}/encounters")
def create_encounter(
    patient_id: int, encounter_date: str = Form(...), encounter_type: str = Form(...), complaint: str = Form(""),
    diagnosis: str = Form(""), treatment: str = Form(""), notes: str = Form(""),
):
    with get_db() as db:
        db.execute(
            "INSERT INTO encounters (patient_id,encounter_date,encounter_type,complaint,diagnosis,treatment,notes,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (patient_id, encounter_date, encounter_type, complaint.strip(), diagnosis.strip(), treatment.strip(), notes.strip(), datetime.now().isoformat(timespec="seconds")),
        )
    return RedirectResponse(f"/patients/{patient_id}", status_code=303)
