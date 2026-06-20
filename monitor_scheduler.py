#!/usr/bin/env python3
import sqlite3
import os
import requests
from datetime import datetime
from dotenv import load_dotenv

# 실행 파일 위치 기준으로 경로 설정 (Docker/OCI 유연성 확보)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data/cavr.db")
ENV_PATH = os.path.join(BASE_DIR, "env/.env")

def send_telegram(token, chat_id, message):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram 전송 실패: {e}")

def check_health():
    if not os.path.exists(ENV_PATH):
        print(".env 파일을 찾을 수 없습니다.")
        return

    load_dotenv(ENV_PATH)
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
    tg_chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not os.path.exists(DB_PATH):
        print("DB 파일이 없습니다.")
        return

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM system_config WHERE key='scheduler_heartbeat'")
        row = cursor.fetchone()
        conn.close()

        if row:
            last_hb_str = row[0]
            last_hb = datetime.strptime(last_hb_str, "%Y-%m-%d %H:%M:%S")
            diff = (datetime.now() - last_hb).total_seconds() / 60

            if diff > 5:
                msg = f"🚨 <b>[cavr 경고]</b>\n스케줄러 심박 중단 감지!\n마지막 신호: {last_hb_str}\n({int(diff)}분 경과)"
                print(msg)
                send_telegram(tg_token, tg_chat_id, msg)
            else:
                print(f"정상 작동 중 (마지막 심박: {int(diff)}분 전)")
        else:
            print("심박 기록을 찾을 수 없습니다.")

    except Exception as e:
        print(f"체크 중 오류 발생: {e}")
        send_telegram(tg_token, tg_chat_id, f"⚠️ <b>[cavr 모니터링 오류]</b>\nHealth Check 스크립트 실행 실패:\n{str(e)}")

if __name__ == "__main__":
    check_health()