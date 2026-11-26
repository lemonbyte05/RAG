"""统一健康数据字典的加载、查询、自检与单位换算。

字典保存在 ``config/data_dictionary.json``，是后续标准化、质量评价、冲突处理
和对外 API 的共同基础。这里只做“读字典”和“按字典换算”，不改变现有
COMPOSITION 存储结构。
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from pathlib import Path
from typing import Optional

from .db import BASE_DIR


DICTIONARY_PATH = BASE_DIR / "config" / "data_dictionary.json"
_ALLOWED_TYPES = ("number", "text", "date")
_CACHE = {}


def _fail(message):
    raise ValueError("健康数据字典有误：" + message)


def _parse(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError("健康数据字典文件无法读取：%s" % path) from exc
    except ValueError as exc:
        raise ValueError("健康数据字典不是有效的 JSON") from exc
    if not isinstance(data, dict):
        _fail("根节点必须是对象")
    metrics = data.get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        _fail("缺少 metrics 或 metrics 为空")
    for code, metric in metrics.items():
        _validate_metric(code, metric)
    return data


def _validate_metric(code, metric):
    if not isinstance(metric, dict):
        _fail("指标 %s 必须是对象" % code)
    if metric.get("code") != code:
        _fail("指标键名与 code 不一致：%s" % code)
    if not str(metric.get("name") or "").strip():
        _fail("指标 %s 缺少中文名称" % code)
    if metric.get("data_type") not in _ALLOWED_TYPES:
        _fail("指标 %s 的 data_type 非法" % code)
    units = metric.get("allowed_units")
    if not isinstance(units, dict) or not units:
        _fail("指标 %s 缺少 allowed_units" % code)
    for unit, entry in units.items():
        if not isinstance(entry, dict):
            _fail("指标 %s 的单位 %s 必须是对象" % (code, unit))
        try:
            factor = Decimal(str(entry.get("factor")))
            offset = Decimal(str(entry.get("offset", 0)))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("健康数据字典有误：指标 %s 的单位 %s 换算参数非法" % (code, unit)) from exc
        if not factor.is_finite() or factor <= 0 or not offset.is_finite():
            _fail("指标 %s 的单位 %s 换算参数非法" % (code, unit))
    if metric["data_type"] == "number":
        if metric.get("standard_unit") not in units:
            _fail("指标 %s 的 standard_unit 不在 allowed_units 中" % code)
        precision = metric.get("precision")
        if not isinstance(precision, int) or isinstance(precision, bool) or precision < 0:
            _fail("指标 %s 的 precision 必须是非负整数" % code)
        value_range = metric.get("plausible_range") or {}
        try:
            low = Decimal(str(value_range.get("min")))
            high = Decimal(str(value_range.get("max")))
        except (InvalidOperation, TypeError, ValueError):
            low = high = None
        if low is None or high is None or not low.is_finite() or not high.is_finite() or low >= high:
            _fail("指标 %s 的 plausible_range 非法" % code)


def load(path: Optional[Path] = None):
    """读取并校验字典；按文件修改时间缓存，避免每次请求重复解析。"""
    target = Path(path) if path is not None else DICTIONARY_PATH
    try:
        stat = target.stat()
    except OSError as exc:
        raise ValueError("健康数据字典文件不存在：%s" % target) from exc
    stamp = (stat.st_mtime_ns, stat.st_size)
    key = str(target)
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    data = _parse(target)
    _CACHE[key] = (stamp, data)
    return data


def metrics(path: Optional[Path] = None):
    return list(load(path)["metrics"].values())


def get(code, path: Optional[Path] = None):
    return load(path)["metrics"].get(code)


def by_template_field(template_id, field_key, path: Optional[Path] = None):
    for metric in load(path)["metrics"].values():
        template = metric.get("template") or {}
        if template.get("template_id") == template_id and template.get("field_key") == field_key:
            return metric
    return None


def _unit(metric, unit):
    text = (unit or "").strip()
    units = metric["allowed_units"]
    if text in units:
        return text, units[text]
    folded = text.casefold()
    for name, entry in units.items():
        if name.casefold() == folded:
            return name, entry
    raise ValueError("指标 %s 不支持单位：%s" % (metric["code"], unit))


def resolve_unit(code, unit, path: Optional[Path] = None):
    """返回 ``(规范化单位名, {factor, offset})``，单位不支持时抛出 ValueError。"""
    metric = get(code, path)
    if metric is None:
        raise ValueError("未知指标编码：%s" % code)
    return _unit(metric, unit)


def convert_to_standard(value, code, unit, path: Optional[Path] = None):
    """把原始数值换算为字典中的标准单位，返回 ``Decimal``。"""
    metric = get(code, path)
    if metric is None:
        raise ValueError("未知指标编码：%s" % code)
    if metric["data_type"] != "number":
        raise ValueError("指标 %s 不是数值型，不能做单位换算" % code)
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError) as exc:
        raise ValueError("测量值不是有效数字：%s" % value) from exc
    if not number.is_finite():
        raise ValueError("测量值必须有限")
    _, entry = _unit(metric, unit)
    return number * Decimal(str(entry["factor"])) + Decimal(str(entry.get("offset", 0)))


def format_standard(value, code, path: Optional[Path] = None):
    """按字典精度把标准单位数值格式化为字符串。"""
    metric = get(code, path)
    if metric is None:
        raise ValueError("未知指标编码：%s" % code)
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    quantum = Decimal(1).scaleb(-int(metric["precision"]))
    return format(number.quantize(quantum, rounding=ROUND_HALF_UP), "f")


def plausible_status(value, code, path: Optional[Path] = None):
    """返回 ``ok`` / ``below_range`` / ``above_range``；不删除任何数据。"""
    metric = get(code, path)
    if metric is None:
        raise ValueError("未知指标编码：%s" % code)
    value_range = metric.get("plausible_range") or {}
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    if number < Decimal(str(value_range["min"])):
        return "below_range"
    if number > Decimal(str(value_range["max"])):
        return "above_range"
    return "ok"


def validate_against(templates, path: Optional[Path] = None):
    """校验字典与临床模板一致，并覆盖模板中的全部数值字段。"""
    data = load(path)
    covered = set()
    for code, metric in data["metrics"].items():
        template = metric.get("template")
        if not template:
            continue
        template_id = template.get("template_id")
        field_key = template.get("field_key")
        fields = {field["key"]: field for field in (templates.get(template_id) or {}).get("fields", [])}
        if field_key not in fields:
            raise ValueError("健康数据字典有误：指标 %s 指向不存在的模板字段 %s.%s" % (code, template_id, field_key))
        covered.add((template_id, field_key))
    missing = []
    for template_id, template in templates.items():
        for field in template.get("fields", []):
            if field.get("kind") == "number" and (template_id, field["key"]) not in covered:
                missing.append("%s.%s" % (template_id, field["key"]))
    if missing:
        raise ValueError("健康数据字典有误：以下数值型模板字段缺少指标定义：%s" % ", ".join(sorted(missing)))
