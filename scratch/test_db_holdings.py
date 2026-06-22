import sys
import os

# 프로젝트 루트 경로 추가
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

from core.database import get_connection, get_holdings_from_db, log_trade_db, init_db

def run_test():
    print("DB Holdings Reconstruction Test Start...")
    
    # DB 초기화 (테이블 생성)
    init_db()
    
    # 1. 테스트용 임시 변수 정의
    symbol = "TEST_TICKER"
    market = "US"
    strategy_name = "TEST_STRATEGY"
    
    # DB 커넥션 획득 및 기존 테스트 데이터 삭제
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM trade_history WHERE strategy_name=?", (strategy_name,))
    conn.commit()
    conn.close()
    
    try:
        # 2. 첫 번째 매수 로그 삽입: 10.5주 (int casting으로 10주로 변경됨) @ $10.0
        # total_amount는 $100.0, fee $0.09
        log_trade_db(
            date="2026-06-22 10:00:00",
            symbol=symbol,
            strategy="CA",
            side="BUY",
            price=10.0,
            qty=10.5,
            fee=0.09,
            total_amount=100.0,
            turn=1.0,
            note="Test Buy 1",
            odno="TX001",
            market=market,
            strategy_name=strategy_name
        )
        
        # 3. 두 번째 매수 로그 삽입: 5.3주 (int casting으로 5주로 변경됨) @ $12.0
        # total_amount는 $60.0, fee $0.05
        log_trade_db(
            date="2026-06-22 10:05:00",
            symbol=symbol,
            strategy="CA",
            side="BUY",
            price=12.0,
            qty=5.3,
            fee=0.05,
            total_amount=60.0,
            turn=2.0,
            note="Test Buy 2",
            odno="TX002",
            market=market,
            strategy_name=strategy_name
        )
        
        # 4. DB holdings 계산 확인 (10 + 5 = 15주)
        # cost = 100.0 + 60.0 = 160.0
        # avg_price = 160.0 / 15 = 10.6666...
        shares, avg_price, eval_amt = get_holdings_from_db(symbol, market, strategy_name)
        print(f"Intermediate holdings calculation: shares={shares}, avg_price={avg_price:.4f}, eval_amt={eval_amt:.4f}")
        assert int(shares) == 15, f"Expected 15 shares, got {shares}"
        assert abs(avg_price - (160.0 / 15)) < 1e-5, f"Expected avg_price ~ 10.6667, got {avg_price}"
        
        # 5. 매도 로그 삽입: 3.5주 (int casting으로 3주로 변경됨) @ $15.0
        # total_amount는 $45.0, fee $0.04
        log_trade_db(
            date="2026-06-22 10:10:00",
            symbol=symbol,
            strategy="CA",
            side="SELL",
            price=15.0,
            qty=3.5,
            fee=0.04,
            total_amount=45.0,
            turn=3.0,
            note="Test Sell 1",
            odno="TX003",
            market=market,
            strategy_name=strategy_name
        )
        
        # 6. 매도 후 holdings 계산 확인
        # sold_qty = 3
        # cost_of_sold = (160.0/15) * 3 = 32.0
        # remaining cost = 160.0 - 32.0 = 128.0
        # remaining shares = 15 - 3 = 12주
        # avg_price = 128.0 / 12 = 10.6666...
        shares, avg_price, eval_amt = get_holdings_from_db(symbol, market, strategy_name)
        print(f"Final holdings calculation: shares={shares}, avg_price={avg_price:.4f}, eval_amt={eval_amt:.4f}")
        assert int(shares) == 12, f"Expected 12 shares, got {shares}"
        assert abs(avg_price - (128.0 / 12)) < 1e-5, f"Expected avg_price ~ 10.6667, got {avg_price}"
        
        print("DB Holdings Reconstruction Test Success!")
        
    except AssertionError as e:
        print(f"Test Failed (AssertionError): {e}")
    except Exception as e:
        print(f"Test Failed (Exception): {e}")
    finally:
        # 클린업
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM trade_history WHERE strategy_name=?", (strategy_name,))
        conn.commit()
        conn.close()
        print("Cleaned up mock trade history data.")

if __name__ == "__main__":
    run_test()
