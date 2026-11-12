"""端到端测试：启动本地静态站点 -> 爬虫抓取 -> 归并核对。

运行方式：
    PYTHONPATH=/tmp/pylibs python3 -I tests/test_end_to_end.py
"""
from __future__ import annotations

import functools
import http.server
import random
import sys
import threading
from datetime import datetime
from pathlib import Path

# -I 隔离模式下 PYTHONPATH 不生效，显式注入依赖目录
for candidate in ("/tmp/pylibs", "/tmp/pyroot/lib/python3.11/site-packages"):
    if Path(candidate).exists() and candidate not in sys.path:
        sys.path.insert(0, candidate)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webapp"))

from app.crawler import crawl  # noqa: E402
from app.merger import merge_announcements  # noqa: E402
from app.models import Announcement, ResultStatus  # noqa: E402
from app.parser import parse_detail  # noqa: E402
from app.utils import TimePrecision, parse_time, parse_time_window, time_precision  # noqa: E402

SITE_DIR = Path(__file__).resolve().parent.parent / "example-site" / "site"

# 项目编号 -> (状态, 当前截止时间, 公告条数)
EXPECTED = {
    "CG-2026-001": (ResultStatus.EFFECTIVE, "2026-11-06 10:00:00", 2),
    "CG-2026-002": (ResultStatus.EFFECTIVE, "2026-10-28 15:00:00", 2),
    "CG-2026-003": (ResultStatus.PENDING, None, 2),
    "CG-2026-004": (ResultStatus.PENDING, None, 3),
    "CG-2026-005": (ResultStatus.EFFECTIVE, "2026-11-02 09:30:00", 1),
    "CG-2026-006": (ResultStatus.UNKNOWN, None, 1),
    # 同分钟冲突期限：先后无法确定 -> 待核实，不显示可用截止时间
    "CG-2026-007": (ResultStatus.PENDING, None, 2),
    # 日期/时刻混用同日冲突先待核实，更晚澄清公布新期限 -> 恢复有效
    "CG-2026-008": (ResultStatus.EFFECTIVE, "2026-11-12 15:30:00", 4),
    # 同分钟"延期无新时间"先待核实，更晚澄清公布新期限 -> 恢复有效
    "CG-2026-009": (ResultStatus.EFFECTIVE, "2026-11-09 10:30:00", 4),
    # 同分钟多条公告期限一致：不冲突，保持有效
    "CG-2026-010": (ResultStatus.EFFECTIVE, "2026-11-04 09:00:00", 3),
}


def start_server() -> str:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return f"http://127.0.0.1:{port}/list/page_1.html"


def assert_equal(actual, expected, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}：期望 {expected!r}，实际 {actual!r}")
    print(f"  ✓ {message}")


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"  ✓ {message}")


def check_time_precision() -> None:
    print("[0] 发布时间精度与时间窗口 ...")
    start, end, precision = parse_time_window("2026-10-25")
    assert_equal(precision, TimePrecision.DATE, "仅日期文本识别为日期精度")
    assert_equal((start, end), (datetime(2026, 10, 25), datetime(2026, 10, 25, 23, 59, 59, 999999)),
                 "日期精度覆盖当天全天窗口")
    assert_equal(time_precision("2026-10-22 14:30"), TimePrecision.MINUTE, "无秒文本识别为分钟精度")
    start, end, precision = parse_time_window("2026-10-22 14:30")
    assert_equal((start, end), (datetime(2026, 10, 22, 14, 30), datetime(2026, 10, 22, 14, 30, 59, 999999)),
                 "分钟精度覆盖该分钟整分钟窗口")
    assert_equal(time_precision("2026-10-25 16:40:00"), TimePrecision.SECOND, "完整时间识别为秒精度")
    assert_equal(time_precision("无法解析的文本"), TimePrecision.NONE, "无法解析返回 NONE")
    # 兼容旧接口：parse_time 返回最早可能时刻
    assert_equal(parse_time("2026年10月25日"), datetime(2026, 10, 25), "parse_time 兼容中文日期文本")
    assert_equal(parse_time(""), None, "空文本 parse_time 返回 None")


def check_order_independence(base_announcements: list[Announcement]) -> None:
    print("[6] 调换同批公告抓取顺序，归并结果必须不变 ...")

    def snapshot(anns: list[Announcement]) -> dict:
        return {
            r.project_no: (r.status, r.current_deadline)
            for r in merge_announcements(anns)
        }

    expected = snapshot(base_announcements)
    rng = random.Random(20261112)
    for trial in range(30):
        shuffled = base_announcements[:]
        rng.shuffle(shuffled)
        assert_equal(snapshot(shuffled), expected, f"第 {trial + 1} 次随机打乱后结果一致")

    # 针对性构造：把 007 的两条同分钟公告分别置于前后，都必须是待核实
    pair = [a for a in base_announcements if a.project_no == "CG-2026-007"]
    assert_equal(len(pair), 2, "007 项目有两条同分钟公告")
    for reverse in (False, True):
        ordered = sorted(pair, key=lambda a: a.deadline or "", reverse=reverse)
        result = merge_announcements(ordered)[0]
        assert_equal(result.status, ResultStatus.PENDING,
                     f"007 同分钟冲突公告（reverse={reverse}）状态为待核实")
        assert_equal(result.current_deadline, None,
                     f"007 同分钟冲突公告（reverse={reverse}）不显示可用截止时间")


def check_merger_units() -> None:
    print("[7] 归并器边界场景（构造数据直接验证） ...")

    def ann(ptime, deadline=None, extension=False, ntype="更正公告", no="UT-1"):
        return Announcement(
            url=f"http://x/{ptime}-{deadline}-{extension}",
            project_no=no, title=f"{no}-{ntype}", notice_type=ntype,
            publish_time=ptime, deadline=deadline, declares_extension=extension,
        )

    # 7a 同分钟：一条带新期限、一条声明延期无新期限 -> 待核实
    r = merge_announcements([
        ann("2026-10-20 11:15", "2026-11-03 14:00:00"),
        ann("2026-10-20 11:15", None, extension=True, ntype="延期公告"),
    ])[0]
    assert_equal(r.status, ResultStatus.PENDING, "同分钟延期无新时间 -> 待核实")
    assert_equal(r.current_deadline, None, "同分钟延期无新时间 -> 不显示截止时间")

    # 7b 同上，但更晚公告（窗口不重叠）给出新期限 -> 恢复有效
    r = merge_announcements([
        ann("2026-10-20 11:15", "2026-11-03 14:00:00"),
        ann("2026-10-20 11:15", None, extension=True, ntype="延期公告"),
        ann("2026-10-23 15:00", "2026-11-09 10:30:00", ntype="澄清公告"),
    ])[0]
    assert_equal(r.status, ResultStatus.EFFECTIVE, "更晚澄清公布新期限 -> 恢复有效")
    assert_equal(r.current_deadline, "2026-11-09 10:30:00", "恢复后采用更晚公告的新期限")

    # 7c 仅日期公告与当天具体时刻公告期限冲突 -> 待核实
    r = merge_announcements([
        ann("2026-10-25", "2026-11-08 10:00:00"),
        ann("2026-10-25 16:40:00", "2026-11-12 15:30:00", ntype="延期公告"),
    ])[0]
    assert_equal(r.status, ResultStatus.PENDING, "日期-only 与同日时刻公告先后不明且期限冲突 -> 待核实")
    assert_equal(r.current_deadline, None, "冲突组不显示任何截止时间")

    # 7d 日期-only 公告与次日公告：窗口不重叠，先后明确，正常替换
    r = merge_announcements([
        ann("2026-10-25", "2026-11-08 10:00:00"),
        ann("2026-10-26 09:00:00", "2026-11-12 15:30:00", ntype="延期公告"),
    ])[0]
    assert_equal(r.status, ResultStatus.EFFECTIVE, "次日公告窗口不重叠 -> 明确更晚，有效")
    assert_equal(r.current_deadline, "2026-11-12 15:30:00", "明确更晚公告的期限生效")

    # 7e 同分钟两条公告给出同一期限 -> 有效，不得误报待核实
    r = merge_announcements([
        ann("2026-10-18 09:45", "2026-11-04 09:00:00"),
        ann("2026-10-18 09:45", "2026-11-04 09:00:00", ntype="澄清公告"),
    ])[0]
    assert_equal(r.status, ResultStatus.EFFECTIVE, "同分钟但期限一致 -> 有效")

    # 7f 发布时间无法解析的公告：与任何公告先后不明，冲突即待核实
    r = merge_announcements([
        ann("2026-10-01 09:00:00", "2026-11-01 09:00:00"),
        ann("发布时间暂缺", "2026-11-05 09:00:00"),
    ])[0]
    assert_equal(r.status, ResultStatus.PENDING, "无法解析时间的冲突公告 -> 待核实")
    assert_true(any("无法解析" in a.order_note for a in r.announcements), "无法解析的公告写入先后提示")

    # 7g 无法解析时间但未产生冲突（只有一条且给期限）-> 有效
    r = merge_announcements([ann("时间未知", "2026-11-05 09:00:00", ntype="招标公告")])[0]
    assert_equal(r.status, ResultStatus.EFFECTIVE, "单条时间未知且给出期限 -> 有效")


def main() -> int:
    check_time_precision()

    entry = start_server()
    print(f"[1] 本地示例站点：{entry}")

    print("[2] 从列表页沿下一页抓取并解析详情 ...")
    report = crawl(entry)
    assert_equal(len(report.visited_list_pages), 2, "遍历了 2 个列表页（下一页跟随成功）")
    assert_equal(len(report.announcements), 24, "解析出 24 条公告（10 个项目）")
    assert_equal(report.errors, [], "抓取过程无错误")

    # 抽查一条详情页的字段标签解析
    first = next(a for a in report.announcements if a.project_no == "CG-2026-001" and a.notice_type == "更正公告")
    assert_equal(first.publish_time, "2026-10-15 16:20:00", "更正公告发布时间按标签正确解析")
    assert_equal(first.deadline, "2026-11-06 10:00:00", "更正公告新截止时间按标签正确解析")

    pending_notice = next(a for a in report.announcements if a.project_no == "CG-2026-003" and a.notice_type == "延期公告")
    assert_equal(pending_notice.deadline, None, "无新时间的延期页截止时间为 None")
    assert_equal(pending_notice.declares_extension, True, "延期页被识别为声明延期")

    # 中文格式、日期-only 发布时间的解析
    date_only = next(a for a in report.announcements if a.project_no == "CG-2026-008" and a.notice_type == "更正公告")
    assert_equal(date_only.publish_time, "2026年10月25日", "中文日期-only 发布时间原样保留")
    assert_equal(time_precision(date_only.publish_time), TimePrecision.DATE, "中文日期-only 识别为日期精度")

    print("[3] 按项目编号归并并按发布时间（含精度）处理 ...")
    results = merge_announcements(report.announcements)
    assert_equal(len(results), 10, "归并出 10 个项目")
    by_no = {r.project_no: r for r in results}

    for project_no, (status, deadline, count) in EXPECTED.items():
        result = by_no[project_no]
        assert_equal(result.status, status, f"{project_no} 状态为 {status.value}")
        assert_equal(result.current_deadline, deadline,
                     f"{project_no} 当前截止时间为 {deadline or '空（不显示旧值）'}")
        assert_equal(len(result.announcements), count, f"{project_no} 保留 {count} 条公告链接")

    print("[4] 公告展示顺序：可确定先后的按时间升序，同批组内保持抓取顺序 ...")
    r8 = by_no["CG-2026-008"]
    times8 = [parse_time(a.publish_time) for a in r8.announcements]
    assert_equal(times8, sorted(times8), "008 公告按最早可能时刻升序展示")
    r7 = by_no["CG-2026-007"]
    assert_true(all("先后无法确定" in a.order_note for a in r7.announcements),
                "007 同分钟公告均标注先后无法确定")
    assert_true(all(a.url for a in r7.announcements), "007 公告保留原文链接")

    print("[5] 解析器兜底能力（dl/dt/dd 与纯文本标签）...")
    alt_html = """
    <html><body><h1>某设备采购更正公告</h1>
      <dl><dt>项目编号：</dt><dd>ALT-999</dd>
          <dt>公告类型</dt><dd>更正公告</dd>
          <dt>发布时间：</dt><dd>2026年11月01日 10:00</dd></dl>
      <p>投标截止时间：2026年11月20日 09:30，特此通知。</p>
    </body></html>
    """
    ann = parse_detail(alt_html, "http://test/alt.html")
    assert_equal(ann.project_no, "ALT-999", "dl/dt/dd 结构解析项目编号")
    assert_equal(ann.notice_type, "更正公告", "dl/dt/dd 结构解析公告类型")
    assert_equal(ann.publish_time, "2026年11月01日 10:00", "dl/dt/dd 结构解析发布时间")
    assert_equal(ann.deadline, "2026年11月20日 09:30", "纯文本标签兜底解析截止时间")

    print("[5b] 无项目编号的页面应被拒绝 ...")
    try:
        parse_detail("<html><body><p>没有字段标签的页面</p></body></html>", "http://test/x.html")
    except ValueError:
        print("  ✓ 无项目编号页面抛出 ValueError")
    else:
        raise AssertionError("无项目编号页面未被拒绝")

    check_order_independence(report.announcements)
    check_merger_units()

    print("\n全部测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
