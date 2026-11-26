from contextlib import asynccontextmanager
from datetime import date
import json
import sqlite3
from html import escape
from urllib.parse import quote
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi import HTTPException
from . import ingest

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from . import attachments as attachments_module
from . import llm
from . import openehr
from . import patients as patient_service
from .db import BASE_DIR, get_db, init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="电子健康档案 MVP", lifespan=lifespan)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
def patient_list(
    request: Request, q: str = "", gender: str = "", status: str = "active",
    created_from: str = "", created_to: str = "",
):
    filters = patient_service.parse_filters(q, gender, status, created_from, created_to)
    with get_db() as db:
        patients = patient_service.list_patients(db, filters)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "patients": patients,
            "filters": filters,
            "genders": patient_service.GENDERS,
            "status_labels": patient_service.STATUS_LABELS,
        },
    )


@app.post("/patients")
def create_patient(
    patient_no: str = Form(...), name: str = Form(...), gender: str = Form(...), birth_date: str = Form(""),
    phone: str = Form(""), address: str = Form(""), notes: str = Form(""),
):
    fields = patient_service.validate_patient(patient_no, name, gender, birth_date, phone, address, notes)
    with get_db() as db:
        patient_id = patient_service.create_patient(db, fields)
        openehr.ensure_ehr(db, patient_id)
    return RedirectResponse("/patients/%d" % patient_id, status_code=303)


@app.get("/patients/{patient_id}/edit", response_class=HTMLResponse)
def edit_patient_page(request: Request, patient_id: int):
    with get_db() as db:
        patient = patient_service.get_patient(db, patient_id)
    if patient is None:
        raise HTTPException(404, "患者不存在")
    return templates.TemplateResponse(
        "patient_edit.html",
        {"request": request, "patient": patient, "genders": patient_service.GENDERS},
    )


@app.post("/patients/{patient_id}/edit")
def edit_patient(
    patient_id: int, patient_no: str = Form(...), name: str = Form(...), gender: str = Form(...),
    birth_date: str = Form(""), phone: str = Form(""), address: str = Form(""), notes: str = Form(""),
):
    fields = patient_service.validate_patient(patient_no, name, gender, birth_date, phone, address, notes)
    with get_db() as db:
        patient_service.update_patient(db, patient_id, fields)
    return RedirectResponse("/patients/%d" % patient_id, status_code=303)


@app.post("/patients/{patient_id}/status")
def change_patient_status(patient_id: int, status: str = Form(...)):
    with get_db() as db:
        patient_service.set_status(db, patient_id, status)
    return RedirectResponse("/patients/%d" % patient_id, status_code=303)


@app.post("/patients/{patient_id}/encounters/{encounter_id}/edit")
def edit_encounter(
    patient_id: int, encounter_id: int, encounter_date: str = Form(...), encounter_type: str = Form(...),
    complaint: str = Form(""), diagnosis: str = Form(""), treatment: str = Form(""), notes: str = Form(""),
):
    fields = patient_service.validate_encounter(encounter_date, encounter_type, complaint, diagnosis, treatment, notes)
    with get_db() as db:
        patient_service.update_encounter(db, patient_id, encounter_id, fields)
    return RedirectResponse("/patients/%d" % patient_id, status_code=303)


@app.get("/patients/{patient_id}", response_class=HTMLResponse)
def patient_detail(request: Request, patient_id: int):
    with get_db() as db:
        patient = patient_service.get_patient(db, patient_id)
        if patient is None:
            raise HTTPException(404, "患者不存在")
        encounters = db.execute(
            "SELECT * FROM encounters WHERE patient_id=? ORDER BY encounter_date DESC, id DESC", (patient_id,)
        ).fetchall()
        compositions = openehr.list_compositions(db, patient_id)
        for comp in compositions:
            comp["history"] = openehr.composition_history(db, comp["uid"])
            comp["edit_fields"] = openehr.composition_form_fields(db, comp)
        timeline = ingest.timeline(db, patient_id)
        attachments = attachments_module.list_attachments(db, patient_id)
    return templates.TemplateResponse(
        "patient.html",
        {
            "request": request,
            "patient": patient,
            "encounters": encounters,
            "compositions": compositions,
            "attachments": attachments,
            "timeline": timeline,
            "templates": openehr.get_templates(),
            "encounter_types": patient_service.ENCOUNTER_TYPES,
            "status_labels": patient_service.STATUS_LABELS,
            "today": date.today().isoformat(),
        },
    )


@app.post("/patients/{patient_id}/encounters")
def create_encounter(
    patient_id: int, encounter_date: str = Form(...), encounter_type: str = Form(...), complaint: str = Form(""),
    diagnosis: str = Form(""), treatment: str = Form(""), notes: str = Form(""),
):
    fields = patient_service.validate_encounter(encounter_date, encounter_type, complaint, diagnosis, treatment, notes)
    with get_db() as db:
        if patient_service.get_patient(db, patient_id) is None:
            raise HTTPException(404, "患者不存在")
        patient_service.create_encounter(db, patient_id, fields)
    return RedirectResponse("/patients/%d" % patient_id, status_code=303)


@app.post("/patients/{patient_id}/compositions")
async def create_composition(request: Request, patient_id: int):
    form = await request.form()
    template_id = str(form.get("template_id", ""))
    template = openehr.TEMPLATES.get(template_id)
    if template is None:
        return RedirectResponse("/patients/%d" % patient_id, status_code=303)
    values = {field["key"]: str(form.get(field["key"], "")) for field in template["fields"]}
    with get_db() as db:
        openehr.create_composition(
            db, patient_id, template_id, values,
            composer=str(form.get("composer", "")), note=str(form.get("note", "")),
        )
    return RedirectResponse("/patients/%d" % patient_id, status_code=303)


@app.post("/compositions/{uid}")
async def revise_composition(request: Request, uid: str):
    form = await request.form()
    with get_db() as db:
        composition = openehr.get_composition(db, uid)
        if composition is None:
            return RedirectResponse("/", status_code=303)
        template = openehr.TEMPLATES.get(composition["template_id"], {})
        values = {field["key"]: str(form.get(field["key"], "")) for field in template.get("fields", [])}
        patient_id = db.execute("SELECT patient_id FROM ehrs WHERE ehr_id=(SELECT ehr_id FROM compositions WHERE uid=?)", (uid,)).fetchone()["patient_id"]
        openehr.update_composition(
            db, uid, values,
            composer=str(form.get("composer", "")), note=str(form.get("note", "")),
        )
    return RedirectResponse("/patients/%d" % patient_id, status_code=303)


@app.post("/patients/{patient_id}/attachments")
async def upload_attachment(
    patient_id: int, file: UploadFile = File(...), category: str = Form(""), description: str = Form(""),
):
    content = await read_upload(file)
    with get_db() as db:
        if content:
            attachments_module.save_attachment(
                db, patient_id, file.filename or "file", content,
                file.content_type or "", category, description,
            )
    return RedirectResponse("/patients/%d" % patient_id, status_code=303)


@app.get("/attachments/{attachment_id}/download")
def download_attachment(attachment_id: int):
    with get_db() as db:
        row = attachments_module.get_attachment(db, attachment_id)
    if row is None:
        return RedirectResponse("/", status_code=303)
    from pathlib import Path

    path = Path(row["stored_path"])
    if not path.exists():
        return RedirectResponse("/", status_code=303)
    return Response(
        content=path.read_bytes(),
        media_type=row["content_type"] or "application/octet-stream",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(row['filename'])},
    )


@app.post("/attachments/{attachment_id}/delete")
def remove_attachment(attachment_id: int):
    with get_db() as db:
        row = attachments_module.get_attachment(db, attachment_id)
        patient_id = row["patient_id"] if row else 0
        attachments_module.delete_attachment(db, attachment_id)
    return RedirectResponse("/patients/%d" % patient_id if patient_id else "/", status_code=303)


@app.get("/patients/{patient_id}/export")
def export_patient(patient_id: int):
    with get_db() as db:
        try:
            bundle = openehr.export_ehr(db, patient_id)
        except ValueError:
            return RedirectResponse("/", status_code=303)
    return Response(
        content=json.dumps(bundle, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="ehr_%d.json"' % patient_id},
    )


@app.post("/ehr/import")
async def import_ehr(file: UploadFile = File(...)):
    raw = await read_upload(file)
    try:
        bundle = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("档案不是有效的 UTF-8 JSON，未导入") from exc
    with get_db() as db:
        patient_id = openehr.import_ehr(db, bundle)
    return RedirectResponse("/patients/%d" % patient_id, status_code=303)


@app.get("/api/patients/{patient_id}/context")
def api_context(patient_id: int):
    with get_db() as db:
        try:
            context = llm.assemble_context(db, patient_id)
        except ValueError:
            return JSONResponse({"error": "患者不存在"}, status_code=404)
    return JSONResponse(context)


@app.get("/api/patients/{patient_id}/prompt")
def api_prompt(patient_id: int):
    with get_db() as db:
        try:
            context = llm.assemble_context(db, patient_id)
        except ValueError:
            return JSONResponse({"error": "患者不存在"}, status_code=404)
    return JSONResponse({"prompt": llm.build_prompt(context)})


@app.post("/api/ask")
async def api_ask(request: Request):
    form = await request.form()
    try:
        patient_id = int(form.get("patient_id", 0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "patient_id 无效"}, status_code=400)
    question = str(form.get("question", ""))
    try:
        result = await run_in_threadpool(answer_local, patient_id, question)
    except ValueError:
        return JSONResponse({"error": "患者不存在"}, status_code=404)
    return JSONResponse({"answer": result["answer"]})


MAX_UPLOAD = 10 * 1024 * 1024

async def read_upload(file):
    content = bytearray()
    try:
        while True:
            chunk = await file.read(65536)
            if not chunk:
                break
            if len(content) + len(chunk) > MAX_UPLOAD:
                raise HTTPException(413, '单个上传文件不能超过 10 MiB')
            content.extend(chunk)
        return bytes(content)
    finally:
        await file.close()


def answer_local(patient_id, question):
    with get_db() as db:
        return llm.answer(db, patient_id, question)


@app.exception_handler(StarletteHTTPException)
async def http_error(request, exc):
    if request.url.path.startswith('/api/'):
        return JSONResponse({'error': exc.detail}, status_code=exc.status_code)
    if exc.status_code == 404:
        return templates.TemplateResponse('404.html', {'request': request}, status_code=404)
    return HTMLResponse(
        '<h1>请求未完成</h1><p>' + escape(str(exc.detail)) + '</p><a href="/">返回首页</a>',
        status_code=exc.status_code,
    )


@app.exception_handler(ValueError)
async def validation_error(request, exc):
    if request.url.path.startswith('/api/'):
        return JSONResponse({'error': str(exc)}, status_code=400)
    return HTMLResponse('<h1>操作未完成</h1><p>' + escape(str(exc)) + '</p><a href="/">返回首页</a> · <a href="/ingest">返回导入中心</a>', status_code=400)


@app.exception_handler(sqlite3.IntegrityError)
async def integrity_error(request, exc):
    return await validation_error(request, ValueError('编号重复或关联记录不存在，请检查输入；本次操作未保存。'))


@app.get('/ingest', response_class=HTMLResponse)
def ingest_page(request: Request):
    with get_db() as db:
        patients = db.execute('SELECT id,name,patient_no FROM patients ORDER BY id').fetchall()
        links = db.execute('SELECT x.*, p.name FROM patient_external_ids x JOIN patients p ON p.id=x.patient_id').fetchall()
        jobs = db.execute('SELECT * FROM ingest_jobs ORDER BY id DESC LIMIT 50').fetchall()
    return templates.TemplateResponse('ingest.html', {'request':request,'patients':patients,'links':links,'jobs':jobs,'rules':ingest.rules()})


@app.post('/ingest/bind')
def bind_patient(source: str = Form(...), external_id: str = Form(...), patient_id: int = Form(...)):
    with get_db() as db:
        ingest.bind(db, source, external_id, patient_id)
    return RedirectResponse('/ingest', status_code=303)


@app.post('/api/ingest/csv')
async def ingest_csv(source: str = Form(...), file: UploadFile = File(...)):
    content = await read_upload(file)
    def run():
        with get_db() as db:
            return ingest.import_csv(db, source, file.filename or 'upload.csv', content)
    return JSONResponse(await run_in_threadpool(run))


@app.get('/api/ingest/jobs/{job_id}')
def ingest_job(job_id: int):
    with get_db() as db:
        job = db.execute('SELECT * FROM ingest_jobs WHERE id=?',(job_id,)).fetchone()
        if not job:
            raise HTTPException(404, '导入任务不存在')
        rows = [dict(r) for r in db.execute('SELECT * FROM raw_records WHERE job_id=? ORDER BY line_number',(job_id,))]
    return {'job':dict(job),'rows':rows}


@app.get('/api/ehr/{patient_id}/timeline')
def ehr_timeline(patient_id: int):
    with get_db() as db:
        try:
            return {'patient_id':patient_id, 'records':ingest.timeline(db,patient_id)}
        except ValueError:
            raise HTTPException(404,'患者不存在')


@app.get('/api/ehr/{patient_id}/bundle')
def ehr_bundle(patient_id: int):
    with get_db() as db:
        try:
            result = openehr.export_ehr(db,patient_id)
            result['timeline'] = ingest.timeline(db,patient_id)
            return result
        except ValueError:
            raise HTTPException(404,'患者不存在')


@app.get('/api/ehr/{patient_id}/context')
def ehr_context(patient_id: int):
    return api_context(patient_id)


@app.get('/api/ehr/{patient_id}/provenance/{uid}')
def record_provenance(patient_id: int, uid: str):
    with get_db() as db:
        origin = db.execute('SELECT r.*,j.source_id,j.filename,j.mapping_json FROM raw_records r JOIN ingest_jobs j ON j.id=r.job_id JOIN compositions c ON c.uid=r.composition_uid JOIN ehrs e ON e.ehr_id=c.ehr_id WHERE c.uid=? AND e.patient_id=? AND r.status=?', (uid,patient_id,'imported')).fetchone()
        if not origin:
            raise HTTPException(404,'来源记录不存在')
        return dict(origin)
