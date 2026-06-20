import sqlite3
import json
import os
import logging
import math
import re
from dataclasses import asdict
from datetime import datetime

# 프로젝트 루트 경로 계산
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")
DB_PATH = os.path.join(DATA_DIR, "cavr.db")

logger = logging.getLogger(__name__)
from core.notifier import send_telegram_message

from .utils import format_symbol_display # ADDED: 종목명 표시를 위한 임포트
def get_connection():
    """DB 연결 객체 반환"""
    try:
        # DB 파일 상태 확인 (로깅 레벨을 DEBUG로 조정하여 INFO 로그 폭주 방지)
        if not os.path.exists(DB_PATH):
            logger.debug(f"🔍 DB 파일이 존재하지 않습니다. 새로 생성됩니다: {DB_PATH}")
        else:
            size = os.path.getsize(DB_PATH)
            logger.debug(f"📂 DB 연결 시도: {DB_PATH} (크기: {size} bytes)")
            
        conn = sqlite3.connect(DB_PATH)
        logger.debug("✅ DB 연결 완료")
        return conn
    except sqlite3.Error as e:
        # 연결 실패 시 원인 분석을 위한 상세 로그 기록
        logger.error(f"❌ DB 연결 실패: {e}")
        logger.debug(f"상세 정보 - 경로: {DB_PATH}, 에러: {str(e)}")
        raise e

def rotate_log_file(log_filename, max_size_mb=2):
    """로그 파일 크기가 기준을 넘으면 로테이션(백업 후 초기화) 수행"""
    log_path = os.path.join(LOG_DIR, log_filename)
    if os.path.exists(log_path) and os.path.getsize(log_path) > max_size_mb * 1024 * 1024:
        try:
            backup_path = log_path + ".old"
            if os.path.exists(backup_path):
                os.remove(backup_path)
            os.rename(log_path, backup_path)
            logger.info(f"🔄 로그 로테이션 완료: {log_filename} (2MB 초과)")
        except Exception as e:
            logger.error(f"로그 로테이션 실패 ({log_filename}): {e}")

def init_db(run_maintenance=False):
    """데이터베이스 및 기본 테이블 초기화"""
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)

    if run_maintenance:
        # [추가] 스케줄러 및 주요 로그 파일 크기 체크 및 정리
        rotate_log_file("scheduler.log", max_size_mb=2)
        rotate_log_file("message_log.txt", max_size_mb=2)
        try:
            cleanup_processed_orders_db()
            cleanup_old_canceled_orders_db(days=3)
            logger.info("✅ 유지보수 작업: 중복 주문 및 오래된 취소 내역(order_history) 정리 완료")
        except Exception as e:
            logger.debug(f"유지보수 작업 건너뜀: {e}")

    conn = get_connection()
    cursor = conn.cursor()
    
    # WAL 모드 활성화 (동시성 향상 및 잠금 완화)
    cursor.execute("PRAGMA journal_mode=WAL;")
    
    # [추가] DB 파일 권한 강제 설정 (컨테이너 내 쓰기 권한 확보)
    try:
        if os.path.exists(DB_PATH):
            os.chmod(DB_PATH, 0o666)
    except Exception as e:
        logger.warning(f"DB 권한 설정 실패 (무시 가능): {e}")

    
    # 거래 기록 테이블
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS trade_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            symbol TEXT,
            strategy TEXT,
            side TEXT,
            market TEXT DEFAULT 'US', 
            strategy_name TEXT, -- Added strategy_name
            price REAL,
            qty REAL,
            fee REAL,
            total_amount REAL,
            turn REAL,
            note TEXT,
            odno TEXT,
            avg_price REAL DEFAULT 0.0,
            realized_profit REAL DEFAULT 0.0,
            realized_profit_rate REAL DEFAULT 0.0,
            cum_buy_amt REAL DEFAULT 0.0, -- [ADD] 누적 매수액
            cum_sell_amt REAL DEFAULT 0.0  -- [ADD] 누적 매도액
        )
    ''')

    # 주문 내역(제출 상태) 테이블
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS order_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (DATETIME('now', 'localtime')),
            symbol TEXT,
            strategy TEXT,
            market TEXT DEFAULT 'US',
            side TEXT,
            price REAL,
            qty REAL,
            type TEXT,
            status TEXT,
            odno TEXT,
            msg TEXT,
            strategy_name TEXT -- Added strategy_name
        )
    ''')
    
    # 전략 상태 저장 테이블 (JSON 형태로 상태 저장)
    # Migration for strategy_state PRIMARY KEY change
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='strategy_state'")
    table_exists = cursor.fetchone()
    
    if table_exists:
        cursor.execute("PRAGMA table_info(strategy_state)")
        ss_cols_info = cursor.fetchall()
        # Check if strategy_name is part of the primary key
        pk_cols = [col[1] for col in ss_cols_info if col[5] > 0] # col[5] is pk
        
        if 'strategy_name' not in pk_cols or len(pk_cols) != 4: # If PK is not (symbol, strategy, market, strategy_name)
            logger.info("🛠️ [Migration] strategy_state 테이블의 PRIMARY KEY를 (symbol, strategy, market, strategy_name)으로 변경합니다.")
            # 1. 기존 테이블 이름 변경
            cursor.execute("ALTER TABLE strategy_state RENAME TO strategy_state_old;")
            # 2. 새 테이블 생성 (새로운 PRIMARY KEY 포함)
            cursor.execute('''
                CREATE TABLE strategy_state (
                    symbol TEXT,
                    strategy TEXT,
                    market TEXT DEFAULT 'US',
                    strategy_name TEXT,
                    state_json TEXT,
                    is_active INTEGER DEFAULT 1,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (symbol, strategy, market, strategy_name)
                )
            ''')
            # 3. 기존 데이터 복사 (strategy_name이 NULL인 경우 빈 문자열로 처리)
            cursor.execute("INSERT INTO strategy_state (symbol, strategy, market, strategy_name, state_json, updated_at) SELECT symbol, strategy, market, COALESCE(strategy_name, ''), state_json, updated_at FROM strategy_state_old;")
            # 4. 이전 테이블 삭제
            cursor.execute("DROP TABLE strategy_state_old;")
    else:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS strategy_state (
                symbol TEXT,
                strategy TEXT,
                market TEXT DEFAULT 'US',
                strategy_name TEXT,
                is_active INTEGER DEFAULT 1,
                state_json TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (symbol, strategy, market, strategy_name)
            )
        ''')
    
    # 종료된 전략 상태 저장 테이블
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS finished_strategy_state (
            symbol TEXT,
            strategy TEXT,
            market TEXT,
            strategy_name TEXT,
            state_json TEXT,
            finished_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 사용자 인증 테이블 추가
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_auth (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            email TEXT UNIQUE,
            password_hash TEXT,
            is_verified INTEGER DEFAULT 0,
            is_temp_password INTEGER DEFAULT 0,
            failed_attempts INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # [추가] 사용자 UI 설정 저장 테이블
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_ui_settings (
            email TEXT PRIMARY KEY,
            last_market TEXT,
            last_symbol TEXT,
            last_strategy TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # OTP 관리 테이블 추가
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS otp_codes (
            email TEXT PRIMARY KEY,
            code TEXT,
            expires_at INTEGER
        )
    ''')

    # 시스템 설정 테이블 (스케줄러 상태 등)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS system_config (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # API 토큰 관리 테이블 추가
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS api_tokens (
            account_no TEXT PRIMARY KEY,
            market TEXT,
            token TEXT,
            expires_at REAL,
            issued_at REAL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Migration: Update existing tables
    cursor.execute("PRAGMA table_info(trade_history)")
    columns = [column[1] for column in cursor.fetchall()]
    if 'odno' not in columns:
        logger.info("🛠️ [Migration] trade_history 테이블에 odno 컬럼을 추가합니다.")
        cursor.execute("ALTER TABLE trade_history ADD COLUMN odno TEXT")
    if 'market' not in columns:
        cursor.execute("ALTER TABLE trade_history ADD COLUMN market TEXT DEFAULT 'US'")

    # [Migration] odno 중복 저장 방지를 위한 UNIQUE 인덱스 추가 (Partial Index)
    # 유효한 주문번호(odno)가 있는 경우에만 시장(market)별 유니크함을 보장합니다.
    try:
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_history_odno 
            ON trade_history (odno, market) 
            WHERE odno IS NOT NULL AND odno != '' AND odno != 'N/A'
        """)
    except Exception as e:
        logger.warning(f"⚠️ [DB] trade_history UNIQUE 인덱스 생성 실패 (중복 데이터 존재 가능성): {e}")

    cursor.execute("PRAGMA table_info(order_history)")
    if 'market' not in [c[1] for c in cursor.fetchall()]:
        cursor.execute("ALTER TABLE order_history ADD COLUMN market TEXT DEFAULT 'US'")

    cursor.execute("PRAGMA table_info(order_history)")
    if 'strategy_name' not in [c[1] for c in cursor.fetchall()]:
        logger.info("🛠️ [Migration] order_history 테이블에 strategy_name 컬럼을 추가합니다.")
        cursor.execute("ALTER TABLE order_history ADD COLUMN strategy_name TEXT")

    cursor.execute("PRAGMA table_info(trade_history)")
    if 'strategy_name' not in [c[1] for c in cursor.fetchall()]:
        logger.info("🛠️ [Migration] trade_history 테이블에 strategy_name 컬럼을 추가합니다.")
        cursor.execute("ALTER TABLE trade_history ADD COLUMN strategy_name TEXT")

    cursor.execute("PRAGMA table_info(trade_history)")
    th_cols = [c[1] for c in cursor.fetchall()]
    if 'avg_price' not in th_cols:
        cursor.execute("ALTER TABLE trade_history ADD COLUMN avg_price REAL DEFAULT 0.0")
    if 'realized_profit' not in th_cols:
        cursor.execute("ALTER TABLE trade_history ADD COLUMN realized_profit REAL DEFAULT 0.0")
    if 'realized_profit_rate' not in th_cols:
        cursor.execute("ALTER TABLE trade_history ADD COLUMN realized_profit_rate REAL DEFAULT 0.0")

    if 'cum_buy_amt' not in th_cols:
        cursor.execute("ALTER TABLE trade_history ADD COLUMN cum_buy_amt REAL DEFAULT 0.0")
    if 'cum_sell_amt' not in th_cols:
        cursor.execute("ALTER TABLE trade_history ADD COLUMN cum_sell_amt REAL DEFAULT 0.0")

    cursor.execute("PRAGMA table_info(strategy_state)")
    ss_cols = [c[1] for c in cursor.fetchall()]
    if 'market' not in ss_cols:
        cursor.execute("ALTER TABLE strategy_state ADD COLUMN market TEXT DEFAULT 'US'")
    if 'strategy_name' not in ss_cols:
        cursor.execute("ALTER TABLE strategy_state ADD COLUMN strategy_name TEXT")
    if 'is_active' not in ss_cols:
        logger.info("🛠️ [Migration] strategy_state 테이블에 is_active 컬럼을 추가합니다.")
        cursor.execute("ALTER TABLE strategy_state ADD COLUMN is_active INTEGER DEFAULT 1")

    # user_auth 추가 컬럼 마이그레이션
    cursor.execute("PRAGMA table_info(user_auth)")
    ua_cols = [c[1] for c in cursor.fetchall()]
    if 'is_temp_password' not in ua_cols:
        cursor.execute("ALTER TABLE user_auth ADD COLUMN is_temp_password INTEGER DEFAULT 0")
    if 'failed_attempts' not in ua_cols:
        cursor.execute("ALTER TABLE user_auth ADD COLUMN failed_attempts INTEGER DEFAULT 0")

    conn.commit()
    conn.close()

def save_ui_settings_db(email, market, symbol, strategy_alias):
    """사용자의 마지막 UI 선택 상태 저장"""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO user_ui_settings (email, last_market, last_symbol, last_strategy, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (email, market, symbol, strategy_alias))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"UI 설정 저장 실패: {e}")

def load_ui_settings_db(email):
    """사용자의 마지막 UI 선택 상태 로드"""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT last_market, last_symbol, last_strategy FROM user_ui_settings WHERE email=?', (email,))
        row = cursor.fetchone()
        conn.close()
        return row if row else (None, None, None)
    except Exception as e:
        logger.error(f"UI 설정 로드 실패: {e}")
        return None, None, None

def get_next_strategy_name(symbol, strategy_type, market):
    """차수를 올린 다음 전략 별칭 생성 (예: TQQQ_1차 -> TQQQ_2차)"""
    conn = get_connection()
    cursor = conn.cursor()
    # 활성 전략과 종료된 전략 모두에서 이름을 가져옴
    cursor.execute('''
        SELECT strategy_name FROM strategy_state WHERE symbol=? AND strategy=? AND market=?
        UNION
        SELECT strategy_name FROM finished_strategy_state WHERE symbol=? AND strategy=? AND market=?
    ''', (symbol, strategy_type, market, symbol, strategy_type, market))
    rows = cursor.fetchall()
    conn.close()

    max_n = 0
    base_prefix = None
    # 정규식: 이름_n차 (예: TQQQ_1차, 삼성전자(005930)_1차)
    pattern = re.compile(r'^(.*)_(\d+)차$')
    
    for row in rows:
        name = row[0]
        if not name: continue
        match = pattern.match(name)
        if match:
            if base_prefix is None: base_prefix = match.group(1)
            max_n = max(max_n, int(match.group(2)))
        else:
            if base_prefix is None: base_prefix = name

    if base_prefix is None:
        base_prefix = symbol
        
    return f"{base_prefix}_{max_n + 1}차"

def save_state_db(state, market: str = "US", strategy_name: str = "", only_if_exists: bool = False):
    """
    전략 상태를 DB에 저장.
    only_if_exists=True인 경우, 기존에 해당 전략이 DB에 있을 때만 업데이트합니다. (유령 전략 생성 방지)
    """
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # 1. 존재 여부 확인
        if only_if_exists:
            cursor.execute('''
                SELECT 1 FROM strategy_state 
                WHERE symbol=? AND strategy=? AND market=? AND strategy_name=?
            ''', (state.symbol, state.strategy_type, market, strategy_name))
            if not cursor.fetchone():
                logger.debug(f"⚠️ [DB] 존재하지 않는 전략({state.symbol}-{strategy_name})의 저장을 건너뜁니다.")
                conn.close()
                return

        state_dict = asdict(state)
        state_dict['market'] = market
        state_dict['strategy_name'] = strategy_name
        json_str = json.dumps(state_dict, ensure_ascii=False)
        
        cursor.execute('''
            INSERT OR REPLACE INTO strategy_state (symbol, strategy, market, strategy_name, state_json, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (state.symbol, state.strategy_type, market, strategy_name, json_str))
        
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to save state to DB: {e}")

def load_state_db(symbol, strategy_type, market: str = "US", strategy_name: str = ""):
    """DB에서 전략 상태 로드"""
    try:
        conn = get_connection()
        cursor = conn.cursor()

        query = 'SELECT state_json, strategy_name FROM strategy_state WHERE (symbol=? OR symbol LIKE ?) AND strategy=? AND market=?'
        params = [symbol, f"{symbol} %", strategy_type, market]

        if strategy_name:
            query += ' AND strategy_name=?'
            params.append(strategy_name)

        cursor.execute(query, params)
        row = cursor.fetchone()
        conn.close()
        
        if row:
            data = json.loads(row[0])
            data['strategy_name'] = row[1]
            data['market'] = market
            return data
        return None
    except Exception as e:
        logger.error(f"Failed to load state from DB: {e}")
        return None

def finish_strategy_db(symbol, strategy, market: str = "US", strategy_name: str = ""):
    """전략을 종료 상태로 이동 (history 관리)"""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # 1. 기존 상태 복사하여 finished 테이블에 삽입
        cursor.execute('''
            INSERT INTO finished_strategy_state (symbol, strategy, market, strategy_name, state_json, finished_at)
            SELECT symbol, strategy, market, strategy_name, state_json, CURRENT_TIMESTAMP
            FROM strategy_state
            WHERE symbol=? AND strategy=? AND market=? AND strategy_name=?
        ''', (symbol, strategy, market, strategy_name))
        
        # 2. 기존 활성 전략 테이블에서 삭제
        cursor.execute('''
            DELETE FROM strategy_state 
            WHERE symbol=? AND strategy=? AND market=? AND strategy_name=?
        ''', (symbol, strategy, market, strategy_name))
        
        conn.commit()
        conn.close()
        logger.info(f"🏁 전략 종료 및 이력 저장 완료: {symbol} ({strategy_name})")
    except Exception as e:
        logger.error(f"Failed to finish strategy: {e}")

def get_finished_states_db():
    """종료된 모든 전략 상태 조회"""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT symbol, strategy, market, strategy_name, state_json, finished_at FROM finished_strategy_state ORDER BY finished_at DESC')
        rows = cursor.fetchall()
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"Failed to load finished states: {e}")
        return []

def get_all_states_db(strategy_type=None, market=None, strategy_name=None, symbol=None):
    """저장된 모든 전략 상태 조회"""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        query = 'SELECT state_json, strategy_name, market, updated_at FROM strategy_state'
        params = []
        conditions = []
        if strategy_type:
            conditions.append('strategy=?')
            params.append(strategy_type)
        if market:
            conditions.append('market=?')
            params.append(market)
        if strategy_name:
            conditions.append('strategy_name=?')
            params.append(strategy_name)
        if symbol:
            # [FIX] 종목명 포함 형식(Ticker (Name)) 대응을 위한 LIKE 검색 도입
            conditions.append('(symbol=? OR symbol LIKE ?)')
            params.extend([symbol, f"{symbol} %"])

        if conditions:
            query += ' WHERE ' + ' AND '.join(conditions)

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()
        
        results = []
        for row in rows:
            data = json.loads(row[0])
            data['strategy_name'] = row[1]
            data['market'] = row[2]
            data['updated_at'] = row[3]
            results.append(data)
        return results
    except Exception as e:
        logger.error(f"Failed to load all states: {e}")
        return []

def log_trade_db(date, symbol, strategy, side, price, qty, fee, total_amount, turn, note, odno=None, market="US", strategy_name: str = "", 
                 avg_price=0.0, realized_profit=0.0, realized_profit_rate=0.0, cum_buy_amt=0.0, cum_sell_amt=0.0):
    """거래 내역 DB 저장"""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT OR IGNORE INTO trade_history (date, symbol, strategy, side, price, qty, fee, total_amount, turn, note, odno, market, strategy_name, avg_price, realized_profit, realized_profit_rate, cum_buy_amt, cum_sell_amt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (date, symbol, strategy, side, price, qty, fee, total_amount, turn, note, odno, market, strategy_name, avg_price, realized_profit, realized_profit_rate, cum_buy_amt, cum_sell_amt))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to log trade to DB: {e}")

def delete_strategy_db(symbol, strategy, market="US", strategy_name=None):
    """전략 상태 삭제"""
    try:
        conn = get_connection()
        cursor = conn.cursor()

        query = 'DELETE FROM strategy_state WHERE symbol=? AND strategy=? AND market=?'
        params = [symbol, strategy, market]

        if strategy_name is not None:
            query += ' AND strategy_name=?'
            params.append(strategy_name)

        cursor.execute(query, params)
        conn.commit()
        conn.close()
        logger.info(f"🗑️ DB에서 전략 삭제 완료: {symbol} ({strategy}) - Market: {market}, Name: {strategy_name}")
    except Exception as e:
        logger.error(f"Failed to delete strategy from DB: {e}")

def rename_strategy_db(symbol, strategy_type, market, old_strategy_name, new_strategy_name):
    """
    Renames an existing strategy in the database, updating all related records.
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        # 1. Check if the new_strategy_name already exists for this symbol/strategy_type/market
        cursor.execute('''
            SELECT 1 FROM strategy_state
            WHERE symbol=? AND strategy=? AND market=? AND strategy_name=?
        ''', (symbol, strategy_type, market, new_strategy_name))
        if cursor.fetchone():
            logger.error(f"Rename failed: New strategy name '{new_strategy_name}' already exists for {symbol} ({strategy_type}, {market}).")
            return False, "새 전략 별칭이 이미 존재합니다."

        # 1-1. Check trade_history (이력 기록 중복 방지)
        cursor.execute('''
            SELECT 1 FROM trade_history WHERE symbol=? AND market=? AND strategy_name=?
        ''', (symbol, market, new_strategy_name))
        if cursor.fetchone():
            return False, "거래 내역에 이미 동일한 별칭의 기록이 존재하여 이름을 변경할 수 없습니다."

        # 1-2. Check order_history (주문 내역 중복 방지)
        cursor.execute('''
            SELECT 1 FROM order_history WHERE symbol=? AND market=? AND strategy_name=?
        ''', (symbol, market, new_strategy_name))
        if cursor.fetchone():
            return False, "주문 내역에 이미 동일한 별칭의 기록이 존재하여 이름을 변경할 수 없습니다."

        # 2. Update strategy_state
        cursor.execute('''
            UPDATE strategy_state
            SET strategy_name = ?
            WHERE symbol=? AND strategy=? AND market=? AND strategy_name=?
        ''', (new_strategy_name, symbol, strategy_type, market, old_strategy_name))
        if cursor.rowcount == 0:
            logger.warning(f"Strategy state not found for renaming: {symbol} ({strategy_type}, {market}, {old_strategy_name})")
            return False, "기존 전략을 찾을 수 없습니다."

        # 3. Update trade_history
        cursor.execute('''
            UPDATE trade_history
            SET strategy_name = ?
            WHERE symbol=? AND strategy=? AND market=? AND strategy_name=?
        ''', (new_strategy_name, symbol, strategy_type, market, old_strategy_name))

        # 4. Update order_history
        cursor.execute('''
            UPDATE order_history
            SET strategy_name = ?
            WHERE symbol=? AND strategy=? AND market=? AND strategy_name=?
        ''', (new_strategy_name, symbol, strategy_type, market, old_strategy_name))

        conn.commit()
        logger.info(f"Strategy renamed successfully: {old_strategy_name} -> {new_strategy_name} for {symbol} ({strategy_type}, {market})")
        return True, "전략 별칭이 성공적으로 변경되었습니다."
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to rename strategy {old_strategy_name} to {new_strategy_name}: {e}")
        return False, f"전략 이름 변경 중 오류 발생: {e}"
    finally:
        conn.close()

def log_order_db(symbol, strategy, side, price, qty, type, status, odno, msg, market="US", strategy_name: str = ""):
    """주문 제출 내역 DB 저장"""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO order_history (symbol, strategy, side, price, qty, type, status, odno, msg, market, strategy_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (symbol, strategy, side, price, qty, type, status, odno, msg, market, strategy_name))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to log order to DB: {e}")

def delete_order_by_odno_db(odno):
    """체결 또는 취소된 주문을 주문 내역에서 삭제"""
    if not odno or odno == 'N/A':
        return
    normalized_odno = str(odno)

    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM order_history WHERE LTRIM(odno, '0')=?", (normalized_odno,))
        affected_rows = cursor.rowcount
        conn.commit()
        conn.close()
        if affected_rows > 0:
            logger.info(f"🗑️ DB에서 주문 삭제 완료 (ODNO: {odno})")
    except Exception as e:
        logger.error(f"Failed to delete order {odno}: {e}")

def cleanup_invalid_orders_db():
    """가격이나 수량이 0인 불완전한 주문 내역을 삭제합니다."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM order_history WHERE price = 0 OR qty = 0")
        count = cursor.rowcount
        conn.commit()
        conn.close()
        return count
    except Exception as e:
        logger.error(f"Cleanup failed: {e}")
        return 0

def cleanup_processed_orders_db():
    """이미 체결된(trade_history에 존재하는) 주문번호를 미체결 내역(order_history)에서 삭제"""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM order_history WHERE odno IN (SELECT odno FROM trade_history WHERE odno IS NOT NULL AND odno != 'N/A' AND odno != '')")
        count = cursor.rowcount
        conn.commit()
        conn.close()
        return count
    except Exception as e:
        logger.error(f"Cleanup processed orders failed: {e}")
        return 0

def cleanup_old_canceled_orders_db(days=3):
    """3일 이상 지난 CANCELED 주문 내역 삭제"""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(f"DELETE FROM order_history WHERE status='CANCELED' AND timestamp < datetime('now', 'localtime', '-{days} days')")
        count = cursor.rowcount
        conn.commit()
        conn.close()
        if count > 0:
            logger.info(f"🗑️ [Cleanup] 3일 이상 경과된 취소 주문 {count}건을 삭제했습니다.")
        return count
    except Exception as e:
        logger.error(f"Cleanup old canceled orders failed: {e}")
        return 0

def sync_open_orders_db(symbol, open_orders):
    """KIS 미체결 내역과 로컬 DB 동기화 (재시작 대응)"""
    try:
        symbol = symbol.strip().upper()
        conn = get_connection()
        cursor = conn.cursor()

            
        # 1. 현재 해당 종목으로 실행 중인 전략 정보(별칭, 시장, 전략타입)를 가져옵니다.
        # 활성(is_active=1) 전략 중 가장 최근 것을 우선순위로 합니다.
        cursor.execute("""
            SELECT strategy_name, market, strategy 
            FROM strategy_state
            WHERE (TRIM(UPPER(symbol))=? OR symbol LIKE ?) AND is_active=1
            ORDER BY updated_at DESC LIMIT 1
        """, (symbol, f"{symbol} %"))
        strat_res = cursor.fetchone()
            
        if not strat_res:
            # 활성 전략이 없다면 전체에서 최근 것을 찾습니다.
            cursor.execute("SELECT strategy_name, market, strategy FROM strategy_state WHERE TRIM(UPPER(symbol))=? ORDER BY updated_at DESC LIMIT 1", (symbol,))
            strat_res = cursor.fetchone()

        active_alias = strat_res[0] if strat_res else ""
        m_code = strat_res[1] if strat_res else "US"
        s_type = strat_res[2] if strat_res else "SYNC"
       
        # 2. KIS 서버의 활성 주문 번호 리스트 (수량이 0인 유령 데이터 제외)
        # [NML] 심볼 정규화 (한국 주식 코드 A122630 -> 122630 대응)
        clean_symbol = str(symbol).strip().upper()
        if clean_symbol.startswith('A') and len(clean_symbol) == 7 and clean_symbol[1:].isdigit():
            clean_symbol = clean_symbol[1:]
        # [NML] 비교를 위해 주문번호 그대로 사용
        active_odnos = [str(o.get('odno')) for o in open_orders if o.get('odno') and float(o.get('nccs_qty') or o.get('ft_ord_qty3') or 0) > 0]
             
        # 3. DB에는 활성 상태로 있는데 KIS에는 없는 주문 처리 (취소 내역 보존을 위해 수정)
        if active_odnos:
            placeholders = ', '.join(['?'] * len(active_odnos))
            # 1) 이미 체결되어 trade_history에 기록된 주문은 중복 방지를 위해 삭제
            cursor.execute(f"""
                DELETE FROM order_history 
                WHERE (symbol=? OR symbol=?) AND odno NOT IN ({placeholders})
                AND odno IN (SELECT odno FROM trade_history WHERE odno IS NOT NULL AND odno != 'N/A')
            """, [clean_symbol, "A" + clean_symbol] + active_odnos)
            # 2) 나머지는 미체결 취소로 간주하고 상태 업데이트 (단, 주문한지 30초 이상 지난 것만)
            cursor.execute(f"""
                UPDATE order_history SET status='CANCELED', msg='Canceled (Not filled)'
                WHERE (symbol=? OR symbol=?) AND status='SUCCESS' AND odno NOT IN ({placeholders})
                AND timestamp < datetime('now', 'localtime', '-30 seconds')
            """, [clean_symbol, "A" + clean_symbol] + active_odnos)
        else:
            # KIS에 미체결이 하나도 없는 경우
            cursor.execute("DELETE FROM order_history WHERE (symbol=? OR symbol=?) AND odno IN (SELECT odno FROM trade_history WHERE odno IS NOT NULL)", (clean_symbol, "A" + clean_symbol))
            cursor.execute("""
                UPDATE order_history SET status='CANCELED', msg='Canceled (Not filled)' 
                WHERE (symbol=? OR symbol=?) AND status='SUCCESS' 
                AND timestamp < datetime('now', 'localtime', '-30 seconds')
            """, (clean_symbol, "A" + clean_symbol))

        # 4. KIS 미체결 내역을 DB에 반영 (신규 추가 또는 0값 업데이트)
        new_count = 0
        update_count = 0
        for o in open_orders:
            odno = o.get('odno')
            if not odno: continue

            # 가격 및 수량 추출 (US/KR/Full 필드 통합 대응)
            # KIS는 unpr, ord_unpr, ft_ord_unpr3 등 다양한 필드명을 사용함
            # 미체결 수량(nccs_qty)을 최우선으로 확인
            p_raw = o.get('ft_ord_unpr3') or o.get('ord_unpr') or o.get('unpr') or o.get('pdno_unpr') or '0'
            q_raw = o.get('nccs_qty') or o.get('ft_ord_qty3') or o.get('ord_qty') or o.get('ord_psbl_qty') or '0'
            
            try:
                price = float(str(p_raw).strip() if p_raw and str(p_raw).strip() != "" else 0)
                qty = float(str(q_raw).strip() if q_raw and str(q_raw).strip() != "" else 0)
            except:
                price, qty = 0.0, 0.0

            if price == 0 or qty == 0:
                # 수량이 0이면 이미 체결된 주문이므로 DB에서 삭제하고 건너뜀
                cursor.execute("DELETE FROM order_history WHERE odno=?", (str(o.get('odno')),))
                logger.debug(f"🗑️ [DB_SYNC] 체결 완료된 주문({odno}) 삭제 처리")
                logger.debug(f"Raw Data: {o}")
                continue

            cursor.execute("SELECT id, price, qty, strategy_name, status FROM order_history WHERE odno=? AND symbol=?", (odno, symbol))
            existing = cursor.fetchone()

            if not existing:
                # 매도/매수 구분 (US: 1/2, KR: 01/02)
                side_raw = str(o.get('sll_buy_dvsn_cd', ''))
                side = "매도" if side_raw in ['01', '1', '매도'] else "매수"                            
                # KIS 주문 시각 파싱 (YYYY-MM-DD HH:MM:SS)
                odt = o.get('ord_dt', '')
                otm = o.get('ord_tmd', '')
                if len(odt) == 8 and len(otm) == 6:
                    order_ts = f"{odt[:4]}-{odt[4:6]}-{odt[6:8]} {otm[:2]}:{otm[2:4]}:{otm[4:6]}"
                else:
                    order_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


                ord_type_code = o.get('ovrs_ord_dvsn_cd') or o.get('ord_dvsn') or '00'

                cursor.execute('''
                    INSERT INTO order_history (timestamp, symbol, strategy, side, price, qty, type, status, odno, msg, market, strategy_name)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (order_ts, symbol, s_type, side, price, qty, 
                      ord_type_code, 'SUCCESS', odno, 'Synced from KIS', m_code, active_alias))
                new_count += 1
            else:
                # 기존 기록이 0이거나 별칭이 없는 경우 업데이트 시도
                curr_id, upd_price, upd_qty, upd_alias, existing_status = existing
                needs_upd = False
                    
                if upd_price == 0 and price > 0:
                    upd_price = price
                    needs_upd = True
                if (upd_qty == 0 or upd_qty != qty) and qty > 0:
                    upd_qty = qty
                    needs_upd = True
                if (not upd_alias or upd_alias == "") and active_alias:
                    upd_alias = active_alias
                    needs_upd = True

                # [FIX] KIS 서버에 주문이 살아있다면 CANCELED 상태에서도 복구
                if needs_upd or existing_status == 'CANCELED':
                    cursor.execute('''
                        UPDATE order_history
                        SET price=?, qty=?, strategy_name=?, status='SUCCESS', msg='Restored from KIS' 
                        WHERE id=?
                    ''', (upd_price, upd_qty, upd_alias, curr_id))
                    update_count += 1
        
        conn.commit()
        conn.close()
        if new_count > 0 or update_count > 0:
            logger.info(f"🔄 [DB_SYNC] {symbol}: 신규 {new_count}건, 업데이트 {update_count}건 처리 (Alias: {active_alias})")
        
    except Exception as e:
        logger.error(f"Failed to sync orders for {symbol}: {e}")

def sync_trade_history_db(symbol, executions, strategy=None, market="US", strategy_name=None, silent=False):
    """API 체결 내역과 DB 거래 내역 동기화 (누락분 추가)"""
    try:
        # [NML] 심볼 정규화 (한국 주식 코드 A122630 -> 122630 대응)
        symbol = str(symbol).strip().upper()
        if symbol.startswith('A') and len(symbol) == 7 and symbol[1:].isdigit():
            symbol = symbol[1:]
        conn = get_connection()
        cursor = conn.cursor()
        
        new_count = 0
        update_count = 0
        processed_db_ids = set() # 이번 루프에서 이미 업데이트한 DB ID 추적
        strategy_to_save = strategy if strategy else "SYNC"
        # 별칭이 지정된 경우 해당 전략만, 아니면 해당 시장의 모든 전략 대상
        target_alias = strategy_name if strategy_name else ""
        
        if not silent:
            logger.info(f"🔄 [DB_SYNC] {symbol} ({target_alias}) 체결 내역 동기화 시작: {len(executions)}건 수신")
            # [DEBUG] API 수신 데이터 원본 상세 로깅 (필요 시 주석 해제하여 확인)
            # for i, ex in enumerate(executions):
            #     logger.info(f"  [API_RAW #{i}] {ex}")

        # 시장별 환경 변수 로드 (수수료/세금)
        env_suffix = market.lower()
        env_path = os.path.join(BASE_DIR, "env", f".env.{env_suffix}")
        if os.path.exists(env_path):
            from dotenv import load_dotenv
            load_dotenv(env_path, override=True)

        for ex in executions:
            # [보강] 한국 시장과 미국 시장의 필드명 차이 대응
            # 미국: ft_ccld_unpr3, ft_ccld_qty, ft_ccld_amt3 / 한국: pndn_unpr, pndn_qty, pndn_amt
            # 날짜: ord_dt 또는 stck_bsop_date

            # 체결 시각 추출
            # REST API (US/KR)는 'ord_tmd'를, 웹소켓은 'exec_time'을 사용 (여기서는 REST API 히스토리 처리)
            raw_time = ex.get('ord_tmd') or ex.get('ft_ccld_tm') or ex.get('stck_cntg_hour') or '000000'
            raw_date = ex.get('ord_dt') or ex.get('stck_bsop_date') or ''
            date_formatted = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}" if len(raw_date) == 8 else raw_date

            if len(raw_time) == 6:
                full_time_str = f"{raw_time[:2]}:{raw_time[2:4]}:{raw_time[4:6]}"
                date_formatted = f"{date_formatted} {full_time_str}" # date 필드에 시각 포함
            else:
                date_formatted = f"{date_formatted} 00:00:00"
            
            side = "BUY" if ex.get('sll_buy_dvsn_cd') in ['02', '2'] else "SELL"
            # [보강] 필드 매핑 유연화 (US/KR 공용)
            price = float(ex.get('ft_ccld_unpr3') or ex.get('pndn_unpr') or ex.get('stck_clpr') or 0)
            qty = float(ex.get('ft_ccld_qty') or ex.get('pndn_qty') or ex.get('stck_clqty') or 0)
            total_amount_api = float(ex.get('ft_ccld_amt3') or ex.get('pndn_amt') or ex.get('stck_clamt') or 0)
            principal = price * qty

            if qty == 0:
                continue # 수량이 0인 취소 주문 등은 스킵

            # 1. 환경 변수 및 요율 로드 (이익 계산을 위해 상단 배치)
            fee_rate = float(os.getenv("fee_rate", "0.000038" if market == "KR" else "0.0009"))
            tax_rate = float(os.getenv("tax_sell", "0.0020" if market == "KR" else "0.0000206"))

            # 1.1 수수료 및 정산 금액 선계산
            # API 정산금액이 원금과 거의 같으면(오차 0.01 미만) 수수료가 누락된 것으로 보고 직접 계산
            if total_amount_api > 0 and abs(total_amount_api - principal) > 0.01:
                fee = round(abs(total_amount_api - principal), 2)
            else:
                # SYNC인 경우 CA를 기본으로 참조하여 전략별 특화 수수료가 있는지 확인
                ref_strategy = strategy_to_save if strategy_to_save != "SYNC" else "CA"
                cursor.execute('SELECT state_json FROM strategy_state WHERE symbol=? AND strategy=? AND market=?', (symbol, ref_strategy, market))
                res_state = cursor.fetchone()
                if res_state:
                    try:
                        temp_dict = json.loads(res_state[0])
                        fee_rate = float(temp_dict.get('fee_rate', fee_rate))
                    except: pass
                # 수수료: 원금 * 요율, 소수점 셋째 자리에서 반올림 (결과적으로 소수점 2자리)
                fee = round(principal * fee_rate, 2)

            if side == "BUY":
                total_amount = principal + fee
            else:
                # 한국은 세금(0.2%), 미국은 SEC Fee(0.00206%) 적용
                tax = round(principal * tax_rate, 2)
                total_amount = principal - fee - tax
            
            # Calculate Profit if SELL
            realized_profit = 0.0
            realized_profit_rate = 0.0
            avg_price_at_trade = 0.0
            
            if side == "SELL":
                # Get current avg_price from state if possible
                cursor.execute('SELECT state_json FROM strategy_state WHERE (symbol=? OR symbol LIKE ?) AND strategy=? AND market=? AND strategy_name=?', 
                               (symbol, f"{symbol} %", strategy_to_save, market, target_alias))
                row_st = cursor.fetchone()
                if row_st:
                    st_json = json.loads(row_st[0])
                    avg_price_at_trade = float(st_json.get('avg_price', 0))
                    
                    # [FIX] 만약 상태 정보에 평단가가 0이라면, trade_history에서 해당 별칭의 마지막 유효 평단가 탐색
                    if avg_price_at_trade <= 0:
                        cursor.execute('''
                            SELECT avg_price FROM trade_history 
                            WHERE symbol=? AND market=? AND strategy_name=? AND avg_price > 0 
                            ORDER BY date DESC, id DESC LIMIT 1
                        ''', (symbol, market, target_alias))
                        row_th = cursor.fetchone()
                        if row_th:
                            avg_price_at_trade = row_th[0]

                    # 매수 시점의 추정 비용(원금 + 수수료) 계산
                    # SYNC인 경우에도 현재 로드된 fee_rate를 사용
                    cost_basis = avg_price_at_trade * qty * (1 + fee_rate)
                    
                    realized_profit = total_amount - cost_basis
                    if cost_basis > 0:
                        realized_profit_rate = (realized_profit / cost_basis) * 100

            odno_raw = ex.get('odno', 'N/A')
            # [NML] 주문번호 그대로 사용
            odno = str(odno_raw) if odno_raw != 'N/A' else 'N/A'
            
            # 2. 중복 체크 강화: odno 컬럼뿐만 아니라 note 필드 내 텍스트까지 검색
            is_duplicate = False
            if odno and odno != 'N/A':
                cursor.execute("SELECT id, qty FROM trade_history WHERE (odno=? OR note LIKE ?) AND market=?", (odno, f'%{odno}%', market))
                existing = cursor.fetchone()
                if existing and existing[0] not in processed_db_ids:
                    is_duplicate = True
                    # [개선] 기존 기록이 있지만 별칭이 일반(MANUAL/SYNC)인 경우 현재 전략으로 업데이트
                    cursor.execute('SELECT strategy, strategy_name FROM trade_history WHERE id=?', (existing[0],))
                    row_check = cursor.fetchone()
                    curr_strat, curr_alias = row_check if row_check else (None, None)
                    
                    curr_alias_clean = str(curr_alias).strip() if curr_alias else ""
                    target_alias_clean = str(target_alias).strip() if target_alias else "" # type: ignore

                    if target_alias_clean and curr_alias_clean != target_alias_clean and curr_alias_clean in ['', 'SYNC', 'MANUAL']:
                        cursor.execute('UPDATE trade_history SET strategy=?, strategy_name=? WHERE id=?', 
                                     (strategy_to_save, target_alias, existing[0]))
                        update_count += 1
                        processed_db_ids.add(existing[0])
                        
                    # 기존 기록이 있으나 수량이 0인 불완전한 데이터인 경우 정보를 업데이트
                    if float(existing[1]) == 0 and qty > 0:
                        cursor.execute('''
                            UPDATE trade_history 
                            SET qty=?, total_amount=?, fee=?, odno=? 
                            WHERE id=?
                        ''', (qty, total_amount, fee, odno, existing[0]))
                        update_count += 1
            
            if is_duplicate:
                continue

            # [ADD] Alias 복구: order_history에 기록된 원래 전략 정보를 찾아 연결
            cursor.execute("SELECT strategy, strategy_name FROM order_history WHERE odno=?", (odno,))
            orig_info = cursor.fetchone()
            if orig_info and (strategy_to_save in ['SYNC', 'MANUAL'] or not target_alias):
                strategy_to_save = orig_info[0]
                target_alias = orig_info[1]
            
            # [ADD] 만약 여전히 별칭이 없다면 strategy_state에서 해당 종목의 활성 별칭을 조회하여 보완
            if not target_alias or target_alias in ['', 'SYNC', 'MANUAL']:
                cursor.execute('''
                    SELECT strategy, strategy_name FROM strategy_state 
                    WHERE (symbol=? OR symbol LIKE ? OR symbol=? OR symbol LIKE ?) AND market=? AND is_active=1 LIMIT 1
                ''', (symbol, f"{symbol} %", "A" + symbol, "A" + symbol + " %", market))
                fallback = cursor.fetchone()
                if fallback:
                    strategy_to_save, target_alias = fallback

            # [ADD] 체결이 확인되었으므로 미체결 주문 내역(order_history)에서 즉시 삭제 (중복 방지 강화)
            cursor.execute("DELETE FROM order_history WHERE odno=? AND (symbol=? OR symbol=?)", (odno, symbol, "A" + symbol))

            # 3. 주문번호가 없는 기존 기록 중 동일 조건 검색 (병합 처리)
            cursor.execute('''
                SELECT id FROM trade_history
                WHERE date=? AND symbol=? AND side=? AND price=? AND (qty=? OR qty=0) AND (odno IS NULL OR odno='N/A') AND market=?
            ''', (date_formatted, symbol, side, price, qty, market))
            match_without_odno = cursor.fetchone()

            if match_without_odno:
                # 주문번호가 없던 기존 기록에 정보 업데이트 (중복 방지)
                cursor.execute('''
                    UPDATE trade_history 
                    SET odno=?, qty=?, total_amount=?, fee=?, note=? 
                    WHERE id=?
                ''', (odno, qty, total_amount, fee, f"Synced (ODNO: {odno})", match_without_odno[0]))
            else:
                # Requirement 4: 누적 매수/매도액 계산
                cursor.execute("SELECT SUM(total_amount) FROM trade_history WHERE strategy_name=? AND symbol=? AND market=? AND side='BUY'", (target_alias, symbol, market))
                c_buy = (cursor.fetchone()[0] or 0.0) + (total_amount if side == "BUY" else 0.0)
                cursor.execute("SELECT SUM(total_amount) FROM trade_history WHERE strategy_name=? AND symbol=? AND market=? AND side='SELL'", (target_alias, symbol, market))
                c_sell = (cursor.fetchone()[0] or 0.0) + (total_amount if side == "SELL" else 0.0)

                cursor.execute('''
                    INSERT OR IGNORE INTO trade_history (date, symbol, strategy, side, price, qty, fee, total_amount, turn, note, odno, market, strategy_name, avg_price, realized_profit, realized_profit_rate, cum_buy_amt, cum_sell_amt)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    date_formatted, 
                    symbol, 
                    strategy_to_save, 
                    side, 
                    price, 
                    qty,
                    fee, 
                    total_amount, 
                    0.0, 
                    f"Synced (ODNO: {odno})",
                    odno,
                    market,
                    target_alias,
                    avg_price_at_trade,
                    realized_profit,
                    realized_profit_rate,
                    c_buy,
                    c_sell
                ))
                
                # [ADD] 체결이 확인되었으므로 미체결 주문 내역(order_history)에서 즉시 삭제
                cursor.execute("DELETE FROM order_history WHERE odno=?", (odno,))

                # [ADD] 누락된 거래 내역 복구 시 텔레그램 알림 발송
                cur_sym = "₩" if market == "KR" else "$"
                price_fmt = f"{int(price):,}" if market == "KR" else f"{price:,.2f}"
                symbol_display = format_symbol_display(symbol, market) # 종목명과 코드를 함께 표시
                tg_msg = (
                    f"⚡ <b>[{market} 체결 알림 (복구됨)]</b>\n"
                    f"일시: {date_formatted}\n"
                    f"종목: <b>{symbol_display}</b>\n"    # 종목명 표시
                    f"구분: {side}\n"
                    f"수량: {int(qty)}주\n"
                    f"가격: {cur_sym}{price_fmt}\n"
                    f"<i>(이 메시지는 누락된 체결 내역 동기화 과정에서 발송되었습니다.)</i>"
                )
                if not silent:
                    send_telegram_message(tg_msg)

                new_count += 1
                
                # [중요] 새로운 체결 발견 시 전략 상태(State) 자동 복구
                if strategy_to_save == "CA":
                    # CA 전략 상태 로드
                    query = 'SELECT state_json, strategy_name FROM strategy_state WHERE symbol=? AND strategy=? AND market=?'
                    params = [symbol, "CA", market]
                    if strategy_name:
                        query += ' AND strategy_name=?'
                        params.append(strategy_name)
                    
                    cursor.execute(query, params)
                    rows = cursor.fetchall()
                    for row in rows:
                        state_dict = json.loads(row[0])
                        row_alias = row[1]
                        old_qty = float(state_dict.get('total_shares', 0))
                        old_avg = float(state_dict.get('avg_price', 0))
                        
                        if side == "BUY":
                            new_total_qty = old_qty + qty
                            new_avg = ((old_qty * old_avg) + (qty * price)) / new_total_qty if new_total_qty > 0 else price
                            state_dict['last_execution_price'] = price
                        else: # SELL
                            new_total_qty = max(0, old_qty - qty)
                            new_avg = old_avg if new_total_qty > 0 else 0.0
                            
                            if new_total_qty == 0:
                                state_dict['pending_cycle_transition'] = True
                                logger.info(f"🚩 [Sync] {symbol} 전량 매도 확인됨. 차수 전환 대기 상태로 변경.")
                        
                        state_dict['total_shares'] = new_total_qty
                        state_dict['avg_price'] = new_avg
                        
                        unit_buy = float(state_dict.get('unit_buy_amount', 0))
                        if unit_buy > 0:
                            invested = new_total_qty * new_avg
                            state_dict['current_turn'] = math.ceil((invested / unit_buy) * 10) / 10.0
                        
                        new_turn = state_dict.get('current_turn', 0.0)
                        cursor.execute('''
                            UPDATE strategy_state SET state_json = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE symbol=? AND strategy=? AND market=? AND strategy_name=?
                        ''', (json.dumps(state_dict, ensure_ascii=False), symbol, "CA", market, row_alias))
                        
                        # [추가] 방금 삽입된 거래 내역 레코드에도 계산된 T(회차) 반영
                        cursor.execute('''
                            UPDATE trade_history SET turn = ?
                            WHERE (symbol=? OR symbol=?) AND odno=? AND turn=0.0 AND market=?
                        ''', (new_turn, symbol, "A" + symbol, odno, market))

                elif strategy_to_save == "VR":
                    # VR 전략 상태 로드 (Pool 복구)
                    query = 'SELECT state_json, strategy_name FROM strategy_state WHERE symbol=? AND strategy=? AND market=?'
                    params = [symbol, "VR", market]
                    if strategy_name:
                        query += ' AND strategy_name=?'
                        params.append(strategy_name)

                    cursor.execute(query, params)
                    rows = cursor.fetchall()
                    for row in rows:
                        state_dict = json.loads(row[0])
                        row_alias = row[1]
                        current_pool = float(state_dict.get('pool', 0.0))
                        trade_amt = price * qty
                        
                        if side == "BUY":
                            state_dict['pool'] = current_pool - trade_amt
                        else:
                            state_dict['pool'] = current_pool + trade_amt
                        
                        cursor.execute('''
                            UPDATE strategy_state SET state_json = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE symbol=? AND strategy=? AND market=? AND strategy_name=?
                        ''', (json.dumps(state_dict, ensure_ascii=False), symbol, "VR", market, row_alias))
        
        conn.commit()
        conn.close()
        if not silent:
            if new_count > 0 or update_count > 0:
                logger.info(f"✅ [DB_SYNC] {symbol}: {new_count}건 복구, {update_count}건 업데이트 완료 (Alias: {target_alias})")
            else:
                logger.debug(f"🔄 [DB_SYNC] {symbol}: 추가할 새로운 내역이 없습니다.")

    except Exception as e:
        logger.error(f"Failed to sync trade history for {symbol}: {e}")

def migrate_sync_trades_db(symbol, target_strategy):
    """'SYNC'로 표시된 거래 내역을 특정 전략으로 변경"""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE trade_history 
            SET strategy = ? 
            WHERE symbol = ? AND strategy = 'SYNC'
        ''', (target_strategy, symbol))
        
        # [중요] 중복 데이터 클리닝: 수량이 0인 불완전한 데이터가 정상 데이터와 공존할 경우 삭제
        cursor.execute('''
            DELETE FROM trade_history 
            WHERE symbol = ? AND qty = 0 AND (
                odno IN (SELECT odno FROM trade_history WHERE symbol = ? AND qty > 0) OR
                note IN (SELECT note FROM trade_history WHERE symbol = ? AND qty > 0)
            )
        ''', (symbol, symbol, symbol))

        # 동일 조건(주문번호, 날짜, 가격, 수량) 중 가장 오래된 하나만 남기고 삭제
        cursor.execute('''
            DELETE FROM trade_history 
            WHERE id NOT IN (
                SELECT MIN(id) 
                FROM trade_history 
                WHERE symbol = ?
                GROUP BY COALESCE(NULLIF(odno, ''), 'N/A'), date, price, qty
            ) AND symbol = ?
        ''', (symbol, symbol))
        
        count = cursor.rowcount # 실제 업데이트된 건수 (삭제 제외)
        conn.commit()
        conn.close()
        return count
    except Exception as e:
        logger.error(f"Failed to migrate SYNC trades for {symbol}: {e}")
        return 0

def migrate_manual_trades_db(symbol, target_strategy):
    """'MANUAL'로 표시된 거래 내역을 특정 전략(CA 등)으로 변경"""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        # 1. strategy를 MANUAL에서 target_strategy로 업데이트
        cursor.execute('''
            UPDATE trade_history 
            SET strategy = ? 
            WHERE symbol = ? AND strategy = 'MANUAL'
        ''', (target_strategy, symbol))
        
        # 2. 중복 데이터 클리닝 (migrate_sync_trades_db와 동일 로직)
        cursor.execute('''
            DELETE FROM trade_history 
            WHERE symbol = ? AND qty = 0 AND (
                odno IN (SELECT odno FROM trade_history WHERE symbol = ? AND qty > 0) OR
                note IN (SELECT note FROM trade_history WHERE symbol = ? AND qty > 0)
            )
        ''', (symbol, symbol, symbol))

        cursor.execute('''
            DELETE FROM trade_history 
            WHERE id NOT IN (
                SELECT MIN(id) 
                FROM trade_history 
                WHERE symbol = ?
                GROUP BY COALESCE(NULLIF(odno, ''), 'N/A'), date, price, qty
            ) AND symbol = ?
        ''', (symbol, symbol))
        
        count = cursor.rowcount
        conn.commit()
        conn.close()
        return count
    except Exception as e:
        logger.error(f"Failed to migrate MANUAL trades for {symbol}: {e}")
        return 0

def get_order_history_db(symbol=None, market=None):
    """주문 내역 조회"""
    try:
        conn = get_connection()
        query = "SELECT timestamp, symbol, strategy, side, price, qty, type, status, odno, msg, market, strategy_name FROM order_history"
        params = []
        conditions = []
        if symbol:
            conditions.append("symbol=?")
            params.append(symbol)
        if market:
            conditions.append("market=?")
            params.append(market)
            
        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY id DESC LIMIT 50"
        cursor = conn.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()
        cols = [description[0] for description in cursor.description]
        conn.close()
        return cols, rows
    except Exception as e:
        logger.error(f"Failed to get order history: {e}")
        return [], []

def get_canceled_orders_db(market: str):
    """금일 발생한 미체결 취소 주문 내역 조회"""
    try:
        today_str = datetime.now().strftime("%Y-%m-%d")
        conn = get_connection()
        cursor = conn.cursor()
        # timestamp가 오늘이고 status가 CANCELED인 주문 추출
        cursor.execute("""
            SELECT symbol, strategy_name, side, price, qty, timestamp 
            FROM order_history 
            WHERE market=? AND status='CANCELED' AND timestamp LIKE ?
            ORDER BY timestamp DESC
        """, (market, f"{today_str}%"))
        rows = cursor.fetchall()
        cols = [description[0] for description in cursor.description]
        conn.close()
        return cols, rows
    except Exception as e:
        logger.error(f"Failed to get canceled orders: {e}")
        return [], []

def save_api_token_db(account_no: str, market: str, token: str, expires_at: float, issued_at: float):
    """API 토큰 정보를 DB에 저장 또는 업데이트"""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO api_tokens (account_no, market, token, expires_at, issued_at, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (account_no, market, token, expires_at, issued_at))
        conn.commit()
        conn.close()
        logger.debug(f"✅ API 토큰 DB 저장/업데이트 완료: 계좌 {account_no}, 시장 {market}")
    except Exception as e:
        logger.error(f"API 토큰 DB 저장 실패: {e}")

def load_api_token_db(account_no: str) -> dict:
    """
    DB에서 특정 계좌번호에 해당하는 API 토큰 정보를 로드합니다.
    단일 계좌에 단일 토큰을 가정하므로 market 필터링 없이 account_no로만 조회합니다.
    """
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT market, token, expires_at, issued_at FROM api_tokens WHERE account_no=?', (account_no,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return {
                "market": row[0],
                "token": row[1],
                "expires_at": row[2],
                "issued_at": row[3]
            }
        return {}
    except Exception as e:
        logger.error(f"API 토큰 DB 로드 실패: {e}")
        return {}

def delete_api_token_db(account_no: str):
    """DB에서 특정 계좌의 토큰 정보를 삭제합니다."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM api_tokens WHERE account_no=?', (account_no,))
        conn.commit()
        conn.close()
        logger.info(f"🗑️ API 토큰 DB 삭제 완료 (계좌: {account_no})")
    except Exception as e:
        logger.error(f"API 토큰 DB 삭제 실패: {e}")

def get_detailed_trade_history_db(symbol, strategy=None, market=None):
    """특정 종목의 상세 거래 내역 조회 (DataFrame 변환용)"""
    try:
        conn = get_connection()
        query = "SELECT date, symbol, strategy, market, strategy_name, side, turn, price, qty, fee, total_amount, note, odno, avg_price, realized_profit, realized_profit_rate, cum_buy_amt, cum_sell_amt FROM trade_history"
        params = []
        conditions = []
        if symbol:
            conditions.append("symbol=?")
            params.append(symbol)
        if strategy:
            conditions.append("strategy=?")
            params.append(strategy)
        if market:
            conditions.append("market=?")
            params.append(market)
            
        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY date DESC, id DESC"
        
        cursor = conn.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()
        cols = [description[0] for description in cursor.description]
        conn.close()
        return cols, rows
    except Exception as e:
        logger.error(f"Failed to get trade history: {e}")
        return [], []

def recalculate_all_profits_db(symbol: str, market: str):
    """
    특정 종목/시장의 거래 내역을 시간순으로 정렬하여 평단가와 실현 손익을 처음부터 다시 계산합니다.
    (데이터 정합성 복구를 위한 일회성 작업용)
    """
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # 1. 해당 종목의 모든 거래 내역 가져오기 (날짜 및 ID 순)
        cursor.execute('''
            SELECT id, side, price, qty, fee, total_amount, strategy_name 
            FROM trade_history 
            WHERE symbol=? AND market=?
            ORDER BY date ASC, id ASC
        ''', (symbol, market))
        rows = cursor.fetchall()
        
        if not rows:
            return False, "거래 내역이 없습니다."

        # 전략 별칭(Alias)별로 독립적인 상태 관리
        # {alias: {'shares': 0, 'cost': 0}}
        alias_states = {}
        update_count = 0

        for row_id, side, price, qty, fee, total_amount, alias in rows:
            # 별칭이 없는 경우 MANUAL로 처리
            if not alias: alias = "MANUAL"
            if alias not in alias_states:
                alias_states[alias] = {'shares': 0.0, 'cost': 0.0}
            
            state = alias_states[alias]
            realized_profit = 0.0
            realized_profit_rate = 0.0
            avg_price_at_trade = 0.0

            if side == "BUY":
                state['cost'] += total_amount # BUY total_amount = principal + fee
                state['shares'] += qty
                avg_price_at_trade = state['cost'] / state['shares'] if state['shares'] > 0 else price
            else: # SELL
                avg_price_at_trade = state['cost'] / state['shares'] if state['shares'] > 0 else 0.0
                # SELL total_amount = principal - fee - tax (순수 정산금)
                cost_of_sold_shares = avg_price_at_trade * qty
                realized_profit = total_amount - cost_of_sold_shares
                if cost_of_sold_shares > 0:
                    realized_profit_rate = (realized_profit / cost_of_sold_shares) * 100
                
                # 상태 업데이트
                state['shares'] = max(0, state['shares'] - qty)
                if state['shares'] == 0:
                    state['cost'] = 0.0
                else:
                    # 비용 비례 차감
                    state['cost'] = max(0, state['cost'] - cost_of_sold_shares)
            
            # DB 업데이트 (계산된 평단가 및 이익 저장)
            cursor.execute('''
                UPDATE trade_history 
                SET avg_price=?, realized_profit=?, realized_profit_rate=?
                WHERE id=?
            ''', (avg_price_at_trade, realized_profit, realized_profit_rate, row_id))
            update_count += 1

        conn.commit()
        conn.close()
        logger.info(f"✅ [{symbol}] 실현 손익 재계산 완료: {update_count}건 업데이트")
        return True, f"'{symbol}' 종목의 {update_count}건 거래 내역에 대해 손익 재계산을 완료했습니다."
    except Exception as e:
        logger.error(f"Profit recalculation failed: {e}")
        return False, str(e)

# --- 시스템 설정(Config) 관련 함수 ---

def set_config(key: str, value: str):
    """시스템 설정을 DB에 저장 (문자열로 저장)"""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO system_config (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        ''', (key, str(value)))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to set config {key}: {e}")

def get_config(key: str, default: str = None) -> str:
    """시스템 설정 값 조회"""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM system_config WHERE key=?', (key,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else default
    except Exception as e:
        logger.error(f"Failed to get config {key}: {e}")
        return default

if __name__ == "__main__":
    init_db()
