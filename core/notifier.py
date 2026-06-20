import os
import requests
import logging
import re
from dotenv import load_dotenv

# 로거 설정
logger = logging.getLogger(__name__)

# 환경변수 로드
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
load_dotenv(os.path.join(PROJECT_ROOT, "env", ".env"))

def send_telegram_message(message: str):
    """
    텔레그램 메시지 전송 함수
    환경변수 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID가 설정되어 있어야 함.
    """
    # [수정] 호출 시점에 환경변수를 다시 읽어오도록 변경 (시장별 .env 교체 로드 반영)
    # 다른 스크립트와의 호환성을 위해 TELEGRAM_TOKEN 변수명도 함께 확인합니다.
    token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        # 설정이 없으면 에러 없이 리턴 (로그는 남길 수 있음)
        return

    # HTML 태그 제거 및 줄바꿈 정리하여 로그 기록
    clean_msg = re.sub('<[^<]+?>', '', message).replace('\n', ' ')
    logger.info(f"[TG_SEND] {clean_msg}")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # HTML 모드로 보내면 굵게/기울임 등을 사용할 수 있음
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }
    
    try:
        response = requests.post(url, json=payload, timeout=5)
        if response.status_code != 200:
            logger.error(f"Telegram API Error: {response.text}")
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")
