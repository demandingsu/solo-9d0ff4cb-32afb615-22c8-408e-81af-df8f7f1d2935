"""通用工具：文本清洗、时间解析（含精度）、同源判断。"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from enum import Enum
from urllib.parse import urlparse


class TimePrecision(str, Enum):
    """发布时间文本的精度。

    精度决定两条公告能否仅凭发布时间确定先后：
    - 仅标注日期的公告，其可能发布时刻覆盖当天 00:00:00~23:59:59；
    - 只写到分钟的公告覆盖该分钟整分钟；
    因此同一天内"日期-only"与"具体时刻"、或同一分钟内的两条公告，
    都可能无法确定先后，归并时必须按"先后不明"处理。
    """

    SECOND = "精确到秒"
    MINUTE = "精确到分钟"
    DATE = "仅标注日期"
    NONE = "未识别"


# (strptime 格式, 对应精度)
_TIME_FORMATS: tuple[tuple[str, TimePrecision], ...] = (
    ("%Y-%m-%d %H:%M:%S", TimePrecision.SECOND),
    ("%Y-%m-%d %H:%M", TimePrecision.MINUTE),
    ("%Y-%m-%d", TimePrecision.DATE),
)

# 兜底正则抓到片段后尝试的格式（与精度对应）
_FRAGMENT_FORMATS: tuple[tuple[str, TimePrecision], ...] = (
    ("%Y-%m-%d %H:%M:%S", TimePrecision.SECOND),
    ("%Y-%m-%d %H:%M", TimePrecision.MINUTE),
    ("%Y-%m-%d", TimePrecision.DATE),
)

_DAY_END = timedelta(hours=23, minutes=59, seconds=59, microseconds=999999)
_MINUTE_END = timedelta(seconds=59, microseconds=999999)


def strip_label(text: str) -> str:
    """去掉字段标签末尾的冒号与空白。"""
    return re.sub(r"[\s:：*]+$", "", text or "").strip()


def _normalize(text: str) -> str:
    normalized = (
        text.strip()
        .replace("年", "-")
        .replace("月", "-")
        .replace("日", " ")
        .replace("时", ":")
        .replace("分", "")
        .replace("：", ":")
        .replace("/", "-")
        .replace(".", "-")
    )
    return re.sub(r"\s+", " ", normalized).strip()


def _window_for(dt: datetime, precision: TimePrecision) -> tuple[datetime, datetime]:
    """按精度把单个时间点扩展为可能发布时刻的闭区间 [start, end]。"""
    if precision is TimePrecision.DATE:
        start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + _DAY_END
    if precision is TimePrecision.MINUTE:
        start = dt.replace(second=0, microsecond=0)
        return start, start + _MINUTE_END
    return dt, dt


def parse_time_window(
    text: str,
) -> tuple[datetime, datetime, TimePrecision] | None:
    """把时间文本解析为 (最早可能时刻, 最晚可能时刻, 精度)，失败返回 None。

    日期-only 文本（如 "2026-10-19"）无法确定当天具体时刻，返回当天全天窗口；
    分钟精度文本（如 "2026-10-18 14:00"）返回该分钟窗口；
    秒精度文本窗口退化为单个时刻。
    """
    if not text:
        return None
    normalized = _normalize(text)
    for fmt, precision in _TIME_FORMATS:
        try:
            dt = datetime.strptime(normalized, fmt)
        except ValueError:
            continue
        start, end = _window_for(dt, precision)
        return start, end, precision

    # 最后兜底：直接抓取日期片段
    match = re.search(
        r"(\d{4}-\d{1,2}-\d{1,2})(?:[ T](\d{1,2}:\d{2}(?::\d{2})?))?", normalized
    )
    if match:
        fragment = match.group(1) + ((" " + match.group(2)) if match.group(2) else "")
        for fmt, precision in _FRAGMENT_FORMATS:
            try:
                dt = datetime.strptime(fragment, fmt)
            except ValueError:
                continue
            start, end = _window_for(dt, precision)
            return start, end, precision
    return None


def parse_time(text: str) -> datetime | None:
    """解析时间文本，返回最早可能时刻；无法解析返回 None。"""
    window = parse_time_window(text)
    return window[0] if window else None


def time_precision(text: str) -> TimePrecision:
    """返回时间文本的精度标签，无法解析时为 NONE。"""
    window = parse_time_window(text)
    return window[2] if window else TimePrecision.NONE


def same_origin(url_a: str, url_b: str) -> bool:
    """判断两个 URL 是否同站点（协议+主机+端口一致）。"""
    parsed_a, parsed_b = urlparse(url_a), urlparse(url_b)
    if not parsed_a.scheme or not parsed_a.netloc:
        return False
    return (parsed_a.scheme, parsed_a.netloc) == (parsed_b.scheme, parsed_b.netloc)
