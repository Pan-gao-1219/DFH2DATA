"""东方红2 · 海上观测资料协作站。Run: streamlit run streamlit_app.py"""
import base64
import html
import json
import uuid
from datetime import time
from pathlib import Path

import streamlit as st

from observations import GROUPS, PROJECTS, TASKS, TASK_BY_ID, now_china, report_text, task_status
from storage import Store

ROOT = Path(__file__).parent
st.set_page_config(page_title="东方红2 · 海上观测协作站", page_icon=":material/sailing:", layout="wide")
st.session_state.setdefault("dialog_task", None)
st.session_state.setdefault("upload_nonce", str(uuid.uuid4()))
st.session_state.setdefault("archive_download", None)
st.session_state.setdefault("intro_seen", False)


@st.cache_data(max_entries=2)
def hero_image():
    photo = ROOT / "assets" / "dongfanghong2.jpg"
    return base64.b64encode(photo.read_bytes()).decode() if photo.exists() else ""


@st.cache_data(max_entries=1)
def intro_image():
    return base64.b64encode((ROOT / "assets" / "team-intro.png").read_bytes()).decode()


def dismiss_dialog():
    st.session_state.dialog_task = None


def open_task(task_id):
    st.session_state.dialog_task = task_id
    st.session_state.upload_nonce = str(uuid.uuid4())


def render_report(record, store, key):
    st.caption("复制下面的文字发到工作群，发送后再确认。")
    st.code(report_text(record), language=None, wrap_lines=True)
    if record["reported_at"]:
        st.success(f"已由 {record['reported_by']} 确认群汇报 · {record['reported_at'][11:16]}")
    else:
        person = st.selectbox("群汇报确认人", GROUPS[TASK_BY_ID[record["task_id"]].group]["members"],
            index=GROUPS[TASK_BY_ID[record["task_id"]].group]["members"].index(record["reporter"]), key=f"confirm_person_{key}")
        if st.button("我已在群里汇报，确认完成", key=f"confirm_{key}", type="primary", icon=":material/task_alt:", width="stretch"):
            try:
                store.mark_reported(record["id"], person)
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))


@st.dialog("提交观测材料", width="large", on_dismiss=dismiss_dialog)
def upload_dialog(task_id, observation_date):
    task = TASK_BY_ID[task_id]
    store = Store()
    latest = store.latest(observation_date).get(task_id)
    st.subheader(f"{task.time} · {task.title}")
    st.caption(f"{observation_date}  /  {task.group}组 · 组长 {GROUPS[task.group]['leader']}")
    if latest:
        st.success(f"材料已保存 · {len(latest['files'])} 个文件 · 提交人 {latest['reporter']}")
        render_report(latest, store, f"dialog_{latest['id']}")
        again = st.toggle("重新提交完整材料（保留历史记录）", key=f"again_{task_id}")
        if not again:
            return
        st.caption("新提交需要重新确认群汇报；上一版材料仍可在资料库下载。")
    nonce = st.session_state.upload_nonce
    with st.form(f"upload_{task_id}_{nonce}"):
        reporter = st.selectbox("提交人", GROUPS[task.group]["members"], index=None, placeholder="选择本组成员", key=f"reporter_{nonce}")
        c1, c2 = st.columns(2)
        actual = c1.time_input("实际完成时间", value=time(task.hour), step=60)
        station = c2.text_input("站位 / 样品编号（选填）", max_chars=120)
        if task.project == "ctd":
            st.caption("CTD与气象同步测量，两类材料都需要提交。合并文件可分别选择同一份。")
            ctd = st.file_uploader("CTD原始数据与记录", accept_multiple_files=True, max_upload_size=100, key=f"ctd_files_{nonce}")
            weather = st.file_uploader("同步气象数据与记录", accept_multiple_files=True, max_upload_size=100, key=f"weather_files_{nonce}")
            uploads = [(file, "ctd") for file in ctd] + [(file, "weather") for file in weather]
        else:
            label = "盐度 / pH测量数据与记录" if task.project == "water" else f"第{task.round}次拖网数据、样品记录与照片"
            uploads = [(file, "data") for file in st.file_uploader(label, accept_multiple_files=True, max_upload_size=100, key=f"data_files_{nonce}")]
        notes = st.text_area("备注（选填）", max_chars=2000, placeholder="异常情况、样品说明或需要交接的事项")
        st.caption("支持仪器原始文件、表格、文档、照片和压缩包。每次最多20个，单个100MB，总计250MB。")
        submitted = st.form_submit_button("上传材料并生成群汇报", type="primary", icon=":material/cloud_upload:", width="stretch")
    if submitted:
        if not reporter:
            st.error("请先选择提交人。")
            return
        try:
            with st.spinner("正在保存材料…"):
                store.submit(observation_date=observation_date, task_id=task_id, reporter=reporter,
                    actual_time=actual.strftime("%H:%M"), station=station, notes=notes,
                    files=[(file.name, file.getvalue(), category) for file, category in uploads], submission_id=nonce)
            st.session_state.upload_nonce = str(uuid.uuid4())
            st.rerun()
        except (ValueError, OSError) as exc:
            st.error(f"提交未完成：{exc}")


css = ROOT / "assets" / "style.css"
if css.exists():
    st.html(f"<style>{css.read_text(encoding='utf-8')}</style>")

# The one-second hold and fade happen in the browser, without blocking Python.
# Session state prevents an upload, filter or report confirmation replaying it.
if not st.session_state.intro_seen:
    st.html(f'<div class="intro-screen" aria-label="D海研学队开场海报"><img src="data:image/png;base64,{intro_image()}" alt="D海研学队：同舟探海，向海求知；五校同行，共赴山海。"></div>')
    st.session_state.intro_seen = True

with st.sidebar:
    st.markdown("### :material/sailing: 东方红2")
    st.caption("海上观测 · 数据协作")
    st.divider()
    observation_date = st.date_input("观测日期", value=now_china().date(), key="observation_date").isoformat()
    group_filter = st.selectbox("查看小组", [0, 1, 2, 3, 4], format_func=lambda g: "全部小组" if g == 0 else f"第 {g} 组", key="group_filter")
    if st.button("刷新协作进度", icon=":material/refresh:", width="stretch"):
        st.rerun()
    st.divider()
    st.markdown("**前班 · 1、2组**")
    st.caption("02:00—05:30")
    st.markdown("**后班 · 3、4组**")
    st.caption("05:20—08:00")
    st.info("05:20—05:30 交接\n\n1组 → 3组\n\n2组 → 4组", icon=":material/swap_horiz:")
    st.caption("每次完成观测 → 上传材料 → 群内汇报")

image = hero_image()
photo_style = f"background-image:linear-gradient(90deg,rgba(5,24,39,.93) 0%,rgba(5,24,39,.52) 45%,rgba(5,24,39,.08) 100%),url(data:image/jpeg;base64,{image});" if image else ""
st.html(f"""<section class="hero" style="{photo_style}">
    <div class="eyebrow">DONG FANG HONG 2 &nbsp; / &nbsp; FIELD OBSERVATIONS</div>
    <h1>向海而行，<br>让每一份观测有迹可循。</h1>
    <p>东方红2 · 海上观测协作站</p>
    <div class="hero-tags"><span>02:00 — 08:00</span><span>4个小组</span><span>17项作业</span></div>
    </section>""")

view = st.segmented_control("工作区", ["观测任务", "数据资料库", "值班与交接"], default="观测任务", key="workspace", label_visibility="collapsed") or "观测任务"
store = Store()
latest = store.latest(observation_date)
tasks = [task for task in TASKS if not group_filter or task.group == group_filter]
uploaded = sum(task.id in latest for task in tasks)
completed = sum(bool(latest.get(task.id, {}).get("reported_at")) for task in tasks)
with st.container(horizontal=True, gap="medium"):
    st.metric("计划作业", f"{len(tasks):02d}", border=True)
    st.metric("已上传材料", f"{uploaded:02d}", border=True)
    st.metric("待群汇报", f"{uploaded - completed:02d}", border=True)
    st.metric("已全部完成", f"{completed:02d}", border=True)
st.progress(completed / len(tasks), text=f"{observation_date} · 完成 {completed} / {len(tasks)} 项（上传并确认群汇报）")

if view == "观测任务":
    plan, updates = st.columns([3.2, 1], gap="large")
    with plan:
        with st.container(border=True, key="plan"):
            st.subheader("整点观测计划")
            st.caption("完成观测后，点击对应任务上传材料。CTD与气象同步提交。")
            headers = st.columns([0.7, 1.25, 1.25, 1.25])
            for col, title, frequency in zip(headers, ["观测时刻", "CTD / 气象", "盐度 / pH", "生物拖网"], ["", "每小时", "每小时", "每3小时"]):
                col.markdown(f"**{title}**")
                if frequency:
                    col.caption(frequency)
            for hour in range(2, 9):
                st.divider()
                cols = st.columns([0.7, 1.25, 1.25, 1.25], vertical_alignment="center")
                cols[0].markdown(f"**{hour:02d}:00**")
                if hour == 6:
                    cols[0].caption("后班开始")
                for col, project in zip(cols[1:], PROJECTS):
                    task_id = f"{project}_{hour:02d}"
                    task = TASK_BY_ID.get(task_id)
                    if not task:
                        col.caption("—")
                        continue
                    if group_filter and task.group != group_filter:
                        col.caption(f"{task.group}组负责")
                        continue
                    status = task_status(latest.get(task_id))
                    icon = {"待上传": ":material/upload_file:", "待群报": ":material/chat_bubble:", "已完成": ":material/check_circle:"}[status]
                    col.button(f"{task.group}组 · {status}", key=f"task_{task_id}", icon=icon, width="stretch",
                        help=f"{task.time} · {task.title} · {GROUPS[task.group]['leader']}组", on_click=open_task, args=(task_id,))
            st.divider()
            st.caption("● 1、2组 02:00—05:30　　● 3、4组 05:20—08:00")
    with updates:
        with st.container(border=True):
            st.subheader("最新提交")
            recent = sorted((r for key, r in latest.items() if TASK_BY_ID[key] in tasks), key=lambda r: r["sequence"], reverse=True)[:5]
            if not recent:
                st.caption("等待第一份海上观测材料。")
                st.markdown(":material/water: **让数据跟上每一次出海。**")
            for record in recent:
                task = TASK_BY_ID[record["task_id"]]
                st.markdown(f"**{task.time} · {PROJECTS[task.project]}**")
                st.caption(f"{task.group}组 · {record['reporter']} · {len(record['files'])}个文件")
                st.caption(task_status(record))
                st.button("查看材料与汇报", key=f"recent_{record['id']}", on_click=open_task, args=(task.id,), width="stretch")
                st.divider()
        with st.container(border=True):
            st.markdown("**05:20—05:30 交接**")
            st.caption("前班留几名人员，后班05:20到岗。交清仪器、数据、样品和未完成事项。")
            st.caption("08:00仍安排最后一轮测量与第3次拖网。")

elif view == "数据资料库":
    st.subheader("数据资料库")
    st.caption("按观测日期归档。每次重新提交均保留历史材料，上传文件不会随页面刷新丢失。")
    records = [record for record in store.list_records(observation_date) if not group_filter or TASK_BY_ID[record["task_id"]].group == group_filter]
    if not records:
        st.info("当前日期和小组还没有上传记录。请在“观测任务”中选择相应任务。", icon=":material/folder_open:")
    else:
        options = {record["id"]: record for record in records}
        chosen = st.selectbox("选择一条提交", list(options), format_func=lambda rid: f"{TASK_BY_ID[options[rid]['task_id']].time} · {TASK_BY_ID[options[rid]['task_id']].title} · {options[rid]['reporter']} · {options[rid]['created_at'][11:19]} · {rid[:8]}", key="record_choice")
        record = options[chosen]
        materials, reporting = st.columns([1.4, 1], gap="large")
        with materials, st.container(border=True):
            st.markdown("#### 原始材料")
            st.caption(f"实际完成 {record['actual_time']} · 站位 {record['station'] or '未填写'}")
            for file in record["files"]:
                category = {"ctd": "CTD", "weather": "同步气象", "data": "观测材料"}[file["category"]]
                st.markdown(f"**{html.escape(file['name'])}**")
                st.caption(f"{category} · {file['size'] / 1024:.1f} KB")
                try:
                    st.download_button("下载文件", data=store.read_file(file), file_name=file["name"], mime="application/octet-stream", key=f"file_{file['id']}", icon=":material/download:")
                except (OSError, ValueError) as exc:
                    st.error(f"文件暂不可读：{exc}")
            if st.button("打包本条提交", icon=":material/folder_zip:", key=f"bundle_{chosen}"):
                try:
                    with st.spinner("正在整理材料…"):
                        st.session_state.archive_download = (chosen, store.bundle(chosen))
                except (OSError, ValueError) as exc:
                    st.error(f"打包失败：{exc}")
            cached = st.session_state.archive_download
            if cached and cached[0] == chosen:
                st.download_button("下载材料包（ZIP）", cached[1], file_name=f"{observation_date}_{record['task_id']}_{chosen[:8]}.zip", mime="application/zip", icon=":material/download:")
        with reporting, st.container(border=True):
            st.markdown("#### 群汇报")
            if latest[record["task_id"]]["id"] == chosen:
                render_report(record, store, f"library_{chosen}")
            else:
                st.caption("这是历史版本。完成状态以该任务的最新提交为准。")
                st.code(report_text(record), language=None, wrap_lines=True)
        export = json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8")
        st.download_button("下载当天提交清单（JSON）", export, file_name=f"{observation_date}_提交清单.json", mime="application/json", icon=":material/list_alt:")

else:
    st.subheader("值班与交接")
    st.caption("按已确认的工作分配执行。每组6人，共24人。")
    for group in (1, 3, 2, 4):
        info = GROUPS[group]
        with st.container(border=True):
            st.markdown(f"#### {group}组 · {info['work']}")
            st.caption(f"{info['shift']}　｜　组长：{info['leader']}")
            st.markdown("、".join(info["members"]))
    st.info("05:20开始交接，05:30交接完成。1组交给3组；2组交给4组。05:00任务由前班完成，06:00起由后班负责。", icon=":material/swap_horiz:")
    st.markdown("**交接内容：** 仪器状态、材料保存、样品编号、异常情况和未完成事项。")
    st.caption("频次：CTD＋同步气象7轮；盐度/pH7轮；生物拖网02:00、05:00、08:00各1次。")

st.caption("东方红2 · 海上观测协作站　/　每一份材料，都来自真实的观测。")
if st.session_state.dialog_task:
    upload_dialog(st.session_state.dialog_task, observation_date)
