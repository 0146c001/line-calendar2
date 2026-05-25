import os
from flask import Flask, request, abort
from dotenv import load_dotenv
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from apscheduler.schedulers.background import BackgroundScheduler

import db_manager
import email_manager
import google_calendar
from gemini_parser import parse_event_message

# 載入環境變數
load_dotenv()

app = Flask(__name__)

# ================= 從環境變數或預設值讀取憑證 =================
CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN', 'lmagqMKGkhEbJqL7sZSrhqF5kWyophFwwWoJKsmvWx3UwfIry3hiqJU2RU4J8YSL1oyx6dVS288efjvePsuBPnMvetNSa+AriQaFOjMK8s6g2+ua0aBWymZpsRjd6vBnx6PX5RssYjvzUov/ufuO0QdB04t89/1O/w1cDnyilFU=')
CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET', '1b0aac2458a142982ae8240394e307e2')
# =========================================================

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# 初始化資料庫
db_manager.init_db()

def reply_to_user(reply_token, text):
    """回覆 LINE 訊息的輔助函式"""
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text=text)]
                )
            )
    except Exception as e:
        print(f"[錯誤] 回覆 LINE 訊息失敗: {e}")

def send_line_push_message(user_id, message_text):
    """發送 LINE 主動推送訊息的輔助函式"""
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=[TextMessage(text=message_text)]
                )
            )
        print(f"[成功] LINE 推送訊息成功發送給: {user_id}")
        return True
    except Exception as e:
        print(f"[錯誤] LINE 推送訊息失敗: {e}")
        return False

# ================= 排程工作：檢查並發送行程提醒 =================
def check_reminders_job():
    """定期檢查 1.5 小時後即將開始的行程，發送 LINE 及 Gmail 提醒"""
    try:
        # 預設查詢未來 90 分鐘（1.5 小時）內開始的行程
        events = db_manager.get_upcoming_unreminded_events(minutes_limit=90)
        for event in events:
            event_id = event['id']
            # 嘗試取得排程鎖定，防範多行程或重複執行
            if db_manager.acquire_reminder_lock(event_id):
                print(f"[排程] 開始發送行程提醒: {event['summary']}")
                
                # 1. 寄送 Gmail 提醒
                subject = f"【行程開始提醒】您的行程『{event['summary']}』即將開始"
                body = f"""您好：
                
您記錄的行程『{event['summary']}』將於一個半小時後開始！

● 行程名稱：{event['summary']}
● 日期時間：{event['event_date']} {event['event_time']}
● 地點：{event['location'] or '未提供'}

請提早準備，祝您順心！
"""
                email_manager.send_gmail(subject, body)
                
                # 2. 發送 LINE 推送訊息
                line_message = f"⏰ 行程提醒：\n您的行程『{event['summary']}』即將在一個半小時後開始！\n\n● 行程：{event['summary']}\n● 時間：{event['event_date']} {event['event_time']}\n● 地點：{event['location'] or '未提供'}"
                send_line_push_message(event['user_id'], line_message)
    except Exception as e:
        print(f"[排程錯誤] 提醒排程執行失敗: {e}")

# 啟動背景排程 (每 60 秒執行一次)
scheduler = BackgroundScheduler()
scheduler.add_job(func=check_reminders_job, trigger="interval", seconds=60)
scheduler.start()

# 核心路由：確認 Webhook 能接收 LINE 平台連線
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("憑證驗證失敗，請檢查 Token 或 Secret 是否填錯！")
        abort(400)

    return 'OK'

# 當收到 LINE 文字訊息時觸發的邏輯
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text.strip()
    print(f"\n[收到使用者訊息] 來自: {user_id}，內容: {user_message}")

    # 1. 讀取此使用者的對話暫存 session
    session = db_manager.get_session(user_id)

    # 2. 使用 Gemini 解析並提取行程資訊
    parsed = parse_event_message(user_message, session)
    if not parsed:
        reply_to_user(event.reply_token, "系統忙碌中，請稍後再試。")
        return

    # 3. 處理「取消行程記錄」特殊指令
    is_event_creation = parsed.get('is_event_creation', False)
    date_val = parsed.get('date')
    time_val = parsed.get('time')
    location_val = parsed.get('location')
    description_val = parsed.get('description')
    missing_fields = parsed.get('missing_fields', [])
    reply_text = parsed.get('reply_text', '')

    if is_event_creation and not any([date_val, time_val, location_val, description_val]) and not missing_fields:
        db_manager.clear_session(user_id)
        reply_to_user(event.reply_token, reply_text or "已幫您取消當前行程記錄。")
        return

    # 4. 處理「行程新增/補充」流程
    if is_event_creation:
        # A. 尚有欄位缺失：儲存當前進度，並向使用者提問補充
        if missing_fields:
            pending_event = {
                "date": date_val,
                "time": time_val,
                "location": location_val,
                "description": description_val
            }
            db_manager.save_session(user_id, "waiting_for_info", pending_event)
            reply_to_user(event.reply_token, reply_text)
        
        # B. 資訊全部補齊：進行記錄、發信、加入行事曆、清理 Session
        else:
            db_manager.clear_session(user_id)
            
            # 本地 ISO 格式之開始時間 YYYY-MM-DD HH:MM:00
            start_time_dt = f"{date_val} {time_val}:00"
            
            # 1. 寄送 Gmail 確認郵件
            subject = f"【行程記錄通知】已成功為您記錄行程：{description_val}"
            body = f"""您好：
            
已成功為您記錄以下行程：

● 行程名稱：{description_val}
● 日期：{date_val}
● 時間：{time_val}
● 地點：{location_val or '未提供'}

我們將會在行程開始前一個半小時，以 LINE 與 Gmail 提醒您。
"""
            email_sent = email_manager.send_gmail(subject, body)
            
            # 2. 新增行程至 Google 行事曆
            google_event_id = google_calendar.add_event_to_calendar(
                summary=description_val,
                location=location_val,
                date_str=date_val,
                time_str=time_val,
                description="透過 LINE 機器人自動新增的行程"
            )
            
            # 3. 寫入本地資料庫以便排程提醒
            db_manager.add_event(
                user_id=user_id,
                summary=description_val,
                event_date=date_val,
                event_time=time_val,
                location=location_val,
                start_time_dt=start_time_dt,
                google_event_id=google_event_id
            )

            # 4. 回覆 LINE 處理狀態
            status_notes = []
            if not email_sent:
                status_notes.append("Gmail 寄送失敗（請至 .env 設定 Gmail 應用程式密碼）")
            if not google_event_id:
                status_notes.append("Google 行事曆同步失敗（請放置 service_account.json 或 token.json 憑證）")
                
            if status_notes:
                reply_text += "\n\n⚠️ 提醒：\n" + "\n".join([f"- {note}" for note in status_notes])
            else:
                reply_text += "\n\n（已成功寄送 Gmail 通知並加入 Google 行事曆！）"
                
            reply_to_user(event.reply_token, reply_text)

    # 5. 一般日常聊天對話
    else:
        reply_to_user(event.reply_token, reply_text or "您好！我是您的行程助理，隨時可以告訴我您想安排的行程喔！")

if __name__ == "__main__":
    # 本地測試執行在 Port 5000
    app.run(host='0.0.0.0', port=5000)