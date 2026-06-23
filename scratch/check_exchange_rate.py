import sys
import os
from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

# 강제로 .env.us 파일의 환경 변수 로드
env_path = os.path.join(PROJECT_ROOT, "env", ".env.us")
if os.path.exists(env_path):
    load_dotenv(env_path, override=True)
    print(f"Loaded env from: {env_path}")

from core.brokers.toss import TossBroker
from core.database import delete_api_token_db

def run():
    print("Forcing token deletion in DB to get fresh token...")
    acc_no = os.getenv('TOSS_ACCOUNT_NO', '').strip()
    if acc_no:
        delete_api_token_db(acc_no)
        print("Deleted cached token.")

    print("Fetching exchange rate from TOSS API...")
    broker = TossBroker(market="US")
    
    # 디버깅용 환경 변수 일부 출력
    print(f"TOSS_BASE_URL: {os.getenv('TOSS_BASE_URL')}")
    print(f"TOSS_CLIENT_ID (first 5 chars): {os.getenv('TOSS_CLIENT_ID', '')[:5]}")
    print(f"TOSS_ACCOUNT_NO: {acc_no}")
    
    try:
        # 1단계: 직접 토큰 신규 발급 테스트
        token = broker.get_access_token()
        print(f"Fresh Token generated: {token[:10]}...")
        
        # 2단계: API 호출
        data = broker._call_api("GET", "/api/v1/exchange-rate", params={
            "baseCurrency": "USD",
            "quoteCurrency": "KRW"
        })
        
        print("\n--- Raw Response Data ---")
        import json
        print(json.dumps(data, indent=2, ensure_ascii=False))
        
        res_obj = data.get("result", {})
        rate_val = res_obj.get("rate")
        print(f"\nrate value type: {type(rate_val)}")
        print(f"rate value: {rate_val}")
    except Exception as e:
        print(f"Error occurred: {e}")

if __name__ == "__main__":
    run()
