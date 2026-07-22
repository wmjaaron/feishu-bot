#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json, os, sys
import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

APP_ID = os.getenv("FEISHU_APP_ID", "").strip()
APP_SECRET = os.getenv("FEISHU_APP_SECRET", "").strip()
if not APP_ID or not APP_SECRET:
    sys.exit("❌ 缺少环境变量：FEISHU_APP_ID / FEISHU_APP_SECRET")

def on_message(data: P2ImMessageReceiveV1) -> None:
    try:
        raw = lark.JSON.marshal(data, indent=2)
    except Exception:
        raw = json.dumps(data.__dict__, ensure_ascii=False, default=str, indent=2)
    print("📨 收到消息: " + raw, flush=True)

def main() -> None:
    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )
    ws = lark.ws.Client(APP_ID, APP_SECRET, event_handler=handler, log_level=lark.LogLevel.INFO)
    print(f"🔌 正在建立长连接... APP_ID={APP_ID}", flush=True)
    print("✅ 连接成功，等待消息...", flush=True)
    ws.start()

if __name__ == "__main__":
    main()
