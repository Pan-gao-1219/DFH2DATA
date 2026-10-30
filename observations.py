"""Confirmed roster and hourly observation tasks. No generated observations."""
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

CHINA_TZ = timezone(timedelta(hours=8))
GROUPS = {
    1: {"leader": "马刚", "members": ["马刚", "潘高", "李朝廷", "李文轩", "吕文哲", "石煜平"], "shift": "02:00—05:30", "work": "CTD测量＋同步气象观测（分别提交）"},
    3: {"leader": "赵一昊", "members": ["赵一昊", "刘聪", "王鹏瑞", "陈冠润", "姜震", "胡盛宇"], "shift": "05:20—08:00", "work": "CTD测量＋同步气象观测（分别提交）"},
    2: {"leader": "孙嘉远", "members": ["孙嘉远", "黄一帆", "余怡晗", "李瑞哲", "郭堃晖", "王巍桥"], "shift": "02:00—05:30", "work": "海水盐度/pH＋生物观测与取样"},
    4: {"leader": "李彦吉", "members": ["李彦吉", "李炼芳", "杨宁华", "程凯鹭", "贾浩林", "李俊霆"], "shift": "05:20—08:00", "work": "海水盐度/pH＋生物观测与取样"},
}
PROJECTS = {"ctd": "CTD测量", "weather": "同步气象观测", "water": "海水盐度 / pH", "trawl": "海洋生物观测"}


@dataclass(frozen=True)
class Task:
    id: str
    hour: int
    project: str
    group: int
    round: int = 0

    @property
    def time(self):
        return f"{self.hour:02d}:00"

    @property
    def requires_sampling(self):
        return self.project == "trawl" and self.round > 0

    @property
    def title(self):
        if self.project == "trawl":
            return f"海洋生物观测＋拖网取样 · 第{self.round}次取样" if self.requires_sampling else "海洋生物观测（不取样）"
        return PROJECTS[self.project]


TASKS = tuple(
    task
    for hour in range(2, 9)
    for task in (
        Task(f"ctd_{hour:02}", hour, "ctd", 1 if hour <= 5 else 3),
        Task(f"weather_{hour:02}", hour, "weather", 1 if hour <= 5 else 3),
        Task(f"water_{hour:02}", hour, "water", 2 if hour <= 5 else 4),
        Task(f"trawl_{hour:02}", hour, "trawl", 2 if hour <= 5 else 4, {2: 1, 5: 2, 8: 3}.get(hour, 0)),
    )
)
TASK_BY_ID = {task.id: task for task in TASKS}


def now_china():
    return datetime.now(CHINA_TZ)


def task_status(record):
    if not record:
        return "待上传"
    return "已完成" if record["reported_at"] else "待群报"


def report_text(record):
    task = TASK_BY_ID[record["task_id"]]
    lines = [
        "【东方红2 · 海上观测汇报】",
        f"观测日期：{record['observation_date']}",
        f"计划时刻：{task.time}｜{task.title}",
        f"负责小组：{task.group}组｜汇报人：{record['reporter']}",
        f"实际完成：{record['actual_time']}",
    ]
    if record["station"]:
        lines.append(f"站位/样品编号：{record['station']}")
    lines.extend([f"材料已上传：{len(record['files'])} 个文件", f"提交编号：{record['id']}"])
    if record["notes"]:
        lines.append(f"备注：{record['notes']}")
    return "\n".join(lines)
