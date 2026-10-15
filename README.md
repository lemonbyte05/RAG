# 电子健康档案 MVP

## 启动

```powershell
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

浏览器访问 <http://127.0.0.1:8000>。首次启动会在项目目录生成 `health_records.db`，并写入两名脱敏示例患者。

当前 MVP 支持患者基础档案、就诊记录新增，以及按患者查看就诊时间线。
