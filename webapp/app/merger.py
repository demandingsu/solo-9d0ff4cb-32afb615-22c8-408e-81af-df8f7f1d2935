"""按项目编号归并公告，按发布时间（含精度）处理投标截止时间。

核心口径：发布时间无法确定先后的公告不得依赖抓取顺序裁决。

1. 先把每条公告的发布时间解析为"可能发布时刻窗口"：
   仅标注日期的覆盖当天全天、只写到分钟的覆盖该分钟、秒级精度退化为时刻点；
   时间无法解析的公告窗口为无穷区间，与任何公告先后都无法确定。
2. 窗口相交（或相接于同一时刻）的公告归入同一"同批组"，组内先后不明；
   各组之间窗口互不相交，可严格按时间从早到晚处理。
3. 同批组内：
   - 若有公告声明延期却未给新截止时间 -> 整组待核实，不显示任何可用截止时间；
   - 若组内出现互相冲突的截止时间 -> 整组待核实（无法判断哪一个是最终期限）；
   - 否则沿用唯一的截止时间（组内给出的期限一致），不改变既有状态。
4. 只有明确晚于待核实组的后续公告公布新截止时间，才恢复"有效"。
5. 从未出现过截止时间的项目标记为"未公布"。

整套处理只依赖发布时间文本本身，与列表页/抓取顺序无关，调换同批公告顺序结果不变。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .models import Announcement, ProjectResult, ResultStatus
from .utils import TimePrecision, parse_time, parse_time_window, time_precision

_CORRECTION_TYPE_RE = re.compile(r"更正|变更|澄清|修改|延期|顺延|补充")
_INF = None  # 用 None 表示无穷端点，避免 datetime 最大值参与运算


@dataclass
class _TimedAnnouncement:
    """带发布时间窗口的公告。"""

    ann: Announcement
    start: object  # datetime | None（None 表示负无穷）
    end: object    # datetime | None（None 表示正无穷）


def _is_correction(ann: Announcement) -> bool:
    return bool(_CORRECTION_TYPE_RE.search(ann.notice_type)) or ann.declares_extension


def _extract_project_name(ann: Announcement) -> str:
    """从公告标题中尽量取出项目名称。"""
    if not ann.title:
        return ""
    name = re.sub(r"[-_—–|].{0,4}(?:招标|采购|更正|变更|澄清|修改|延期|顺延|中标|成交|终止).{0,4}公告$", "", ann.title)
    name = re.sub(r"^(?:.*?关于)?(.*?)(?:项目)?(?:的)?(?:招标|采购|更正|变更|澄清|修改|延期|顺延|中标|成交|终止|废标)公告.*$", r"\1", name)
    return name.strip("-_—–| ")


def _build_timed(items: list[Announcement]) -> list[_TimedAnnouncement]:
    """为每条公告构造发布时间窗口；解析失败时窗口为 (-∞, +∞)。"""
    timed: list[_TimedAnnouncement] = []
    for ann in items:
        window = parse_time_window(ann.publish_time)
        if window is None:
            start, end = _INF, _INF
        else:
            start, end, _precision = window
        timed.append(_TimedAnnouncement(ann=ann, start=start, end=end))
    return timed


def _windows_overlap(a: _TimedAnnouncement, b: _TimedAnnouncement) -> bool:
    """两个发布时间窗口是否相交/相接（相接于同一时刻同样无法严格区分先后）。

    含无穷窗口的公告与任何公告先后都无法确定。
    """
    if a.start is _INF or a.end is _INF or b.start is _INF or b.end is _INF:
        return True
    return a.start <= b.end and b.start <= a.end


def _group_by_time(timed: list[_TimedAnnouncement]) -> list[list[_TimedAnnouncement]]:
    """按窗口重叠关系分组（重叠关系可传递），组间按窗口起点从早到晚排序。

    无穷窗口会把全部公告并入同一组（其与任何公告先后不明）。
    """
    n = len(timed)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    for i in range(n):
        for j in range(i + 1, n):
            if _windows_overlap(timed[i], timed[j]):
                union(i, j)

    groups_map: dict[int, list[_TimedAnnouncement]] = {}
    for i, item in enumerate(timed):
        groups_map.setdefault(find(i), []).append(item)

    groups = list(groups_map.values())

    def group_key(group: list[_TimedAnnouncement]):
        # 含无穷窗口的组排在最后（无法定位时刻）；其余按最早可能时刻排序
        starts = [g.start for g in group if g.start is not _INF]
        return (1, min(starts)) if starts else (0, datetime.min)

    groups.sort(key=group_key)
    return groups


def _deadline_equal(a: str, b: str) -> bool:
    """判断两个截止时间原文是否指同一时刻；能解析就按时刻比较，否则按文本比较。"""
    ta, tb = parse_time(a), parse_time(b)
    if ta is not None and tb is not None:
        return ta == tb
    return a.strip() == b.strip()


def _deadline_label(deadline: str) -> str:
    return f"“{deadline}”"


def merge_announcements(announcements: list[Announcement]) -> list[ProjectResult]:
    grouped: dict[str, list[Announcement]] = {}
    # dict 保序：记录抓取顺序，作为同批组内的稳定展示次序（不参与状态裁决）
    for ann in announcements:
        grouped.setdefault(ann.project_no, []).append(ann)

    results: list[ProjectResult] = []
    for project_no, items in grouped.items():
        # 展示排序：先按发布时间窗口起点（无法解析的排最后），再按窗口终点，
        # 窗口相同（同分钟/同日期的同批公告）保持抓取顺序。状态裁决不依赖该顺序。
        def display_key(ann: Announcement) -> tuple[int, datetime, datetime]:
            window = parse_time_window(ann.publish_time)
            if window is None:
                return (1, datetime.max, datetime.max)
            start, end, _precision = window
            return (0, start, end)

        items = sorted(items, key=display_key)
        timed = _build_timed(items)
        groups = _group_by_time(timed)

        current_deadline: str | None = None
        pending = False
        pending_note = ""

        for group in groups:
            group_items = [t.ann for t in group]
            ambiguous = len(group) > 1  # 组内多于一条 -> 彼此先后无法确定

            # 1) 声明延期却没有新截止时间：无论同批组内其他公告写了什么，都待核实
            extension_without_time = [
                a for a in group_items
                if not a.deadline and a.declares_extension and _is_correction(a)
            ]
            if extension_without_time:
                current_deadline = None
                pending = True
                src = extension_without_time[0]
                if ambiguous:
                    pending_note = (
                        f"公告（{src.notice_type or '更正公告'}，{src.publish_time or '发布时间未知'}）"
                        "声明延期但未写明新的投标截止时间，且其发布时间与同批公告无法确定先后，"
                        "旧时间不再视为有效，需人工核实。"
                    )
                else:
                    pending_note = (
                        f"较晚公告（{src.notice_type or '更正公告'}，{src.publish_time or '发布时间未知'}）"
                        "声明延期但未写明新的投标截止时间，旧时间不再视为有效，需人工核实。"
                    )
                continue

            # 2) 同批组内出现互相冲突的截止时间：无法判断哪个是最终期限
            deadlines: list[str] = []
            for a in group_items:
                if a.deadline and not any(_deadline_equal(a.deadline, d) for d in deadlines):
                    deadlines.append(a.deadline)
            if ambiguous and len(deadlines) > 1:
                current_deadline = None
                pending = True
                options = "、".join(_deadline_label(d) for d in deadlines)
                times = "、".join(a.publish_time or "发布时间未知" for a in group_items)
                pending_note = (
                    f"同批公告（发布时间 {times}）先后无法确定，却给出相互冲突的投标截止时间"
                    f"（{options}），无法判断最终期限，需人工核实；在此之前不显示可用截止时间。"
                )
                continue

            # 3) 组内期限一致（或只有一条公告给出期限）：公布即替换，并可解除待核实
            if deadlines:
                current_deadline = deadlines[0]
                pending = False
                pending_note = ""

        # 为每条公告写入先后提示，供浏览器展示
        _annotate_order(timed, groups)

        if pending:
            status = ResultStatus.PENDING
            note = pending_note
        elif current_deadline:
            status = ResultStatus.EFFECTIVE
            note = "当前截止时间取自主明截止时间且发布时间明确有效的最新公告。"
        else:
            status = ResultStatus.UNKNOWN
            note = "已抓取公告中均未公布投标截止时间。"

        ordered_items = [t.ann for t in timed]
        results.append(
            ProjectResult(
                project_no=project_no,
                project_name=_extract_project_name(ordered_items[-1]) or ordered_items[-1].title,
                status=status,
                current_deadline=current_deadline,
                note=note,
                announcements=ordered_items,
            )
        )

    # 项目编号排序，保证展示稳定
    results.sort(key=lambda r: r.project_no)
    return results


def _annotate_order(
    timed: list[_TimedAnnouncement], groups: list[list[_TimedAnnouncement]]
) -> None:
    """把发布时间精度与同批先后提示写回公告，供页面展示。"""
    for item in timed:
        ann = item.ann
        precision = time_precision(ann.publish_time)
        if precision is TimePrecision.NONE:
            ann.order_note = "发布时间无法解析，与其他公告的先后无法确定"
        elif precision is TimePrecision.DATE:
            ann.order_note = "仅标注发布日期，当天具体时刻未知"
        elif precision is TimePrecision.MINUTE:
            ann.order_note = "发布时间精确到分钟"
        else:
            ann.order_note = "发布时间精确到秒"

    for group in groups:
        if len(group) <= 1:
            continue
        # 同批组：收集各自的精度标签，提示先后不明
        for item in group:
            precision = time_precision(item.ann.publish_time)
            if precision is TimePrecision.NONE:
                item.ann.order_note = "发布时间无法解析，与同批公告先后无法确定"
            else:
                item.ann.order_note += "，与同批公告的发布先后无法确定"
