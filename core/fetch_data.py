import os
import sys
import time
import logging
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv

# 프로젝트 루트 경로 계산
CORE_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(CORE_DIR)

def update_ticker_data(ticker, days=300, market="US"):
    """특정 티커의 데이터를 Toss API로 수집하여 CSV로 저장하는 헬퍼 함수"""
    try:
        from core.brokers.toss import TossBroker
        broker = TossBroker(market=market)
        
        logging.info(f"[{ticker}] Toss API를 사용하여 과거 일봉 데이터 수집 시작 (요청 일수: {days})...")
        
        all_rows = []
        before_cursor = None
        remaining = days
        
        while remaining > 0:
            count_to_fetch = min(200, remaining)
            params = {
                "symbol": ticker,
                "interval": "1d",
                "count": count_to_fetch,
                "adjusted": True
            }
            if before_cursor:
                params["before"] = before_cursor
                
            # Toss API 캔들 조회 호출
            data = broker._call_api("GET", "/api/v1/candles", params=params)
            res_obj = data.get("result", {})
            candles = res_obj.get("candles", [])
            next_before = res_obj.get("nextBefore")
            
            if not candles:
                break
                
            for c in candles:
                ts = c.get("timestamp", "")
                date_str = ts.split("T")[0].replace("-", "") if ts else ""
                
                all_rows.append({
                    "Date": date_str,
                    "Open": float(c.get("openPrice", 0.0)),
                    "High": float(c.get("highPrice", 0.0)),
                    "Low": float(c.get("lowPrice", 0.0)),
                    "Close": float(c.get("closePrice", 0.0)),
                    "% change": "0.0%", 
                    "Volume": int(float(c.get("volume", 0.0)))
                })
                
            remaining -= len(candles)
            before_cursor = next_before
            
            if not before_cursor:
                break
                
            time.sleep(0.1) # Rate Limit 방지
            
        if not all_rows:
            return False, f"{ticker} 데이터 수집 실패: API 응답이 비어있습니다."
            
        df = pd.DataFrame(all_rows)
        df['Date'] = pd.to_datetime(df['Date'], format='%Y%m%d')
        df = df.sort_values('Date', ascending=False).reset_index(drop=True)
        
        data_dir = os.path.join(BASE_DIR, "data")
        os.makedirs(data_dir, exist_ok=True)
        save_path = os.path.join(data_dir, f"{ticker}.csv")
        df.to_csv(save_path, index=False)
        
        logging.info(f"  > [{ticker}] 저장 완료: {save_path} ({len(df)} rows)")
        return True, f"{ticker} 데이터 {len(df)}건 저장 완료 ({save_path})"
        
    except Exception as e:
        logging.error(f"[Fetch Error] {ticker} 데이터 수집 중 오류: {e}")
        return False, f"{ticker} 데이터 수집 실패: {e}"

def main():
    # 로깅 설정
    log_file_path = os.path.join(CORE_DIR, "logs.txt")
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file_path, mode='a', encoding='utf-8'),
            logging.StreamHandler()
        ]
    )

    logging.info("Toss API 기반 과거 주가 데이터 수집 프로세스 시작")
    
    # 한국, 미국 시장 모두 동기화
    for market in ["US", "KR"]:
        list_file = f"ETF_list_{market.lower()}.txt"
        etf_list_path = os.path.join(BASE_DIR, "env", list_file)
        
        if not os.path.exists(etf_list_path):
            logging.warning(f"{etf_list_path} 파일이 존재하지 않아 {market} 시장 수집을 건너뜁니다.")
            continue
            
        targets = []
        try:
            with open(etf_list_path, 'r', encoding='utf-8') as f:
                for line in f:
                    clean_line = line.split('#')[0].strip()
                    if clean_line:
                        targets.append(clean_line.split()[0].upper())
        except Exception as e:
            logging.error(f"{etf_list_path} 읽기 오류: {e}")
            continue
            
        logging.info(f"[{market} 시장] 대상 종목({len(targets)}개): {targets}")
        logging.info("=" * 60)
        
        for ticker in targets:
            success, msg = update_ticker_data(ticker, days=300, market=market)
            time.sleep(0.5)
            logging.info("-" * 60)

if __name__ == "__main__":
    main()
