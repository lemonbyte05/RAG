"""大模型接入层。

将结构化（OpenEHR composition）与非结构化（附件抽取文本）数据组装为统一上下文，
并通过 OpenAI 兼容接口调用康养垂直大模型。未配置时返回本地兜底结果，便于演示。
配置方式（环境变量）：
    EHR_LLM_BASE_URL  例如 https://api.example.com/v1
    EHR_LLM_API_KEY
    EHR_LLM_MODEL     例如 qwen2.5:7b
"""

from datetime import date
import json
import os
import urllib.error
import urllib.request

from . import attachments as attachments_module
from . import openehr


SYSTEM_PROMPT = (
    "你是一名老年康养健康助手，服务于居家、社区、机构三类养老场景。"
    "请基于患者的电子健康档案（结构化观测数据与非结构化资料）给出专业、合规的中文回答，"
    "内容涵盖健康状况评估、慢病风险提示与个性化康养建议。回答中不要编造未提供的数据。"
)


def assemble_context(db, patient_id):
    patient = db.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
    if patient is None:
        raise ValueError("患者不存在: %s" % patient_id)
    from .ingest import timeline
    records = []
    for rec in timeline(db, patient_id):
        records.append({**rec, 'template_id': rec['name'], 'committed_at': rec['observed_at']})
    documents = []
    for row in attachments_module.list_attachments(db, patient_id):
        if row.get("extracted_text"):
            documents.append({
                "filename": row["filename"],
                "modality": row["modality"],
                "text": row["extracted_text"],
            })
    return {
        "generated_at": date.today().isoformat(),
        "subject": {
            "patient_no": patient["patient_no"],
            "name": patient["name"],
            "gender": patient["gender"],
            "birth_date": patient["birth_date"],
            "phone": patient["phone"],
            "address": patient["address"],
            "notes": patient["notes"],
        },
        "structured_records": records,
        "unstructured_documents": documents,
    }


def build_prompt(context):
    lines = ["【患者基础信息】"]
    subject = context["subject"]
    lines.append("姓名：%s；性别：%s；出生日期：%s" % (subject["name"], subject["gender"], subject["birth_date"] or "未知"))
    lines.append("患者编号：%s；联系电话：%s；地址：%s" % (subject["patient_no"], subject["phone"] or "无", subject["address"] or "无"))
    if subject.get("notes"):
        lines.append("备注：%s" % subject["notes"])

    lines.append("")
    lines.append("【结构化观测记录】")
    if context["structured_records"]:
        for rec in context["structured_records"]:
            pairs = "；".join(
                "%s=%s%s" % (item["label"], item["value"], item["unit"] or "") for item in rec["items"]
            ) or "无有效数据"
            lines.append("- [%s] %s（%s，v%s）：%s" % (rec["template_id"], rec["name"], rec["committed_at"], rec["version"], pairs))
    else:
        lines.append("- 暂无结构化记录")

    lines.append("")
    lines.append("【非结构化资料（文本抽取）】")
    if context["unstructured_documents"]:
        for doc in context["unstructured_documents"]:
            lines.append("- 文件《%s》(%s)：%s" % (doc["filename"], doc["modality"], doc["text"][:2000]))
    else:
        lines.append("- 暂无可用文档")
    return "\n".join(lines)


def _call_remote(prompt, question):
    if os.environ.get("EHR_ALLOW_REMOTE_LLM") != "1":
        return None
    base_url = os.environ.get("EHR_LLM_BASE_URL", "").rstrip("/")
    api_key = os.environ.get("EHR_LLM_API_KEY", "")
    model = os.environ.get("EHR_LLM_MODEL", "")
    if not base_url or not model:
        return None
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt + "\n\n【问题】" + (question or "请对该患者做一次综合健康评估并给出康养建议。")},
        ],
        "temperature": 0.3,
    }
    request = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"]
    except (urllib.error.URLError, KeyError, IndexError, ValueError) as exc:
        return "（调用远程大模型失败：%s）" % exc


def _fallback_answer(context, question):
    name = context["subject"]["name"]
    bp_items = []
    for rec in context["structured_records"]:
        for item in rec["items"]:
            if item["key"] in ("systolic", "diastolic"):
                bp_items.append(item["value"])
    hint = ""
    if len(bp_items) >= 2:
        try:
            if int(bp_items[0]) >= 140 or int(bp_items[1]) >= 90:
                hint = "最新血压偏高，建议持续监测并按医嘱用药。"
        except ValueError:
            hint = ""
    return (
        "【本地兜底回答 · 未配置大模型】\n"
        "患者 %s 当前档案包含 %d 条结构化记录、%d 份文档资料。%s\n"
        "问题：%s\n"
        "配置 EHR_LLM_BASE_URL / EHR_LLM_MODEL / EHR_LLM_API_KEY 后可获得大模型生成结果。"
        % (name, len(context["structured_records"]), len(context["unstructured_documents"]), hint, question or "综合健康评估")
    )


def answer(db, patient_id, question=""):
    context = assemble_context(db, patient_id)
    prompt = build_prompt(context)
    reply = _call_remote(prompt, question)
    if reply is None:
        reply = _fallback_answer(context, question)
    return {"answer": reply, "prompt": prompt, "context": context}
