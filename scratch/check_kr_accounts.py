import sys
import os
import time
from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

# 강제로 .env.kr 파일의 환경 변수 로드
env_path = os.path.join(PROJECT_ROOT, "env", ".env.kr")
if os.path.exists(env_path):
    load_dotenv(env_path, override=True)
    print(f"Loaded env from: {env_path}")

from core.brokers.toss import TossBroker
from core.database import save_api_token_db

def run():
    acc_no = os.getenv('TOSS_ACCOUNT_NO', '').strip()
    if not acc_no:
        print("TOSS_ACCOUNT_NO is not set.")
        return
        
    print("--- 401 Simulation Test Start ---")
    print("Injecting invalid token into DB with valid expiration time (to force cached token usage)...")
    # 1시간 만료의 가짜 토큰 주입
    now = time.time()
    save_api_token_db(acc_no, "KR", "invalid_mock_token_for_401_test_999", now + 3600, now)
    print("Fake token injected successfully.")

    print("\nInitializing TossBroker (This will try accountSeq initialization)...")
    # 이 과정에서 내부적으로 _init_account_seq()가 기동됨
    broker = TossBroker(market="KR")
    
    print("\n--- Result Summary ---")
    print(f"Final TossBroker.account_seq: {broker.account_seq}")
    if broker.account_seq is not None:
        print("Test Result: SUCCESS! TossBroker successfully bypassed 401 using automatic token renewal.")
    else:
        print("Test Result: FAILED! TossBroker failed to handle 401 properly.")

if __name__ == "__main__":
    run()
