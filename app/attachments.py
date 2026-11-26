"""非结构化数据（附件）的存储与文本抽取。

文本类文件直接读取为纯文本用于大模型上下文；图片/音视频等二进制文件先存储，
后续可接入 OCR / ASR 等模态转换后再回填 ``extracted_text``。
"""

from datetime import datetime
from pathlib import Path
import hashlib
import re
from uuid import uuid4

from .db import UPLOAD_DIR


TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log", ".xml", ".html", ".htm"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
MAX_TEXT_CHARS = 20000


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _safe_name(filename):
    base = Path(filename or "file").name
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]", "_", base) or "file"


def _modality(extension):
    if extension in TEXT_EXTENSIONS:
        return "text"
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in AUDIO_EXTENSIONS:
        return "audio"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    return "binary"


def _extract_text(data, extension):
    if extension not in TEXT_EXTENSIONS:
        return None
    try:
        text = data.decode("utf-8", errors="ignore")
    except Exception:
        return None
    return text[:MAX_TEXT_CHARS]


def save_attachment(db, patient_id, filename, content, content_type="", category="", description=""):
    if not db.execute("SELECT 1 FROM patients WHERE id=?", (patient_id,)).fetchone():
        raise ValueError("患者不存在")
    extension = Path(filename or "").suffix.lower()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stored_name = "%s_%s" % (uuid4().hex, _safe_name(filename))
    stored_path = UPLOAD_DIR / stored_name
    stored_path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    extracted = _extract_text(content, extension)
    db.execute(
        "INSERT INTO attachments (patient_id,filename,content_type,size,sha256,category,description,stored_path,extracted_text,uploaded_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (patient_id, _safe_name(filename), content_type or "", len(content), digest,
         category or _modality(extension), description or "", str(stored_path), extracted, _now()),
    )
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def list_attachments(db, patient_id):
    rows = db.execute(
        "SELECT * FROM attachments WHERE patient_id=? ORDER BY id DESC", (patient_id,)
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["modality"] = _modality(Path(row["filename"]).suffix.lower())
        result.append(item)
    return result


def get_attachment(db, attachment_id):
    return db.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()


def delete_attachment(db, attachment_id):
    row = get_attachment(db, attachment_id)
    if row is None:
        return False
    try:
        Path(row["stored_path"]).unlink(missing_ok=True)
    except OSError:
        pass
    db.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
    return True
