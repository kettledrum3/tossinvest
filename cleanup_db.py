import os
import sys

# 현재 파일이 위치한 디렉토리를 최우선 탐색 경로에 추가하여 'core' 패키지를 인식하게 함
PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.database import delete_strategy_db, init_db, get_connection

def remove_received_tag_from_dates():
    """
    trade_history 테이블의 date 컬럼과 order_history 테이블의 timestamp 컬럼에서 ' (수신)' 태그를 제거합니다.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # 1. trade_history 업데이트
    cursor.execute("UPDATE trade_history SET date = REPLACE(date, ' (수신)', '') WHERE date LIKE '% (수신)%'")
    th_count = cursor.rowcount
    
    # 2. order_history 업데이트
    cursor.execute("UPDATE order_history SET timestamp = REPLACE(timestamp, ' (수신)', '') WHERE timestamp LIKE '% (수신)%'")
    oh_count = cursor.rowcount
    
    conn.commit()
    conn.close()
    print(f"✅ [Cleanup] 날짜 태그(' (수신)') 제거 완료 (trade_history: {th_count}건, order_history: {oh_count}건)")

def cleanup_unlinked_orders():
    """
    order_history 테이블에서 strategy_name이 NULL이거나 빈 문자열인 주문을 삭제합니다.
    이는 자동매매 전략과 연결되지 않은 주문 (예: 과거의 버그로 인한 기록, 수동 주문)을 정리합니다.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("DELETE FROM order_history WHERE strategy_name IS NULL OR strategy_name = ''")
    deleted_count = cursor.rowcount
    
    conn.commit()
    conn.close()
    
    if deleted_count > 0:
        print(f"🗑️ [Cleanup] order_history 테이블에서 연결되지 않은 주문 {deleted_count}건을 삭제했습니다.")
    else:
        print("✅ [Cleanup] order_history 테이블에서 정리할 연결되지 않은 주문이 없습니다.")

def migrate_tqqq_alias():
    """
    기존에 별칭 없이 저장된 TQQQ CA US 내역을 'TQQQ_1차' 별칭으로 업데이트합니다. (1회성)
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # 1. trade_history 업데이트 (거래 내역)
    cursor.execute('''
        UPDATE trade_history 
        SET strategy_name = 'TQQQ_1차' 
        WHERE symbol = 'TQQQ' AND strategy = 'CA' AND market = 'US' 
        AND (strategy_name IS NULL OR strategy_name = '')
    ''')
    th_count = cursor.rowcount
    
    # 2. order_history 업데이트 (주문 내역)
    cursor.execute('''
        UPDATE order_history 
        SET strategy_name = 'TQQQ_1차' 
        WHERE symbol = 'TQQQ' AND strategy = 'CA' AND market = 'US' 
        AND (strategy_name IS NULL OR strategy_name = '')
    ''')
    oh_count = cursor.rowcount
    
    conn.commit()
    conn.close()
    print(f"✅ [Migration] TQQQ 내역 별칭 업데이트 완료 (Trade: {th_count}건, Order: {oh_count}건)")

def remove_incorrect_profit_records():
    """
    2026-04-08에 발생한 TQQQ 매도 내역 중 이익이 0으로 잘못 기록된 데이터를 삭제합니다.
    삭제 후 재동기화를 통해 정확한 평단가로 재계산하기 위함입니다.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        DELETE FROM trade_history 
        WHERE symbol = 'TQQQ' AND side = 'SELL' AND date = '2026-04-08' AND realized_profit = 0
    ''')
    count = cursor.rowcount
    conn.commit()
    conn.close()
    print(f"🗑️ [Cleanup] 잘못된 실현손익 레코드 {count}건을 삭제했습니다.")

def fix_kr_duplicate_trades_20260422():
    """
    2026-04-22 KR 시장 웹소켓 파싱 오류로 발생한 중복/오류 데이터를 정리합니다.
    1. 동일 주문번호(odno) 중복 기록 삭제 (주문접수 시 기록된 데이터 제거)
    2. 잘못된 매수/매도 구분 데이터 정리
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # 1. 중복된 주문번호(odno) 확인 및 이전 기록(ID가 작은 것) 삭제
    # 2026-04-22 한국 시장 데이터 대상
    cursor.execute('''
        DELETE FROM trade_history
        WHERE market = 'KR' AND date = '2026-04-22'
        AND id NOT IN (
            SELECT MAX(id)
            FROM trade_history
            WHERE market = 'KR' AND date = '2026-04-22'
            GROUP BY odno
        )
        AND odno IS NOT NULL AND odno != 'N/A'
    ''')
    dup_count = cursor.rowcount
    
    conn.commit()
    conn.close()
    
    if dup_count > 0:
        print(f"🗑️ [Cleanup] KR 중복 체결 데이터 {dup_count}건을 삭제했습니다. (최종 체결가만 보존)")
    else:
        print("✅ [Cleanup] 정리할 KR 중복 데이터가 없습니다.")
    
    print("💡 알림: 데이터 삭제 후 대시보드의 '현재 계좌 상태 조회' 버튼을 눌러 실제 잔고(평단/수량)와 DB를 반드시 동기화해주세요.")

def fix_kr_recording_issue_20260423():
    """
    2026-04-23 KR 시장에서 체결되었으나 기록되지 않은 데이터를 강제 동기화하기 위한 작업을 수행합니다.
    잘못된 'SYNC' 데이터나 별칭이 없는 거래 내역을 삭제하여 재동기화가 가능하게 합니다.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # 1. 오늘 날짜 KR 시장의 불완전한 거래 내역(별칭 없음 또는 SYNC) 삭제
    cursor.execute('''
        DELETE FROM trade_history 
        WHERE market = 'KR' AND date = '2026-04-23' 
        AND (strategy_name IS NULL OR strategy_name = '' OR strategy_name = 'SYNC')
    ''')
    th_count = cursor.rowcount
    
    # 2. 이미 체결되었으나 order_history에 잘못 남아있는 내역 정리
    cursor.execute('''
        DELETE FROM order_history 
        WHERE market = 'KR' AND status = 'FILLED'
    ''')
    oh_count = cursor.rowcount
    
    conn.commit()
    conn.close()
    
    print(f"🗑️ [Cleanup] 2026-04-23 KR 기록 누락 대응 완료 (Trade: {th_count}건, Order: {oh_count}건)")
    print("💡 알림: 이후 대시보드에서 '현재 계좌 상태 조회' 버튼을 누르면 KIS API를 통해 누락된 체결 내역을 다시 가져옵니다.")

def fix_shifted_data_20260416():
    """
    2026-04-16에 발생한 데이터 밀림 현상 수정.
    symbol 컬럼에 '01' 또는 '02'가 잘못 입력된 14건의 데이터를 삭제합니다.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        DELETE FROM trade_history 
        WHERE date = '2026-04-16' AND symbol IN ('01', '02')
    ''')
    count = cursor.rowcount
    conn.commit()
    conn.close()
    
    print(f"🗑️ [Cleanup] 2026-04-16 잘못 기록된(shifted) 데이터 {count}건을 삭제했습니다.")

def standardize_and_cleanup_db():
    """
    데이터베이스 전반의 매수/매도 명칭을 BUY/SELL로 통일하고, 
    주문번호(odno)를 기준으로 중복 기록된 체결 내역을 정리합니다.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # 1. 매수/매도 구분자 통일 (한글 -> 영어)
    cursor.execute("UPDATE trade_history SET side = 'BUY' WHERE side = '매수'")
    cursor.execute("UPDATE trade_history SET side = 'SELL' WHERE side = '매도'")
    
    # 2. 주문번호(odno) 기반 중복 데이터 삭제 (odno, market 기준 가장 먼저 생성된 ID만 보존)
    cursor.execute("""
        DELETE FROM trade_history 
        WHERE id NOT IN (
            SELECT MIN(id) 
            FROM trade_history 
            WHERE odno IS NOT NULL AND odno != '' AND odno != 'N/A'
            GROUP BY odno, market
        )
        AND odno IS NOT NULL AND odno != '' AND odno != 'N/A'
    """)
    
    # 3. 'Realtime WS (None)' 등으로 기록된 note 필드를 새로운 Synced 포맷으로 업데이트
    cursor.execute("""
        UPDATE trade_history 
        SET note = 'Synced (ODNO: ' || odno || ')' 
        WHERE (note LIKE '%Realtime WS%' OR note IS NULL OR note = '') 
        AND odno IS NOT NULL AND odno != '' AND odno != 'N/A'
    """)

    # 4. odno 중복 저장을 방지하기 위한 UNIQUE 인덱스 생성 (Partial Index)
    print("4. odno UNIQUE 인덱스 생성 중...")
    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_history_odno 
        ON trade_history (odno, market) 
        WHERE odno IS NOT NULL AND odno != '' AND odno != 'N/A'
    """)

    conn.commit()
    conn.close()
    print("✅ [Standardize] 명칭 통일, 중복 정리 및 UNIQUE 인덱스 생성이 완료되었습니다.")

def force_cleanup_today_canceled():
    """
    오늘 발생한 CANCELED 상태의 주문들을 삭제합니다.
    삭제 후 대시보드에서 '계좌 상태 조회'를 누르면 KIS 서버의 실제 상태로 재동기화됩니다.
    """
    conn = get_connection()
    cursor = conn.cursor()
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    
    cursor.execute("DELETE FROM order_history WHERE status = 'CANCELED' AND timestamp LIKE ?", (f"{today}%",))
    count = cursor.rowcount
    conn.commit()
    conn.close()
    print(f"🧹 [Cleanup] 오늘자 CANCELED 주문 {count}건을 강제 삭제했습니다.")

def normalize_symbols_in_db():
    """DB 내의 모든 KR 종목 코드에서 'A' 접두사를 제거합니다."""
    conn = get_connection()
    cursor = conn.cursor()
    tables = ["trade_history", "order_history", "strategy_state", "finished_strategy_state"]
    total_count = 0
    
    for table in tables:
        cursor.execute(f"UPDATE {table} SET symbol = LTRIM(symbol, 'A') WHERE symbol LIKE 'A%' AND (market = 'KR' OR market IS NULL)")
        total_count += cursor.rowcount
    
    conn.commit()
    conn.close()
    print(f"✅ [Normalize] {total_count}건의 종목 코드 정규화(A 제거)를 완료했습니다.")

if __name__ == "__main__":
    init_db() # DB가 초기화되었는지 확인

    print("\n🧹 [Cleanup] 날짜 필드에서 '(수신)' 태그 제거 작업을 시작합니다...")
    remove_received_tag_from_dates()

    print("🧹 [Cleanup] DB 표준화 및 정합성 작업을 시작합니다...")
    standardize_and_cleanup_db()

    print("\n🧹 [Cleanup] 종목 코드 정규화 작업을 시작합니다...")
    normalize_symbols_in_db()

    print("\n🧹 [Cleanup] 오늘자 오판된 취소 내역 정리를 시작합니다...")
    force_cleanup_today_canceled()

    print("🧹 [Cleanup] 잘못된 실현손익 데이터 정리를 시작합니다...")
    remove_incorrect_profit_records()
    
    print("\n🧹 [Cleanup] 2026-04-22 KR 시장 데이터 정합성 작업을 시작합니다...")
    fix_kr_duplicate_trades_20260422()

    print("\n🧹 [Cleanup] 2026-04-23 KR 시장 기록 누락 정정 작업을 시작합니다...")
    fix_kr_recording_issue_20260423()

    print("\n🧹 [Cleanup] 2026-04-16 KR 시장 데이터 밀림(shifted) 정정 작업을 시작합니다...")
    fix_shifted_data_20260416()

    print("🧹 [Cleanup] US 시장의 별칭 없는 TQQQ CA 전략을 삭제합니다...")
    # strategy_name="" (빈 문자열)을 타겟으로 삭제 수행
    delete_strategy_db(symbol="TQQQ", strategy="CA", market="US", strategy_name="")
    print("✅ 정리 완료.")
    
    print("\n🧹 [Cleanup] order_history 테이블에서 연결되지 않은 주문을 정리합니다...")
    cleanup_unlinked_orders()
    print("✅ order_history 정리 완료.")