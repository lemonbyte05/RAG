# 电子健康档案 MVP

面向老年康养场景的电子健康档案集成平台原型，对应申报书"研究内容 3"。

## 启动

```powershell
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

浏览器访问 <http://127.0.0.1:8000>。首次启动会生成 `health_records.db`（SQLite），并写入脱敏示例患者、模板与示例健康记录；附件保存于 `data/uploads/`。

## 功能

- 患者基础档案与就诊记录管理（列表 / 新增 / 编辑 / 时间线）
- **档案维护**：按姓名或编号搜索，按性别、状态、创建时间筛选；停用代替删除，可随时启用
- **就诊记录修订**：可直接在时间线中编辑，保留最后修改时间
- **OpenEHR 双层建模**：参考模型 + 老年康养临床模板，记录以 COMPOSITION 形式持久化
- **版本管理**：每次修改生成新版本，可查看历史追溯
- **非结构化数据**：附件上传/下载/删除，文本类文件自动抽取内容，支持图像、音频、视频归档
- **档案交换**：一键导出/导入 OpenEHR JSON，支持跨机构数据交换
- **大模型 API**：结构化 + 非结构化数据组装为统一上下文，提供评估接口

## 目录

```
app/
  main.py         # FastAPI 路由
  db.py           # SQLite 连接、表结构、迁移、示例数据
  patients.py     # 患者/就诊记录的校验、搜索、编辑与停用
  ingest.py       # 多源 CSV 导入、映射与来源留痕
  openehr.py      # OpenEHR 简化模型、模板、版本管理、导入导出
  attachments.py  # 附件存储与文本抽取
  llm.py          # 大模型上下文组装与调用
templates/        # Jinja2 页面
static/           # 样式与脚本
```

## 数据字典 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/dictionary` | 返回完整健康数据字典（指标、单位、精度、合理范围） |
| GET | `/api/dictionary/{code}` | 返回单个指标定义，如 `/api/dictionary/BP.SYS` |

## 大模型 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/patients/{id}/context` | 返回组装后的结构化 + 非结构化上下文 JSON |
| GET | `/api/patients/{id}/prompt` | 返回可直接送入大模型的提示词 |
| POST | `/api/ask` | 表单参数 `patient_id`、`question`，返回评估结果 |

接入 OpenAI 兼容大模型（可选）：

```powershell
$env:EHR_LLM_BASE_URL="https://api.example.com/v1"
$env:EHR_LLM_MODEL="qwen2.5:7b"
$env:EHR_LLM_API_KEY="sk-..."
```

未配置时 `/api/ask` 返回本地兜底结果，便于离线演示。


## 多源档案 MVP（2026-10-25）

推荐演示启动：`powershell -ExecutionPolicy Bypass -File .\start.ps1 -Demo`。
独立演示数据库已经包含医院、体检、社区随访三源档案，不影响原数据库。
完整操作、API 契约、验收方法和边界见 [MVP 演示说明](docs/mvp-demo.md)。

测试：`python -m unittest discover -s tests -v`。
当前为简化 openEHR 风格本地演示，不是完整标准服务器。

## V0.2-01 档案模块完善（2026-11-26）

- 患者档案支持编辑、按姓名/编号搜索、按性别/状态/创建时间筛选。
- 档案停用代替删除：默认列表只显示在用档案，可切换查看已停用档案并重新启用。
- 就诊记录支持编辑，且只能修改属于当前患者的记录。
- 表单后端校验统一收敛到 `app/patients.py`：必填、性别取值、日期格式、出生日期不得晚于今天、字段长度。
- 重复患者编号给出友好提示；不存在的患者返回 404 页面。
- 旧数据库启动时自动补齐 `patients.status/updated_at`、`encounters.updated_at` 列，不丢数据。
- 新增 `tests/test_patients.py`，并扩展 `tests/test_http.py` 覆盖编辑、搜索、停用、就诊修订、校验与 404。

## V0.3 任务一：统一健康数据字典（2026-11-26）

- 新增 `config/data_dictionary.json`：8 个初始指标（收缩压、舒张压、心率、体温、血氧、血糖、体重、身高），包含自定义编码、数据类型、标准单位、允许单位与换算参数、精度、合理范围、测量方式和定义。
- 新增 `app/dictionary.py`：负责字典加载、结构校验、模板覆盖率校验、单位换算与合理范围标记。
- 单位换算是“值 × factor + offset”，支持 ℃/℉、mmHg/kPa、mmol/L/mg/dL、kg/g/斤、cm/m/mm 等；换算结果保留原始记录，不覆盖来源值。
- 启动时校验字典与临床模板一致，模板中的数值字段必须都有字典定义。
- 新增 `GET /api/dictionary`、`GET /api/dictionary/{code}`，供后续标准化、质量评价和其他板块调用。
- 编码为项目内部初始编码，不是 LOINC/SNOMED CT 等国际标准；后续再研究标准术语映射。
- 保持 COMPOSITION 为档案核心载体，本阶段不新建 `health_records` 表，避免两套数据不一致。

## V0.3 任务二：标准化接入与时间语义（2026-11-26）

- `app/ingest.py` 导入数值字段时调用 `app/dictionary.py`，按指标编码和来源单位换算为标准单位。
- `config/mappings.json` 新增 `metrics`：为每个数值字段声明标准指标编码和原始单位，例如体温 `TEMP` / `℃`、血糖 `GLU` / `mg/dL`。
- 支持 ℃/℉、mmHg/kPa、mmol/L/mg/dL、kg/g/斤、cm/m/mm 等单位换算；不支持的指标或单位、非法数值都会在行级失败并返回明确原因。
- COMPOSITION 新增 `normalization`：保存每个数值字段的 `metric_code`、`raw_value`、`raw_unit`、`standard_value`、`standard_unit`、`plausible`，原始值不丢失。
- 明确区分时间语义：`observed_at` 测量时间、`recorded_at` 入库时间、`updated_at` 更新时间；`record_origins` 同步保存 `recorded_at/updated_at`。
- `GET /api/ehr/{id}/provenance/{uid}` 返回 `normalization`，可直接核对原始值与标准值。
- 启动时 `ingest.validate_rules()` 校验映射配置与模板、数据字典一致，防止配置漂移。
- 新增/扩展测试覆盖单位换算、非法数值、未知指标、不支持单位、重复导入、时间字段和来源追溯。
外部大模型默认关闭；显式设置 `EHR_ALLOW_REMOTE_LLM=1` 才会调用配置的远端服务。
