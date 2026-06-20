import os
import json
import requests
import logging
from datetime import datetime
from pytz import timezone
from dotenv import load_dotenv

# 1. 환경 설정 및 경로 파악
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
load_dotenv(os.path.join(PROJECT_ROOT, "env", ".env"))

# KIS API 설정
APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")
ACCOUNT_NO = os.getenv("KIS_ACCOUNT_NO")
BASE_URL = os.getenv("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443")

# 계좌번호 분리 (8자리-2자리)
CANO = ACCOUNT_NO.split("-")[0]
ACNT_PRDT_CD = ACCOUNT_NO.split("-")[1]

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("NASD_Order_Test")

def get_access_token():
    """Access Token 발급"""
    url = f"{BASE_URL}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    body = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET
    }
    res = requests.post(url, headers=headers, data=json.dumps(body))
    if res.status_code == 200:
        return res.json()["access_token"]
    else:
        logger.error(f"토큰 발급 실패: {res.text}")
        return None

def get_current_price(token, symbol):
    """현재가 조회 (NASD 종목은 3자리 'NAS' 사용)"""
    url = f"{BASE_URL}/uapi/overseas-price/v1/quotations/price"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "HHDFS00000300"
    }
    params = {
        "AUTH": "",
        "EXCD": "NAS",  # 시세 조회용 코드
        "SYMB": symbol
    }
    res = requests.get(url, headers=headers, params=params)
    if res.status_code == 200:
        data = res.json()
        if data["rt_cd"] == "0":
            return float(data["output"]["last"])
    logger.error(f"시세 조회 실패: {res.text}")
    return None

def place_limit_order(token, symbol, price, qty):
    """지정가 매수 주문 (미국주식은 4자리 'NASD' 사용)"""
    url = f"{BASE_URL}/uapi/overseas-stock/v1/trading/order"
    # 실전투자 미국 매수 TR_ID: TTTT1002U (기존 TTTS에서 변경)
    tr_id = "TTTT1002U" 
    
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id
    }
    
    payload = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "OVRS_EXCG_CD": "NASD",  # 주문용 코드
        "PDNO": symbol,
        "ORD_QTY": str(int(qty)),
        "OVRS_ORD_UNPR": f"{price:.2f}",
        "CTAC_TLNO": "",         # 선택 파라미터 (공백 허용)
        "MGCO_APTM_ODNO": "",    # 운용사지정주문번호
        "ORD_SVR_DVSN_CD": "0",  # 필수 파라미터
        "ORD_DVSN": "00"         # 00: 지정가
    }
    
    logger.info(f"주문 요청 송신: {symbol} {qty}주 @ ${price:.2f}")
    res = requests.post(url, headers=headers, data=json.dumps(payload))
    
    return res.status_code, res.json()

def main():
    # 현재 시간 확인 (NY 시간 기준)
    now_utc = datetime.now(timezone('UTC'))
    ny_time = now_utc.astimezone(timezone('America/New_York'))
    kst_time = now_utc.astimezone(timezone('Asia/Seoul'))

    logger.info(f"현재 뉴욕 시간: {ny_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"현재 한국 시간: {kst_time.strftime('%Y-%m-%d %H:%M:%S')}")

    token = get_access_token()
    if not token: return

    symbol = "TQQQ"
    current_price = get_current_price(token, symbol)
    
    if current_price:
        target_price = current_price - 1.0
        logger.info(f"{symbol} 현재가: ${current_price:.2f} -> 목표가: ${target_price:.2f}")
        
        # 실제 주문 실행 (1주)
        status_code, response = place_limit_order(token, symbol, target_price, 1)
        
        if status_code == 200 and response.get("rt_cd") == "0":
            logger.info("✅ 주문 접수 성공!")
            logger.info(f"주문번호: {response['output']['ODNO']}")
            logger.info(f"메시지: {response['msg1']}")
        else:
            logger.error("❌ 주문 접수 실패")
            logger.error(f"에러 코드: {response.get('msg_cd')}")
            logger.error(f"에러 메시지: {response.get('msg1')}")
            
            if response.get("msg_cd") == "APBK0918":
                logger.warning("💡 APBK0918 발생: 거래소 코드(NASD)와 ORD_SVR_DVSN_CD를 다시 확인하세요.")
    else:
        logger.error("현재가를 가져올 수 없어 주문을 중단합니다.")

if __name__ == "__main__":
    main()