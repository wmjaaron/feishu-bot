#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书工作填报机器人（动态添加记录版 · Card JSON 2.0）
====================================================
- 卡片使用 Card JSON 2.0（"schema": "2.0"）
- 所有交互组件（下拉 + 输入框 + 按钮）都在同一个 form 表单容器里
- **每一个按钮**（提交 / ＋添加 / ×删除）都设置 form_action_type="submit"，
  这样每次点击都会把当前表单里所有 name 字段一起回传（放在 event.action.form_value 里），
  我们再根据 value.action 分派：
    - action=add_record     → 重新生成一张多 1 条记录的卡片，已填内容通过
                              input.default_value / select.initial_option 回填
    - action=remove_record  → 类似 add_record，但少 1 条
    - action=form_submit    → 校验 → 发审批卡给高文杰 → 审批通过写入多维表格
- 卡片刷新方式：直接在 P2CardActionTriggerResponse 的 body 里返回
  {"toast": {...}, "card": {"type": "raw", "data": <new_card_json>}}
- 顶部只保留「填报人」下拉；工作类型移到每条记录里。
- 最多 5 条记录，达到上限后不再显示「＋添加」按钮。
- 至少保留 1 条记录；只有 1 条时不显示「×删除」按钮。
"""

import json
import logging
import os
import re
import traceback
import uuid
from copy import deepcopy
from datetime import datetime
from threading import Lock
from typing import Any, Dict, List, Optional

import lark_oapi as lark
from lark_oapi.api.bitable.v1 import AppTableRecord, CreateAppTableRecordRequest
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    P2ImMessageReceiveV1,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
    force=True,
)
log = logging.getLogger("feishu_bot")

# ============================== 配置 ==============================
APP_ID = os.getenv("FEISHU_APP_ID", "").strip()
APP_SECRET = os.getenv("FEISHU_APP_SECRET", "").strip()
APPROVER_OPEN_ID = os.getenv("FEISHU_APPROVER_OPEN_ID", "").strip()
APPROVER_NAME = os.getenv("FEISHU_APPROVER_NAME", "高文杰").strip()
BITABLE_APP_TOKEN = os.getenv("FEISHU_BITABLE_APP_TOKEN", "ARc5bbQNVa61Ips43necdharnRc").strip()
BITABLE_TABLE_ID = os.getenv("FEISHU_BITABLE_TABLE_ID", "tblHs7BJeH25iPp1").strip()

F_DATE = os.getenv("FEISHU_BITABLE_F_DATE", "日期")
F_SUBMITTER = os.getenv("FEISHU_BITABLE_F_SUBMITTER", "填报人")
F_WORK_TYPE = os.getenv("FEISHU_BITABLE_F_WORK_TYPE", "工作类型")
F_TIME_RANGE = os.getenv("FEISHU_BITABLE_F_TIME_RANGE", "时间段")
F_EVENT = os.getenv("FEISHU_BITABLE_F_EVENT", "事件")
F_PROGRESS = os.getenv("FEISHU_BITABLE_F_PROGRESS", "完成量级/是否完成")
F_FOLLOWUP = os.getenv("FEISHU_BITABLE_F_FOLLOWUP", "后续跟进备注")
F_STATUS = os.getenv("FEISHU_BITABLE_F_STATUS", "审批状态")

SUBMITTER_OPTIONS = ["王港", "曾玥", "文明杰", "金润田", "刘庆"]
WORK_TYPE_OPTIONS = [
    ("standard", "标准工作"),
    ("non_standard", "非标准工作"),
]

MAX_RECORDS = int(os.getenv("FEISHU_MAX_RECORDS", "5"))
INITIAL_SLOTS = 1

for k, v in (
    ("FEISHU_APP_ID", APP_ID),
    ("FEISHU_APP_SECRET", APP_SECRET),
    ("FEISHU_APPROVER_OPEN_ID", APPROVER_OPEN_ID),
):
    if not v:
        raise RuntimeError(f"❌ 缺少环境变量：{k}")

client = (
    lark.Client.builder()
    .app_id(APP_ID)
    .app_secret(APP_SECRET)
    .log_level(lark.LogLevel.INFO)
    .build()
)

# ============================== 状态 ==============================
_lock = Lock()
# 已提交待审批的批次
approval_state: Dict[str, Dict[str, Any]] = {}


# ============================== 发消息工具 ==============================
def send_text(open_id: str, text: str) -> None:
    req = (
        CreateMessageRequest.builder()
        .receive_id_type("open_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(open_id)
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )
        .build()
    )
    resp = client.im.v1.message.create(req)
    if not resp.success():
        log.error(f"send_text 失败: {resp.code} {resp.msg}")


def send_card(open_id: str, card: dict) -> Optional[str]:
    req = (
        CreateMessageRequest.builder()
        .receive_id_type("open_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(open_id)
            .msg_type("interactive")
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        )
        .build()
    )
    resp = client.im.v1.message.create(req)
    if not resp.success():
        log.error(f"send_card 失败: {resp.code} {resp.msg}")
        return None
    return resp.data.message_id


# ============================== 写多维表格 ==============================
def write_to_bitable(record: dict, work_type_label: str, submitter: str, status: str) -> None:
    fields = {
        F_DATE: datetime.now().strftime("%Y-%m-%d"),
        F_SUBMITTER: submitter,
        F_WORK_TYPE: work_type_label,
        F_TIME_RANGE: record.get("time_range", ""),
        F_EVENT: record.get("event", ""),
        F_PROGRESS: record.get("progress", ""),
        F_FOLLOWUP: record.get("followup", ""),
        F_STATUS: status,
    }
    fields = {k: v for k, v in fields.items() if v}
    req = (
        CreateAppTableRecordRequest.builder()
        .app_token(BITABLE_APP_TOKEN)
        .table_id(BITABLE_TABLE_ID)
        .request_body(AppTableRecord.builder().fields(fields).build())
        .build()
    )
    resp = client.bitable.v1.app_table_record.create(req)
    if resp.success():
        log.info("✅ 写入多维表格成功")
    else:
        log.error(f"❌ 写入多维表格失败: {resp.code} {resp.msg} fields={fields}")


# ============================== 卡片基元 ==============================
def _plain(content: str) -> dict:
    return {"tag": "plain_text", "content": content}


def _select_option(value: str, text: str) -> dict:
    return {"value": value, "text": _plain(text)}


def _label_md(text: str) -> dict:
    """在交互组件前面加一行 markdown，充当字段说明（select_static / input 都不支持 label 属性）。"""
    return {"tag": "markdown", "content": f"**{text}**"}


def _input(
    name: str,
    placeholder: str,
    prefill: Optional[Dict[str, Any]] = None,
    required: bool = False,
    max_length: int = 200,
) -> dict:
    """Card 2.0 input，用 default_value 回填已填内容。"""
    d = {
        "tag": "input",
        "name": name,
        "placeholder": _plain(placeholder),
        "required": required,
        "max_length": max_length,
        "input_type": "text",
        "width": "fill",
        "margin": "4px 0px 4px 0px",
    }
    if prefill:
        val = prefill.get(name)
        if val:
            d["default_value"] = str(val)
    return d


def _select_static(
    name: str,
    placeholder: str,
    options: List[dict],
    prefill: Optional[Dict[str, Any]] = None,
    required: bool = False,
) -> dict:
    """Card 2.0 select_static，用 initial_option 回填已选择项。"""
    d = {
        "tag": "select_static",
        "name": name,
        "placeholder": _plain(placeholder),
        "required": required,
        "options": options,
        "width": "fill",
    }
    if prefill:
        cur = prefill.get(name)
        if cur:
            values = {o.get("value") for o in options}
            if cur in values:
                d["initial_option"] = cur
    return d


def _submit_style_button(text: str, btn_type: str, value: Dict[str, Any]) -> dict:
    """
    卡片里所有按钮统一 form_action_type="submit"，
    这样每次点击都能把 form_value 一起回传，我们通过 value.action 分派逻辑。

    ⚠️ Card JSON 2.0 关键点：按钮的回调数据必须放在 behaviors[].value 里
    （type="callback"），而不是 Card 1.0 的顶层 "value" 字段。
    否则 callback 的 event.action.value 会是空 dict，导致 action 读取为 None。
    """
    return {
        "tag": "button",
        "text": _plain(text),
        "type": btn_type,          # "primary" | "default" | "danger"
        "width": "fill",
        "form_action_type": "submit",
        "name": f"btn_{value.get('action', 'x')}_{value.get('idx', '')}",
        "behaviors": [{"type": "callback", "value": value}],
    }


def _column(elements: List[dict], weight: int = 1, vertical_align: str = "top") -> dict:
    return {
        "tag": "column",
        "width": "weighted",
        "weight": weight,
        "vertical_align": vertical_align,
        "elements": elements,
    }


def _column_set(columns: List[dict], background_style: str = "default", horizontal_spacing: str = "12px", margin: str = "0px") -> dict:
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": background_style,
        "horizontal_spacing": horizontal_spacing,
        "margin": margin,
        "columns": columns,
    }


# ============================== 填报卡片（动态） ==============================
def build_fill_card(
    open_id: str,
    session_id: str,
    n_slots: int = INITIAL_SLOTS,
    prefill: Optional[Dict[str, Any]] = None,
) -> dict:
    """
    动态填报卡片：初始 1 条记录，用户点「＋添加一条记录」/「×删除」后重建卡片，
    用 input.default_value / select.initial_option 回填已经填写的内容。
    """
    prefill = prefill or {}
    n_slots = max(1, min(MAX_RECORDS, int(n_slots)))
    work_type_options = [_select_option(v, label) for v, label in WORK_TYPE_OPTIONS]
    submitter_options = [_select_option(n, n) for n in SUBMITTER_OPTIONS]

    # 顶部：填报人（唯一放在记录外的字段）
    top_field = _column(
        [
            _label_md("👤 填报人"),
            _select_static("submitter", "请选择填报人", submitter_options, prefill=prefill, required=True),
        ]
    )
    top_row = _column_set([top_field], margin="0px 0px 8px 0px")

    # 每条记录一个灰底 column_set，内部两列两行 + 后续备注单行
    record_groups: List[dict] = []
    for i in range(n_slots):
        head_left = _column([
            {
                "tag": "markdown",
                "content": f"**📋 记录 {i + 1}**  <font color=\"grey\">可编辑</font>",
            },
        ], weight=3)
        # 删除按钮（>1 条时才显示）
        head_right_elems: List[dict] = []
        if n_slots > 1:
            head_right_elems.append(
                _submit_style_button(
                    "× 删除",
                    "default",
                    {
                        "action": "remove_record",
                        "idx": i,
                        "n_slots": n_slots,
                        "user": open_id,
                        "session_id": session_id,
                    },
                )
            )
        head_right = _column(head_right_elems, weight=1, vertical_align="center")
        record_header = _column_set([head_left, head_right])

        row_types = _column_set([
            _column([
                _label_md("🏷️ 工作类型"),
                _select_static(
                    f"work_type_{i}",
                    "请选择工作类型",
                    work_type_options,
                    prefill=prefill,
                    required=True,
                ),
            ]),
            _column([
                _label_md("📊 完成量级/是否完成"),
                _input(f"progress_{i}", "如 完成 30% / 已完成", prefill=prefill),
            ]),
        ])

        row_events = _column_set([
            _column([
                _label_md("⏰ 时间段"),
                _input(f"time_range_{i}", "如 09:30-10:20", prefill=prefill),
            ]),
            _column([
                _label_md("📌 事件"),
                _input(f"event_{i}", "本时段做了什么", prefill=prefill),
            ]),
        ])

        row_followup = _column_set([
            _column([
                _label_md("📝 后续跟进备注"),
                _input(f"followup_{i}", "如无请留空", prefill=prefill),
            ]),
        ])

        group_inner = _column([record_header, row_types, row_events, row_followup])
        record_groups.append(
            _column_set(
                [group_inner],
                background_style="grey",
                margin="10px 0px 10px 0px",
            )
        )

    # 底部动作区：＋添加（未达上限时展示） + 提交（始终展示）
    footer_cols: List[dict] = []
    if n_slots < MAX_RECORDS:
        footer_cols.append(
            _column([
                _submit_style_button(
                    "＋ 添加一条记录",
                    "default",
                    {
                        "action": "add_record",
                        "n_slots": n_slots,
                        "user": open_id,
                        "session_id": session_id,
                    },
                )
            ], vertical_align="center")
        )
    footer_cols.append(
        _column([
            _submit_style_button(
                "✅ 提交填报",
                "primary",
                {
                    "action": "form_submit",
                    "n_slots": n_slots,
                    "user": open_id,
                    "session_id": session_id,
                },
            )
        ], vertical_align="center")
    )
    footer_row = _column_set(footer_cols)

    form_elements: List[dict] = [
        top_row,
        {"tag": "hr"},
        *record_groups,
        {"tag": "hr"},
        footer_row,
    ]

    # 卡片头部 banner
    banner = _column_set(
        [
            _column(
                [
                    {"tag": "markdown", "content": "<font color=\"white\">**📝 工作日报填报**</font>"},
                    {
                        "tag": "markdown",
                        "content": (
                            f"<font color=\"grey\">当前 {n_slots}/{MAX_RECORDS} 条记录。"
                            f"最多 {MAX_RECORDS} 条，提交后推送给 {APPROVER_NAME} 审批。</font>"
                        ),
                    },
                ],
                vertical_align="center",
            )
        ],
        background_style="blue",
        margin="0px 0px 8px 0px",
    )

    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "wide_screen_mode": True,
            "streaming_mode": False,
        },
        "header": {
            "template": "blue",
            "title": _plain("📝 工作日报填报"),
        },
        "body": {
            "direction": "vertical",
            "elements": [
                banner,
                {
                    "tag": "form",
                    "name": "fill_form",
                    "elements": form_elements,
                },
            ],
        },
    }


# ============================== 提交预览 & 审批卡片 ==============================
def _work_type_label(work_type: str) -> str:
    for v, label in WORK_TYPE_OPTIONS:
        if v == work_type:
            return label
    return work_type or "—"


def build_summary_card(approval_key: str) -> dict:
    st = approval_state[approval_key]
    records = st["records"]
    lines = [
        f"**👤 填报人：** {st['submitter']}",
        f"**📅 日期：** {st['date']}",
        f"**📝 共 {len(records)} 条记录**",
        "",
    ]
    for i, r in enumerate(records, 1):
        lines.append(f"**── 记录 {i} ──**")
        lines.append(f"🏷️ 工作类型：{_work_type_label(r.get('work_type', ''))}")
        lines.append(f"⏰ 时间段：{r.get('time_range') or '—'}")
        lines.append(f"📌 事件：{r.get('event') or '—'}")
        lines.append(f"📊 完成量级：{r.get('progress') or '—'}")
        lines.append(f"📝 后续跟进：{r.get('followup') or '—'}")
        lines.append("")

    return {
        "schema": "2.0",
        "config": {"update_multi": True, "wide_screen_mode": True},
        "header": {
            "template": "turquoise",
            "title": _plain("📬 已提交，等待审批"),
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": "\n".join(lines)},
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": f"已推送给 **{APPROVER_NAME}** 审批，审批结果将通过消息通知你。",
                },
            ]
        },
    }


def build_approval_card(approval_key: str) -> dict:
    st = approval_state[approval_key]
    records = st["records"]
    sm: Dict[str, str] = st.get("status_map", {})

    elements: List[dict] = [
        {
            "tag": "markdown",
            "content": (
                f"**👤 填报人：** {st['submitter']}\n"
                f"**📅 日期：** {st['date']}\n"
                f"**📝 共 {len(records)} 条待审批**"
            ),
        },
        {"tag": "hr"},
    ]

    for i, r in enumerate(records):
        status = sm.get(str(i), "pending")
        elements.append(
            {
                "tag": "markdown",
                "content": (
                    f"**记录 {i + 1}**\n"
                    f"🏷️ 工作类型：{_work_type_label(r.get('work_type', ''))}\n"
                    f"⏰ 时间段：{r.get('time_range') or '—'}\n"
                    f"📌 事件：{r.get('event') or '—'}\n"
                    f"📊 完成量级：{r.get('progress') or '—'}\n"
                    f"📝 后续跟进：{r.get('followup') or '—'}"
                ),
            }
        )
        if status == "approved":
            elements.append({"tag": "markdown", "content": "🟢 **已同意**"})
        elif status == "rejected":
            elements.append({"tag": "markdown", "content": "🔴 **已驳回**"})
        else:
            elements.append(
                _column_set([
                    _column([
                        {
                            "tag": "button",
                            "text": _plain("✅ 同意"),
                            "type": "primary",
                            "width": "fill",
                            "behaviors": [{"type": "callback", "value": {"action": "approve", "approval_key": approval_key, "idx": i}}],
                        }
                    ]),
                    _column([
                        {
                            "tag": "button",
                            "text": _plain("❌ 驳回"),
                            "type": "danger",
                            "width": "fill",
                            "behaviors": [{"type": "callback", "value": {"action": "reject", "approval_key": approval_key, "idx": i}}],
                        }
                    ]),
                ])
            )
        elements.append({"tag": "hr"})

    return {
        "schema": "2.0",
        "config": {"update_multi": True, "wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": _plain("🔍 工作填报待审批"),
        },
        "body": {"elements": elements},
    }


# ============================== 表单解析 & 提交 ==============================
def parse_records_from_form(form_value: Dict[str, Any], n_slots: int) -> List[Dict[str, str]]:
    """解析 form_value 中所有已填字段。空记录（全字段皆空）自动忽略。"""
    records: List[Dict[str, str]] = []
    for i in range(n_slots):
        rec = {
            "work_type": str(form_value.get(f"work_type_{i}") or "").strip(),
            "time_range": str(form_value.get(f"time_range_{i}") or "").strip(),
            "event": str(form_value.get(f"event_{i}") or "").strip(),
            "progress": str(form_value.get(f"progress_{i}") or "").strip(),
            "followup": str(form_value.get(f"followup_{i}") or "").strip(),
        }
        if any(rec.values()):
            records.append(rec)
    return records


def _find_max_slot_index(form_value: Dict[str, Any]) -> int:
    """扫描 form_value 里出现过的字段索引，返回 n_slots（1-based 数量）。"""
    max_i = 0
    for k in form_value.keys():
        for prefix in ("work_type_", "time_range_", "event_", "progress_", "followup_"):
            if k.startswith(prefix):
                try:
                    max_i = max(max_i, int(k[len(prefix):]) + 1)
                except ValueError:
                    pass
    return max(1, max_i)


def _shift_form_value_after_remove(form_value: Dict[str, Any], removed_idx: int, n_slots: int) -> Dict[str, Any]:
    """删除某条记录后，把后面记录的 index 往前挪 1 位。"""
    new_fv: Dict[str, Any] = {}
    for k, v in form_value.items():
        matched = False
        for prefix in ("work_type_", "time_range_", "event_", "progress_", "followup_"):
            if k.startswith(prefix):
                try:
                    idx = int(k[len(prefix):])
                except ValueError:
                    continue
                if idx == removed_idx:
                    matched = True
                    break
                if idx > removed_idx:
                    new_fv[f"{prefix}{idx - 1}"] = v
                    matched = True
                    break
                # idx < removed_idx: keep as is
                new_fv[k] = v
                matched = True
                break
        if not matched:
            new_fv[k] = v
    return new_fv


def handle_form_submit(open_id: str, form_value: Dict[str, Any], n_slots: int) -> Dict[str, Any]:
    submitter = str(form_value.get("submitter") or "").strip()
    if not submitter:
        return {"toast": {"type": "error", "content": "请选择填报人"}}

    records = parse_records_from_form(form_value, n_slots)
    if not records:
        return {"toast": {"type": "error", "content": "请至少填写一条完整记录"}}

    # 每条记录必须选择工作类型
    for i, r in enumerate(records, 1):
        if not r["work_type"]:
            return {"toast": {"type": "error", "content": f"记录 {i} 请选择工作类型"}}

    approval_key = uuid.uuid4().hex[:10]
    with _lock:
        approval_state[approval_key] = {
            "records": records,
            "submitter": submitter,
            "submitter_open_id": open_id,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "status_map": {},
        }

    send_card(APPROVER_OPEN_ID, build_approval_card(approval_key))
    log.info(f"新填报 approval_key={approval_key} submitter={submitter} records={len(records)}")

    return {
        "toast": {"type": "success", "content": f"✅ 已提交 {len(records)} 条记录"},
        "card": {"type": "raw", "data": build_summary_card(approval_key)},
    }


# ============================== 事件回调 ==============================
def on_message_received(data: P2ImMessageReceiveV1) -> None:
    print("📨 [DEBUG] on_message_received called", flush=True)
    try:
        msg = data.event.message
        chat_type = msg.chat_type
        sender_id = data.event.sender.sender_id
        open_id = sender_id.open_id
        log.info(f"收到消息 | chat_type={chat_type} | msg_type={msg.message_type} | open_id={open_id}")

        if msg.message_type != "text":
            if chat_type == "p2p":
                _send_fill_card(open_id, greeting="收到啦～直接填这张卡片就行 👇")
            return

        try:
            content = json.loads(msg.content).get("text", "").strip()
        except Exception:
            content = ""
        content = _strip_mentions(content)

        if chat_type == "p2p":
            _send_fill_card(open_id)
        elif chat_type == "group":
            if msg.mentions:
                send_text(open_id, "我已经私聊你发填报卡片啦，去私聊里填哦～")
                _send_fill_card(open_id)
    except Exception as e:
        print(f"❌ [ERROR] on_message_received: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        log.exception("on_message_received 出错")


def _send_fill_card(open_id: str, greeting: Optional[str] = None) -> None:
    if greeting:
        send_text(open_id, greeting)
    session_id = uuid.uuid4().hex[:8]
    send_card(open_id, build_fill_card(open_id, session_id, n_slots=INITIAL_SLOTS))


def _strip_mentions(text: str) -> str:
    return re.sub(r"@_user_\d+|@_all", "", text).strip()


def on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    print("🎴 [DEBUG] on_card_action called", flush=True)
    try:
        action = data.event.action

        # ── 多路径读取 value / form_value / name ──────────────────────────
        # 飞书长连接 SDK 有时会把 behaviors[].value 放到 action.value，
        # 但也可能只在原始 body dict 里。优先用 SDK 属性，回退到 __dict__，
        # 最终回退到 data 原始 body（如果 SDK 暴露了的话）。
        def _safe_dict(obj, attr: str) -> dict:
            v = getattr(obj, attr, None)
            if isinstance(v, dict):
                return v
            if v is not None and hasattr(v, "__dict__"):
                return v.__dict__
            return {}

        value: dict = _safe_dict(action, "value")
        form_value: dict = _safe_dict(action, "form_value")
        btn_name: str = getattr(action, "name", None) or ""

        # 尝试从 data 原始 body 补充（lark_oapi SDK 通常把原始 body 放在 data.body 或 data._body）
        raw_body: dict = {}
        for _attr in ("body", "_body", "raw_body"):
            _b = getattr(data, _attr, None)
            if isinstance(_b, dict):
                raw_body = _b
                break
            if isinstance(_b, (bytes, str)):
                try:
                    raw_body = json.loads(_b)
                except Exception:
                    pass
                break

        if raw_body:
            _ev = raw_body.get("event", {})
            _act = _ev.get("action", {})
            print(f"🎴 [DEBUG] raw_body.event.action = {json.dumps(_act, ensure_ascii=False, default=str)}", flush=True)
            if not value:
                value = _act.get("value") or {}
            if not form_value:
                form_value = _act.get("form_value") or {}
            if not btn_name:
                btn_name = _act.get("name", "")

        print(f"🎴 [DEBUG] resolved: value={json.dumps(value, ensure_ascii=False, default=str)} "
              f"form_keys={sorted(form_value.keys())} btn_name={btn_name!r}", flush=True)

        act = value.get("action") if isinstance(value, dict) else None

        # 兜底：从按钮 name（btn_<action>_<idx>）解析 action
        if not act and btn_name:
            m = re.match(r"btn_(add_record|remove_record|form_submit|approve|reject)_", btn_name)
            if m:
                act = m.group(1)
                log.warning(f"action.value 为空，已从 button name 兜底解析 action={act}")

        log.info(f"card.action: {act} value_keys={list(value.keys()) if isinstance(value, dict) else []} form_keys={sorted(form_value.keys())}")

        # ---- 动态：添加一条记录 ----
        if act == "add_record":
            open_id = value.get("user") or data.event.operator.open_id
            session_id = value.get("session_id") or uuid.uuid4().hex[:8]
            cur = int(value.get("n_slots") or _find_max_slot_index(form_value))
            new_n = min(MAX_RECORDS, cur + 1)
            if new_n == cur:
                return P2CardActionTriggerResponse({"toast": {"type": "warning", "content": f"最多 {MAX_RECORDS} 条"}})
            new_card = build_fill_card(open_id, session_id, n_slots=new_n, prefill=form_value)
            return P2CardActionTriggerResponse({
                "toast": {"type": "success", "content": f"已添加记录 {new_n}"},
                "card": {"type": "raw", "data": new_card},
            })

        # ---- 动态：删除某条记录 ----
        if act == "remove_record":
            open_id = value.get("user") or data.event.operator.open_id
            session_id = value.get("session_id") or uuid.uuid4().hex[:8]
            cur = int(value.get("n_slots") or _find_max_slot_index(form_value))
            idx = int(value.get("idx", -1))
            if cur <= 1:
                return P2CardActionTriggerResponse({"toast": {"type": "warning", "content": "至少保留 1 条"}})
            shifted = _shift_form_value_after_remove(form_value, idx, cur)
            new_card = build_fill_card(open_id, session_id, n_slots=cur - 1, prefill=shifted)
            return P2CardActionTriggerResponse({
                "toast": {"type": "success", "content": f"已删除记录 {idx + 1}"},
                "card": {"type": "raw", "data": new_card},
            })

        # ---- 提交 ----
        if act == "form_submit":
            open_id = value.get("user") or data.event.operator.open_id
            n_slots = int(value.get("n_slots") or _find_max_slot_index(form_value))
            resp_data = handle_form_submit(open_id, form_value, n_slots)
            return P2CardActionTriggerResponse(resp_data)

        # ---- 审批：同意 / 驳回 ----
        if act in ("approve", "reject"):
            approval_key = value.get("approval_key")
            idx = int(value.get("idx", -1))
            with _lock:
                st = approval_state.get(approval_key)
                if not st:
                    return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "审批已过期"}})
                sm = st.setdefault("status_map", {})
                if str(idx) in sm:
                    return P2CardActionTriggerResponse({"toast": {"type": "warning", "content": "已处理"}})
                new_status = "approved" if act == "approve" else "rejected"
                sm[str(idx)] = new_status
                rec = st["records"][idx]
                submitter = st["submitter"]
                submitter_open_id = st["submitter_open_id"]
                snapshot = build_approval_card(approval_key)

            write_to_bitable(
                rec,
                _work_type_label(rec.get("work_type", "")),
                submitter,
                "已同意" if new_status == "approved" else "已驳回",
            )
            label = "✅ 同意" if new_status == "approved" else "❌ 驳回"
            send_text(
                submitter_open_id,
                f"{label}｜{APPROVER_NAME} 已处理你的一条填报：\n"
                f"• 工作类型：{_work_type_label(rec.get('work_type', ''))}\n"
                f"• 时间段：{rec.get('time_range') or '—'}\n"
                f"• 事件：{rec.get('event') or '—'}\n"
                f"• 完成量级：{rec.get('progress') or '—'}\n"
                f"• 后续跟进：{rec.get('followup') or '—'}",
            )
            return P2CardActionTriggerResponse(
                {
                    "toast": {
                        "type": "success" if new_status == "approved" else "warning",
                        "content": "已同意 ✅" if new_status == "approved" else "已驳回 ❌",
                    },
                    "card": {"type": "raw", "data": snapshot},
                }
            )

        return P2CardActionTriggerResponse({"toast": {"type": "warning", "content": f"未知操作: {act}"}})
    except Exception as e:
        print(f"❌ [ERROR] on_card_action: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        log.exception("on_card_action 出错")
        return P2CardActionTriggerResponse({"toast": {"type": "error", "content": f"内部错误: {e}"}})


# ============================== 启动 ==============================
def main() -> None:
    print("=" * 60, flush=True)
    print("🤖 [BOOT] 飞书填报机器人（动态添加记录版 · Card 2.0）启动中…", flush=True)
    print(f"[BOOT] APP_ID={APP_ID}", flush=True)
    print(f"[BOOT] APPROVER_OPEN_ID={APPROVER_OPEN_ID}", flush=True)
    print(f"[BOOT] BITABLE={BITABLE_APP_TOKEN}/{BITABLE_TABLE_ID}", flush=True)
    print(f"[BOOT] MAX_RECORDS={MAX_RECORDS}", flush=True)
    print("=" * 60, flush=True)
    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message_received)
        .register_p2_card_action_trigger(on_card_action)
        .build()
    )
    ws = lark.ws.Client(APP_ID, APP_SECRET, event_handler=handler, log_level=lark.LogLevel.INFO)
    print("🔌 WebSocket 长连接启动中…", flush=True)
    ws.start()


if __name__ == "__main__":
    main()
