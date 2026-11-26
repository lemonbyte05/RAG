# 多源档案 MVP 验收与演示

## 启动与复现

在项目目录执行 `powershell -ExecutionPolicy Bypass -File .\start.ps1 -Demo`。
使用独立的 `data/demo.db` 和 `data/demo_uploads/`，不覆盖原有 `health_records.db`。
首次使用需先按 README 安装现有依赖；本次未增加第三方依赖。
端口占用时加 `-Port 8001`。运行期间保留启动终端；关闭终端后服务停止。

## 演示步骤

1. 首页打开李明的虚构示例档案，页面底部“统一档案时间线”可看到医院、体检和随访记录。
2. 打开“多源导入中心”，查看 H001 / E001 / C001 对应同一患者的明确绑定。
3. 选择医院来源，上传 `examples/hospital.csv`，结果为 duplicate=1，档案不会重复。
4. 将文件另存为测试文件，改成新的就诊编号再导入，可看到新记录；修改日期为错误值，可看到失败行号和原因。
5. 查看批次详情及原始记录；在患者时间线查看来源和版本。
6. 查看 `/api/ehr/1/timeline`、`/api/ehr/1/context`、`/api/ehr/1/bundle`。这三个接口供其他板块调用，无需访问数据库。
7. 上传中文文本附件、下载核对；导出档案并重新导入，打开新建的 IMP 编号档案，核对版本、附件、手工就诊及来源。

## 接口契约

| 方法与路径 | 输入与结果 |
| --- | --- |
| POST `/ingest/bind` | 表单 source、external_id、patient_id；明确绑定，不允许覆盖到其他患者 |
| POST `/api/ingest/csv` | multipart 表单 source、file；返回 job_id、imported、duplicate、failed、errors（行号与原因） |
| GET `/api/ingest/jobs/{id}` | 批次映射配置快照、原始行、哈希和逐行结果 |
| GET `/api/ehr/{id}/timeline` | patient_id、records；每条含 id、observed_at、recorded_at、updated_at、time_basis、version、source、normalization、provenance、items |
| GET `/api/ehr/{id}/provenance/{uid}` | 患者名下记录的原始输入、映射快照和 standardization 结果 |
| GET `/api/ehr/{id}/context` | 兼容原上下文接口，纳入传统就诊和来源记录 |
| GET `/api/ehr/{id}/bundle` | 完整交换数据与统一时间线 |

映射定义在 `config/mappings.json`，支持来源字段、模板、日期格式，以及通过 `metrics` 声明的标准指标编码和原始单位。
数值字段导入时调用 `app/dictionary.py` 换算为标准单位，并在 COMPOSITION 的 `normalization` 中保留原始值、原始单位、标准值和标准单位。
时间语义：`observed_at` 为测量时间，`recorded_at` 为入库时间，`updated_at` 为最后更新时间。
来源内记录编号唯一：相同内容重导跳过；同编号不同内容报告冲突，不自动覆盖。
行级失败独立回滚，其他合法行可以成功；未知指标、不支持单位、非法数值都会返回明确原因；表头或文件格式错误拒绝整个文件。
CSV 日期按本地时间解析。无事件时间的手工模板记录显示录入时间，time_basis=recorded。
原始内容哈希按解析后的行计算，不等同于源文件字节哈希。

## 数据与边界

- 本轮为本地单用户、虚构数据演示。没有登录、患者权限或访问审计，不用于真实多人部署。
- 三种 CSV 体现三个来源的字段异构；尚未实现医院协议、设备接口、PDF/OCR、事件推送或增量同步游标。
- 使用简化 openEHR 风格模型，不宣称完整标准兼容；旧 encounters 保留，通过统一查询合并展示，不迁移或双写旧数据。
- 首次外部编号由人确认绑定；未知编号导入报错，不使用姓名猜测关联。
- 上传文件上限 10 MiB，分块读取并在限额内缓冲；CSV 上限 5000 行。
- 导出 JSON 包含附件原文 Base64；大档案可能超过当前导入大小限额，需缩小演示档案。缺失附件内容、错误版本会显式拒绝，不静默丢弃。
- 交换导入创建独立档案（编号冲突时使用 IMP 前缀），保留来源快照，但不自动复制外部身份绑定或导入任务。
- 外部模型调用默认关闭，仅显式设置 EHR_ALLOW_REMOTE_LLM=1 时启用。该开关不是脱敏机制。

## 验证

执行 `python -m unittest discover -s tests -v`。
测试使用临时目录和独立数据库，覆盖三源关联、日期/数值校验、部分成功、幂等、冲突、单位换算与标准化、非法数值、未知指标、不支持单位、时间字段、连接关闭、持久化、版本/附件往返、远端默认关闭及真实 HTTP 页面和接口流程。
