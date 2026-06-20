import os
import math
import time
import json
import requests
from dotenv import load_dotenv, dotenv_values
import csv
import pytz
import urllib3
import logging
from datetime import datetime, time as dtime
import threading
from pytz import timezone
from dataclasses import dataclass, asdict
from typing import Optional, List, Tuple, Literal

from core.database import save_state_db, load_state_db, log_trade_db, log_order_db, init_db, finish_strategy_db, get_next_strategy_name
from core.database import save_api_token_db, load_api_token_db, delete_api_token_db # ADDED: delete 추가
from .utils import format_symbol_display, get_ticker_name # ADDED: 종목명 표시 유틸리티 임포트
# ==============================================================================
# TOSS Holiday API 연동부
# ==============================================================================
# SSL 인증서 검증 무시 설정
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
# 로거 설정
logger = logging.getLogger(__name__)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 토큰 발급 실패 시 쿨다운 타임스탬프 (계좌번호별 통합 관리)
_token_fail_count = {} # account_no -> consecutive fail count
_last_token_fail_times = {}
_token_invalidation_lock = threading.Lock() # Global lock for token invalidation
_token_fetch_lock = threading.Lock() # ADDED: 실제 API 발급 요청을 하나로 제한하는 락
_last_token_invalidated_time = {} # market -> last time token was invalidated

from core.notifier import send_telegram_message # MOVED: 순환 참조 방지를 위해 아래로 이동

# KIS/TOSS 주문 유형 코드 매핑 (대시보드 공용)
ORDER_TYPE_MAP = {
    "00": "지정가",
    "01": "시장가",
    "02": "조건부지정가",
    "03": "최유리지정가",
    "04": "최우선지정가",
    "05": "장전 시간외",
    "06": "장후 시간외",
    "07": "시간외 단일가",
    "31": "MOO (장개시시장가)",
    "32": "LOO (장개시지정가)",
    "33": "MOC (장마감시장가)",
    "34": "LOC (장마감지정가)",
    "35": "TWAP (시간가중평균)",
    "36": "VWAP (거래량가중평균)",
}

def fetch_kr_holiday(target_date: str = None) -> bool:
    """
    국내 휴장일 여부를 조회합니다.
    target_date: YYYYMMDD 형식. None이면 오늘 날짜(KST).
    Returns: True if market is open, False otherwise.
    """
    try:
        from core.brokers.toss import TossBroker
        broker = TossBroker(market="KR")
        
        kr_tz = timezone('Asia/Seoul')
        if target_date is None:
            target_date_dt = datetime.now(kr_tz)
        else:
            target_date_dt = datetime.strptime(target_date, "%Y%m%d")
            
        date_str = target_date_dt.strftime("%Y-%m-%d")
        
        data = broker._call_api("GET", "/api/v1/market-calendar/KR", params={"date": date_str})
        today_cal = data.get("result", {}).get("today", {})
        
        is_open = today_cal.get("integrated") is not None
        return is_open
    except Exception as e:
        logger.error(f"[KR Holiday] 토스 API 조회 중 오류 발생: {e}")
    return True

def fetch_us_holiday() -> bool:
    """
    미국 휴장일 여부를 조회합니다.
    Returns: True if market is open, False otherwise.
    """
    try:
        from core.brokers.toss import TossBroker
        broker = TossBroker(market="US")
        
        ny_tz = timezone('America/New_York')
        date_str = datetime.now(ny_tz).strftime("%Y-%m-%d")
        
        data = broker._call_api("GET", "/api/v1/market-calendar/US", params={"date": date_str})
        today_cal = data.get("result", {}).get("today", {})
        
        is_open = today_cal.get("regularMarket") is not None
        return is_open
    except Exception as e:
        logger.error(f"[US Holiday] 토스 API 조회 중 오류 발생: {e}")
    return True

# ==============================================================================
# Broker 추상화 및 KIS 구현
# ==============================================================================

class Broker:
    def get_price(self, symbol: str) -> float:
        raise NotImplementedError
        
    def get_stock_info(self, symbol: str) -> Optional[dict]:
        """상품 기본 정보 조회 (명칭 등)"""
        raise NotImplementedError

    def get_previous_close(self, symbol: str) -> float:
        raise NotImplementedError

    def get_current_high(self, symbol: str) -> float:
        raise NotImplementedError

    def get_current_low(self, symbol: str) -> float:
        raise NotImplementedError
    
    def get_account_equity(self, symbol: str) -> Optional[Tuple[float, float, float]]:
        """returns (shares, avg_price, eval_amt) or None on failure"""
        raise NotImplementedError
    
    def get_cash_pool(self) -> float:
        raise NotImplementedError
    
    def get_cumulative_buy_amount(self, symbol: str) -> float:
        """Returns the current total cost basis of the holding."""
        raise NotImplementedError

    def place_order(self, symbol: str, price: float, qty: float, order_type: Literal["BUY", "SELL"], price_type: str = "00", strategy: str = "MANUAL") -> bool:
        raise NotImplementedError

    def fetch_open_orders(self, symbol: str) -> List[dict]:
        """특정 종목의 미체결 주문 내역을 반환합니다."""
        raise NotImplementedError

    def fetch_execution_history(self, symbol: str, start_date: str, end_date: str) -> List[dict]:
        """특정 기간 동안의 체결 내역을 조회합니다."""
        raise NotImplementedError

    def adjust_price_by_tick(self, symbol: str, price: float, order_type: Literal["BUY", "SELL"]) -> float:
        """기본적으로는 소수점 2자리 반올림/올림/내림 처리 (시장별 오버라이드 가능)"""
        if order_type == "BUY":
            # 매수 시 호가 유리하게 올림 (미국 소수점 2자리 기준)
            return math.ceil(price * 100) / 100.0
        else:
            # 매도 시 보수적으로 내림
            return math.floor(price * 100) / 100.0


# ==============================================================================
# 전략별 설정(Config) 및 상태(State) 데이터 클래스
# ==============================================================================

@dataclass
class BaseConfig:
    symbol: str
    strategy_type: str
    save_path: str = "state.json"
    use_db: bool = False  # DB 사용 여부 추가
    market: str = "US"    # 시장 정보 추가
    strategy_name: str = "" # 전략 별칭 추가

@dataclass
class CAConfig(BaseConfig):
    """무한매수법(Cost Averaging) V2.2 설정"""
    strategy_type: str = "CA"
    initial_budget: float = 10000.0 # 시작 원금 (진행률 계산용)
    version: str = "V2.2"           # 전략 버전 (V2.2 또는 V4.0)
    unit_buy_amount: float = 0.0    # 1회 매수액 (0이면 budget/a_default로 자동 계산)
    a_default: int = 40             # 분할 횟수 (a)
    T_default: int = 40             # 기본 설정 회수 (T)
    target_profit_pct: float = 0.10 # 목표 수익률 (10%)
    use_quarter_stop: bool = True   # 쿼터 손절 사용 여부
    fee_rate: float = float(os.getenv("fee_rate", "0.0009")) # 미국 주식 수수료 0.09% 기본값
    trade_history_path: str = "data/trade_history.csv" # 거래 내역 경로
    detailed_trade_history_path: str = "data/detailed_trade_history.csv" # 상세 거래 내역 경로

    def __post_init__(self):
        # 한국 시장은 2배 레버리지가 주력이므로 기본 목표수익률을 7%로 조정
        if self.market == "KR" and self.target_profit_pct == 0.10:
            self.target_profit_pct = 0.07

@dataclass
class VRConfig(BaseConfig):
    """밸류리밸런싱(Value Rebalancing) 설정"""
    strategy_type: str = "VR"
    initial_budget: float = 10000.0
    G: float = 10.0
    band_low_pct: float = 85.0
    band_high_pct: float = 115.0
    periodic_accumulation: float = 250.0
    investment_type: str = "accumulation"

@dataclass
class BaseState:
    symbol: str
    strategy_type: str
    strategy_name: Optional[str] = ""
    market: str = "US"
    is_active: bool = True

@dataclass
class CAState(BaseState):
    """무한매수법 상태"""
    strategy_type: str = "CA"
    version: str = "V2.2"      # 전략 버전 추가
    avg_price: float = 0.0
    total_shares: float = 0.0
    current_turn: float = 0.0  # 현재 진행 회차 (T)
    pool: float = 0.0          # [추가] 사용 가능 예수금 (외화잔고)
    cycle_budget: float = 0.0  # 현재 사이클의 운용 예산
    total_profit: float = 0.0  # 전체 누적 손익
    unit_buy_amount: float = 0.0 # 1회 매수 금액 (상태 저장용)
    a_default: int = 40        # 분할 횟수
    target_profit_pct: float = 0.10 # 목표 수익률
    use_quarter_stop: bool = True   # 쿼터 손절 사용 여부
    reverse_star_price: float = 0.0 # 리버스 모드용 별지점 (5일 평균)

    
    # 모드 관리
    mode: str = "NORMAL"       # NORMAL, QUARTER, REVERSE
    quarter_turn: int = 0      # 쿼터손절 모드 진행 회차 (0~10)

    # 장중 매수 관리 (일일 초기화 필요)
    daily_bought_amount: float = 0.0  # 당일 장중 매수 누적액
    last_intraday_level: int = 0      # 당일 장중 매수 단계 (1: -5%, 2: -10% ...)
    last_execution_price: float = 0.0 # 마지막 장중 체결가 (동적 기준점)
    current_price: float = 0.0        # [추가] 실시간 현재가 캐시
    last_check_date: str = ""         # 날짜 변경 감지용
    
    pending_cycle_transition: bool = False # [ADD] 차수 전환 대기 플래그
    day_start_turn: float = 0.0            # [ADD] 당일 시작 회차 고정값
    day_start_star_pct: float = 0.0        # [ADD] 당일 시작 별값 고정값
    
@dataclass
class VRState(BaseState):
    """밸류리밸런싱 상태"""
    strategy_type: str = "VR"
    V: float = 0.0
    pool: float = 0.0
    last_E: float = 0.0
    periodic_accumulation: float = 0.0
    current_price: float = 0.0        # [추가] 실시간 현재가 캐시
    initial_budget: float = 0.0
    G: float = 10.0                   # 기울기 설정값
    band_low_pct: float = 85.0        # 하단 밴드
    band_high_pct: float = 115.0       # 상단 밴드
    investment_type: str = "accumulation" # 투자 방식
    
    # 모드 관리
    mode: str = 'BOOTSTRAP'
    bootstrap_day_count: int = 0
    
    # 사이클별 고정값
    cycle_V: float = 0.0
    cycle_start_pool: float = 0.0

# ==============================================================================
# 상태 저장/로드 유틸리티
# ==============================================================================

def load_state(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(state: BaseState, path: str):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(asdict(state), f, ensure_ascii=False, indent=2)

# ==============================================================================
# 전략 실행 엔진 (Strategy Engines)
# ==============================================================================

_ca_intraday_lock = threading.Lock() # ADDED: 장중 체크 동시성 제어용 락

class CostAveragingEngine:
    """무한매수법(CA) V2.2 실행 로직 (backtest_v2_2.py 기반 재구성)"""
    def __init__(self, config: CAConfig, broker: Optional[Broker] = None):
        self.config = config
        if broker is None:
            raise ValueError("Broker instance must be provided to the CostAveragingEngine.")
        self.broker = broker
        self.state = self._initialize_state()

        # State에 저장된 설정이 있으면 Config 덮어씌움 (DB 우선 반영)
        if self.state.unit_buy_amount > 0:
            self.config.unit_buy_amount = self.state.unit_buy_amount
        if hasattr(self.state, 'a_default') and self.state.a_default > 0:
            self.config.a_default = self.state.a_default
        if hasattr(self.state, 'target_profit_pct') and self.state.target_profit_pct > 0:
            self.config.target_profit_pct = self.state.target_profit_pct
        if hasattr(self.state, 'use_quarter_stop'):
            self.config.use_quarter_stop = self.state.use_quarter_stop
        if hasattr(self.state, 'version'):
            self.config.version = self.state.version

        # Config 자동 보정: unit_buy_amount가 0이면 budget / a_default
        if self.config.unit_buy_amount <= 0 and self.config.a_default > 0:
            budget = self.state.cycle_budget if self.state.cycle_budget > 0 else self.config.initial_budget
            self.config.unit_buy_amount = budget / self.config.a_default

        # DB 초기화 (DB 모드일 경우)
        if self.config.use_db:
            init_db()

        # 거래 기록 파일 초기화
        if not self.config.use_db:
            self._init_trade_history()
            self._init_detailed_trade_history()
        self.planned_orders = []  # Preview 모드용 주문 저장소
        self.current_order_filter = None # "LIMIT_ONLY" or "LOC_ONLY"
        self.open_orders_pool = [] # 미체결 주문 비교용 풀

    def _is_already_ordered(self, side: str, price: float, qty: int) -> bool:
        """동일한 조건의 미체결 주문이 이미 존재하는지 확인 및 소모 (Preview용)"""
        if not self.open_orders_pool:
            return False
        for i, oo in enumerate(self.open_orders_pool):
            # KIS US/KR 필드 통합 대응
            oo_price = float(oo.get('ft_ord_unpr3') or oo.get('ord_unpr') or oo.get('unpr') or oo.get('pdno_unpr') or 0)
            oo_qty = float(oo.get('nccs_qty') or oo.get('ft_ord_qty3') or oo.get('ord_qty') or 0)
            side_raw = str(oo.get('sll_buy_dvsn_cd', ''))
            oo_side = "SELL" if side_raw in ['01', '1', '매도'] else "BUY"
            
            # 가격 오차 0.01 이내, 수량 일치, 구분 일치 시 동일 주문으로 간주
            if oo_side == side and int(oo_qty) == int(qty) and abs(oo_price - price) < 0.01:
                self.open_orders_pool.pop(i) # 계획에서 중복 처리 방지를 위해 제거
                return True
        return False

    def _round_price(self, price: float, side: Literal["BUY", "SELL"]) -> float:
        """주문 가격 소수점 처리: 매수(올림), 매도(버림)"""
        # [수정] 브로커의 틱 단위 보정 기능을 우선 사용하도록 통합
        return self.broker.adjust_price_by_tick(self.config.symbol, price, side)
        
    def _init_trade_history(self):
        path = self.config.trade_history_path
        if not os.path.exists(path):
            # data 폴더가 없을 경우 생성
            data_dir = os.path.dirname(path)
            if not os.path.exists(data_dir):
                os.makedirs(data_dir)
            with open(path, 'w', encoding='utf-8') as f:
                f.write("date,ticker,profit,total_realized_profit,cash\n")

    def _init_detailed_trade_history(self):
        path = self.config.detailed_trade_history_path
        if not os.path.exists(path):
            data_dir = os.path.dirname(path)
            if not os.path.exists(data_dir):
                os.makedirs(data_dir)
            with open(path, 'w', encoding='utf-8', newline='') as f:
                writer = csv.writer(f)
                # 매매일자 / 매수,매도 구분 / T 회수 / 체결가 / 수량 / 수수료 / 누적매입금액(수수료포함) / 비고
                writer.writerow(["Date", "Ticker", "Type", "Turn", "Price", "Qty", "Fee", "TotalAmount", "Note"])

    def _log_cycle_end(self, date, cycle_profit: float):
        """사이클 종료 시 손익 기록"""
        self.state.total_profit += cycle_profit
        date_str = date.strftime("%Y-%m-%d %H:%M:%S") if isinstance(date, datetime) else str(date)
        cash = self.broker.get_cash_pool()
        with open(self.config.trade_history_path, 'a', encoding='utf-8') as f:
            f.write(f"{date_str},{self.config.symbol},{cycle_profit:.2f},{self.state.total_profit:.2f},{cash:.2f}\n")

    def _log_detailed_trade(self, date_str, type_, price, qty, fee, turn, total_amt, note=""):
        """상세 거래 내역 기록"""
        if self.config.use_db:
            realized_profit = 0.0
            realized_profit_rate = 0.0
            if type_ == "SELL" and self.state.avg_price > 0:
                cost_basis = self.state.avg_price * qty * 1.0025
                realized_profit = total_amt - cost_basis
                if cost_basis > 0:
                    realized_profit_rate = (realized_profit / cost_basis) * 100
                    
            log_trade_db(date_str, self.config.symbol, "CA", type_, price, qty, fee, total_amt, turn, note, 
                         strategy_name=self.config.strategy_name, market=self.config.market,
                         avg_price=self.state.avg_price, realized_profit=realized_profit, realized_profit_rate=realized_profit_rate)
            return

        # 파일 로깅 (백테스트 등)
        try:
            with open(self.config.detailed_trade_history_path, 'a', encoding='utf-8', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    date_str, self.config.symbol, type_, f"{turn:.1f}", 
                    f"{price:.2f}", f"{qty}", f"{fee:.2f}", f"{total_amt:.2f}", note
                ])
        except Exception as e:
            logger.error(f"Failed to log detailed trade: {e}")

    def _initialize_state(self) -> CAState:
        if self.config.use_db:
            state_data = load_state_db(self.config.symbol, self.config.strategy_type, market=self.config.market, strategy_name=self.config.strategy_name)
        else:
            state_data = load_state(self.config.save_path)

        if state_data and state_data.get("strategy_type") == "CA":
            # 이전 버전 state 파일 호환
            if 'total_profit' not in state_data:
                state_data['total_profit'] = 0.0
            if 'unit_buy_amount' not in state_data:
                state_data['unit_buy_amount'] = self.config.unit_buy_amount
            if 'cumulative_buy_amount' in state_data:
                del state_data['cumulative_buy_amount'] # 더 이상 엔진에서 관리하지 않음
            return CAState(**state_data)
        
        shares, avg_price, _ = self.broker.get_account_equity(self.config.symbol)
        
        return CAState(
            symbol=self.config.symbol,
            strategy_type="CA",
            strategy_name="",
            market=self.config.market,
            avg_price=avg_price,
            pool=self.broker.get_cash_pool(), # [추가] 초기 예수금 로드
            total_shares=shares,
            current_turn=0.0,
            cycle_budget=self.config.initial_budget,
            total_profit=0.0,
            unit_buy_amount=self.config.unit_buy_amount,
            daily_bought_amount=0.0,
            last_intraday_level=0,
            last_check_date="",
            mode="NORMAL"
        )

    def _update_current_turn_from_broker(self, shares=None, avg_price=None):
        """브로커 정보를 기반으로 회차(T)를 동기화합니다. 정보가 주어지면 API 호출을 생략합니다."""
        if shares is not None and avg_price is not None:
            cumulative_buy_amount = shares * avg_price
        else:
            cumulative_buy_amount = self.broker.get_cumulative_buy_amount(self.config.symbol) or 0.0
            
        if self.config.unit_buy_amount > 0:
            raw_turn = cumulative_buy_amount / self.config.unit_buy_amount
            self.state.current_turn = raw_turn # [V4.0 원칙] 반올림/올림 없음

    def _calculate_star_percent(self, turn: float) -> float:
        """별값(%) 계산: 목표수익률 - (T/2 * (T_default/a_default)) %"""
        t = max(0, turn)
        term = (t / 2.0) * (self.config.T_default / self.config.a_default)
        # [무매 공통 규칙] 별값(%)의 소수점 셋째 자리에서 올림 (0.07123 -> 0.0713)
        raw_star = self.config.target_profit_pct - (term / 100.0)
        return math.ceil(raw_star * 10000) / 10000.0

    def _buy(self, amount_to_invest: float, price: float, desc: str, turn: float = None, price_type: str = "00", preview: bool = False) -> bool:
        """매수 실행 및 상태 업데이트"""
        # [KR Market Split Logic]
        if self.config.market == "KR" and self.current_order_filter == "SELL_LIMIT_ONLY":
            return False # 오전장: 매수 금지
            
        if self.config.market == "KR" and self.current_order_filter == "LOC_ONLY":
            is_loc_type = "LOC" in desc or "Star%" in desc or price_type == "34"
            if not is_loc_type:
                return False

        # [수정] 거래소 전체 현금이 아닌 전략에 할당된 POOL(self.state.pool) 기준 체크
        if self.state.pool < amount_to_invest or price <= 0:
            logger.info(f"⚠️ [매수 스킵] {self.config.symbol}: 잔고부족(보유:{self.broker.get_cash_pool():.2f} < 필요:{amount_to_invest:.2f}) 또는 가격오류({price})")
            return False

        # [Multi-Market] KR 시장은 LOC/MOC 미지원 -> 지정가(00) 강제
        if self.config.market == "KR" and price_type in ["34", "33", "32", "31"]:
            price_type = "00"

        # 가격 호가 단위 보정
        price = self._round_price(price, "BUY")

        qty = int(amount_to_invest / price)

        # 실전 모드에서 중복 주문 체크
        if not preview and self.open_orders_pool:
            if self._is_already_ordered("BUY", price, qty):
                logger.info(f"-> [{desc}] 이미 동일한 매수 주문이 존재합니다. (건너뜀)")
                return True

        # [개선] 0.5T 금액이 주가보다 작더라도 Pool이 충분하면 최소 1주는 매수 계획에 포함
        if qty <= 0 and amount_to_invest > 0 and self.state.pool >= price:
            qty = 1

        if preview:
            if qty > 0:
                # 이미 제출된 주문은 계획에서 제외
                if self._is_already_ordered("BUY", price, qty):
                    logger.debug(f"[Preview] 이미 제출된 매수 주문 제외: {qty}주 @ {price}")
                else:
                    self.planned_orders.append({
                        "strategy": "CA", "side": "BUY", "symbol": self.config.symbol, 
                        "qty": int(qty), "price": price, "type": price_type, "desc": desc
                    })
            return True
        
        if qty <= 0:
            return False
        
        actual_cost = qty * price
        fee = actual_cost * self.config.fee_rate
        
        if self.broker.place_order(self.config.symbol, price, qty, "BUY", price_type=price_type, strategy="CA"):
            # 성공 시 로그 기록
            current_t = turn if turn is not None else self.state.current_turn
            
            # [TPS 최적화] 브로커 재조회 대신 현재 상태와 실제 매수액 합산
            cum_amt = self.state.total_shares * self.state.avg_price
            est_cum_amt = cum_amt + actual_cost
            
            # 실전 모드(use_db)에서는 웹소켓 체결 시 기록하므로, 백테스트 모드에서만 기록
            if not self.config.use_db:
                self._log_detailed_trade(time.strftime("%Y-%m-%d %H:%M:%S"), "BUY", price, qty, fee, current_t, est_cum_amt, desc)
                
            # 시장별 통화 기호 및 주문 유형 적용
            cur_sym = "₩" if self.config.market == "KR" else "$"
            def fmt(v):
                if self.config.market == "KR": return f"{int(v):,}"
                return f"{v:,.4f}"
            # [수정] 웹소켓 알림과 중복되므로 INFO 대신 DEBUG로 변경
            logger.debug(f"-> [{desc}] 매수 주문 전송 완료: {qty}주 @ {cur_sym}{fmt(price)}")
            return True
        return False # 주문 실패

    def _sell(self, qty_to_sell: float, price: float, desc: str, price_type: str = "00", preview: bool = False) -> float:
        """매도 실행 및 손익 계산"""
        # [KR Market Split Logic]
        if self.config.market == "KR" and self.current_order_filter:
            is_loc_type = "LOC" in desc or "Star%" in desc
            if self.current_order_filter == "LIMIT_ONLY" and is_loc_type:
                return False
            if self.current_order_filter == "LOC_ONLY" and not is_loc_type:
                return False

        if qty_to_sell <= 0 or self.state.total_shares <= 0:
            logger.debug(f"[DEBUG] 매도 스킵: 수량 0 (요청:{qty_to_sell}, 보유:{self.state.total_shares})")
            return 0.0
        
        real_qty = min(qty_to_sell, self.state.total_shares)
        
        # [Multi-Market] KR 시장은 LOC/MOC 미지원 -> 지정가(00) 강제
        if self.config.market == "KR" and price_type in ["34", "33", "32", "31"]:
            price_type = "00"

        # 가격 호가 단위 보정
        price = self._round_price(price, "SELL")
        
        # 실전 모드에서 중복 주문 체크
        if not preview and self.open_orders_pool:
            if self._is_already_ordered("SELL", price, int(real_qty)):
                logger.info(f"-> [{desc}] 이미 동일한 매도 주문이 존재합니다. (건너뜀)")
                return 0.0

        if preview:
            # 이미 제출된 주문은 계획에서 제외
            if self._is_already_ordered("SELL", price, int(real_qty)):
                logger.debug(f"[Preview] 이미 제출된 매도 주문 제외: {int(real_qty)}주 @ {price}")
                return 0.0

            self.planned_orders.append({
                "strategy": "CA", "side": "SELL", "symbol": self.config.symbol, 
                "qty": int(real_qty), "price": price, "type": price_type, "desc": desc
            })
            return 0.0 # Preview에서는 수익 계산 생략
        
        cost_basis = self.state.avg_price * real_qty
        proceeds = real_qty * price
        fee = proceeds * self.config.fee_rate
        net_proceeds = proceeds - fee
        profit = net_proceeds - cost_basis
        
        if self.broker.place_order(self.config.symbol, price, real_qty, "SELL", price_type=price_type, strategy="CA"):
            # [TPS 최적화]
            cum_amt = self.state.total_shares * self.state.avg_price
            est_cum_amt = max(0, cum_amt - cost_basis)
            
            # 실전 모드(use_db)에서는 웹소켓 체결 시 기록하므로, 백테스트 모드에서만 기록
            if not self.config.use_db:
                self._log_detailed_trade(time.strftime("%Y-%m-%d %H:%M:%S"), "SELL", price, real_qty, fee, self.state.current_turn, est_cum_amt, desc)
                
            # 시장별 통화 기호 적용
            cur_sym = "₩" if self.config.market == "KR" else "$"
            def fmt(v): # type: ignore
                if self.config.market == "KR": return f"{int(v):,}"
                return f"{v:,.2f}"
            
            logger.debug(f"-> [{desc}] 매도 주문 전송 완료: {real_qty:.2f}주 @ {cur_sym}{fmt(price)}")
            
        return profit

    def run_intraday_check(self, current_price: float = None, prev_close: float = None):
        """
        장중 주가 확인 및 추가 매수 로직 (동적 기준점 적용)
        조건: 기준가 대비 -3% 하락 시마다 1주 매수-->0.5T 분량(체결 시 해당 가격이 새 기준가)
        """
        # [FIX] 동시성 제어를 위해 락 획득
        with _ca_intraday_lock:
            # 시장별 타임존을 기준으로 날짜를 확인하여 초기화
            market_tz = pytz.timezone('Asia/Seoul') if self.config.market == "KR" else pytz.timezone('America/New_York')
            now_dt = datetime.now(market_tz)
            today_str = now_dt.strftime("%Y-%m-%d")

            if self.config.market == "KR":
                reg_open, reg_close = dtime(9, 0), dtime(15, 30)
            else:
                reg_open, reg_close = dtime(9, 30), dtime(16, 0)
            
            if not (reg_open <= now_dt.time() <= reg_close):
                return

            if self.state.last_check_date != today_str:
                self.state.daily_bought_amount = 0.0
                self.state.last_intraday_level = 0
                self.state.last_execution_price = 0.0
                self.state.last_check_date = today_str
                if self.config.use_db:
                    save_state_db(self.state, market=self.state.market, strategy_name=self.state.strategy_name, only_if_exists=True)
                else:
                    save_state(self.state, self.config.save_path)
                logger.info(f"[{today_str}] {self.config.symbol} 장중 매수 상태 초기화 완료")

            if getattr(self.state, 'pending_cycle_transition', False):
                return

            if self.state.mode == "REVERSE":
                return

            if current_price is None:
                current_price = self.broker.get_price(self.config.symbol)
                
            if current_price > 0:
                self.state.current_price = current_price

            if prev_close is None:
                try:
                    prev_close = self.broker.get_previous_close(self.config.symbol)
                except Exception as e:
                    logger.debug(f"[{self.config.market}] 전일종가 조회 실패: {e}")
                    prev_close = 0

            if current_price <= 0: return
            effective_prev_close = prev_close if prev_close > 0 else self.state.avg_price
            if effective_prev_close <= 0: return

        # 로직상 기준가: 마지막 체결가가 있으면 그것을 쓰고, 없으면 전일 종가를 기준점으로 잡음
        trigger_base = float(self.state.last_execution_price) if float(self.state.last_execution_price) > 0 else float(effective_prev_close)
        if trigger_base <= 0: return

        trigger_drop_rate = (current_price - trigger_base) / trigger_base
        display_drop_rate = (current_price - prev_close) / prev_close

        # [설정 확인] 일일 최대 한도 2T, 회당 매수 0.5T
        max_daily_buy = self.config.unit_buy_amount * 2.0 
        buy_amount = self.config.unit_buy_amount * 0.5

        # [수정] 로그 폭증 방지: 기준점 대비 -3% 하락했더라도 일일 한도 초과 시 로직을 조기 종료하여 로그 비대화 방지
        if trigger_drop_rate <= -0.03:
            if self.state.daily_bought_amount + buy_amount > max_daily_buy:
                # 이미 한도에 도달했다면 로깅 없이 즉시 반환
                return

            # [수정] 시장별 통화 기호 및 포맷팅 정의
            cur_sym = "₩" if self.config.market == "KR" else "$"
            def fmt(v):
                if self.config.market == "KR": return f"{int(v):,}"
                return f"{v:,.2f}"

            logger.info(f"🚨 [장중 급락 감지] {self.config.symbol} 현재가({cur_sym}{fmt(current_price)})가 기준가({cur_sym}{fmt(trigger_base)}) 대비 {trigger_drop_rate*100:.2f}% 하락")
            
            # 지정가로 0.5T 분량 매수 (내부적으로 수량 계산)
            if self._buy(buy_amount, current_price, "(장중) -3% 급락 매수", price_type="00"):
                symbol_display = format_symbol_display(self.config.symbol, self.config.market)

                # 장중 급락 매수 요약 정보 텔레그램 발송
                notif_msg = (
                    f"📉 <b>[장중 급락 매수 실행]</b> {symbol_display}\n"
                    f"전일비 하락률: <b>{display_drop_rate*100:.2f}%</b>\n"
                    f"계좌 평단가: <b>{cur_sym}{fmt(self.state.avg_price)}</b>\n"
                    f"매수 단가: {cur_sym}{fmt(current_price)}\n"
                    f"새 기준가: {cur_sym}{fmt(current_price)} (다음 -3% 감시)"
                )
                if trigger_base != effective_prev_close:
                    notif_msg += f"\n기준점 대비 하락률: <b>{trigger_drop_rate*100:.2f}%</b> (기준가: {cur_sym}{fmt(trigger_base)})"
                if self.config.use_db:
                    send_telegram_message(notif_msg)

                # [FIX] 중복 업데이트 제거 및 정확한 한도 차감
                self.state.daily_bought_amount += buy_amount
                self.state.last_execution_price = current_price # 체결가를 새로운 기준점으로 설정
                if self.config.use_db:
                    save_state_db(self.state, market=self.state.market, strategy_name=self.state.strategy_name, only_if_exists=True)
                else:
                    save_state(self.state, self.config.save_path)

    def run_cycle(self, date, check_existing_orders: bool = False, preview: bool = False, order_filter: Literal["LIMIT_ONLY", "LOC_ONLY", None] = None, transition_only: bool = False):
        """무한매수법 1회 사이클 실행 (일별 로직)"""
        filter_msg = f" ({order_filter})" if order_filter else ""
        logger.info(f"\n--- [{date.strftime('%Y-%m-%d')}] 무한매수법(CA) 사이클{filter_msg} ---")
        
        self.current_order_filter = order_filter

        # [V4.0 전용] 유동적 1회 매수금 계산: 잔금 / (a - T)
        if self.config.version == "V4.0" and self.state.mode != "REVERSE":
            available_slots = self.config.a_default - self.state.current_turn
            if available_slots > 0:
                # [중요] 1회 매수금을 매일 재계산하여 유동성 부여
                self.config.unit_buy_amount = self.state.pool / available_slots
                logger.info(f"📍 [V4.0] 유동 매수액 재계산: {self.config.unit_buy_amount:,.2f} (남은회차: {available_slots:.2f})")
            if self.state.current_turn > (self.config.a_default - 1):
                if self.state.mode != "REVERSE" and not preview:
                    send_telegram_message(f"🚨 <b>[CA V4.0 리버스 모드 가동]</b> {format_symbol_display(self.config.symbol, self.config.market)}\n예산 소진(T > {self.config.a_default - 1})에 따라 리버스 모드로 전환합니다.")
                self.state.mode = "REVERSE"

        # [ADD] SELL_LIMIT_ONLY 필터인 경우 매수 로직은 건너뜁니다.
        if order_filter == "SELL_LIMIT_ONLY":
            logger.info(f"[{self.config.symbol}] SELL_LIMIT_ONLY 필터 적용. 매수 로직은 건너뜁니다.")

        # 시작 시 중복 주문 체크 (Preview일때는 무시하거나 체크만 로그)
        if check_existing_orders:
            self.open_orders_pool = self.broker.fetch_open_orders(self.config.symbol)
            if self.open_orders_pool:
                logger.info(f"[{self.config.symbol}] 미체결 주문 {len(self.open_orders_pool)}건 감지. 중복 주문은 제외하고 진행합니다.")
        
        # 1. 시세 및 잔고 조회
        current_price = self.broker.get_price(self.config.symbol)
        
        # [수정] 브로커 시세 조회 실패 시 DB에 저장된 최신 웹소켓 가격 사용 (SOXL 등 거래소 코드 이슈 대응)
        if current_price <= 0:
            if self.state.current_price > 0:
                current_price = self.state.current_price
                logger.info(f"[{self.config.symbol}] 브로커 시세 조회 실패. DB 캐시 가격 사용: ${current_price:.2f}")
            else:
                logger.warning(f"[{self.config.symbol}] 현재가 조회 최종 실패.")
                self._save_and_finish(current_price=0.0)
                return self.planned_orders if preview else None
                
        # [FIX] 전일 장중 전량 매도되어 차수 전환이 필요한 경우 처리 (시세 확보 후로 이동하여 NameError 방지)
        # 1. 차수 전환 대기 상태 우선 처리
        if getattr(self.state, 'pending_cycle_transition', False):
            logger.info(f"🚩 [{self.config.symbol}] 전일 전량 매도 확인됨. 차수 전환 및 신규 매수를 시작합니다.")
            if self.config.market == "KR" and order_filter == "SELL_LIMIT_ONLY":
                return None # 한국장은 오전 10시에 별도 처리
            return self._handle_cycle_finish_and_restart(current_price, date, preview)

        # 2. 실시간 잔고(API) 조회
        equity_data = self.broker.get_account_equity(self.config.symbol)
        if equity_data is None or equity_data[0] is None:
            logger.warning(f"[{self.config.symbol}] 잔고 정보 조회 실패. 사이클 처리를 스킵합니다.")
            return self.planned_orders if preview else None
            
        shares, avg_price, eval_amt = equity_data
        logger.debug(f"[DEBUG] API 조회 결과: Price=${current_price}, Shares={shares}, Avg=${avg_price}")

        prev_shares = self.state.total_shares
        prev_avg = self.state.avg_price

        # 로컬 상태 즉시 업데이트
        self.state.total_shares = shares
        self.state.avg_price = avg_price

        # 3. 전량 매도 완료 감지 및 차수 전환 (API 잔고가 0인데 DB에는 기록이 남아있던 경우)
        if shares == 0 and (prev_shares > 0 or prev_avg > 0 or getattr(self.state, 'pending_cycle_transition', False)):
            logger.info(f"🚩 [{self.config.symbol}] 전량 매도 완료 감지 (전략:{self.config.strategy_name}). 사이클 종료 및 차수 전환을 시작합니다.")
            if self.config.market == "KR" and order_filter == "SELL_LIMIT_ONLY":
                return None
            return self._handle_cycle_finish_and_restart(current_price, date, preview)
        
        # 3-1. 리버스 모드 로직 분기
        if self.state.mode == "REVERSE":
            return self._run_reverse_mode_routine(current_price, date, preview)

        # 4. transition_only 모드(10시 점검): 차수 전환이 불필요(잔고 > 0)한 경우 상태 업데이트 후 조용히 종료
        if transition_only:
            self._update_current_turn_from_broker(shares=shares, avg_price=avg_price)
            self.state.day_start_turn = self.state.current_turn
            self.state.day_start_star_pct = self._calculate_star_percent(self.state.current_turn)
            logger.info(f"📍 [Prepare] 당일 기준값 고정 완료: T={self.state.day_start_turn:.1f}")
            # 보유 잔고가 0이 아니면 텔레그램 메시지 생략 (사용자 요청)
            self._save_and_finish(current_price=current_price, silent=(shares > 0), shares=shares, avg_price=avg_price)
            return None        

        # [FIX] shares == 0 이고, 과거 이력이 없는 (avg_price, current_turn 모두 0) 완전 초기 상태일 때만 신규 매수
        # 그렇지 않고 shares == 0 이지만 과거 이력이 있다면 (즉, 전량 매도 후 차수 전환이 필요한데 안된 경우)
        # _handle_cycle_finish_and_restart를 호출하도록 변경
        if shares == 0 and self.state.avg_price == 0 and self.state.current_turn == 0:
            logger.info(f"[{self.config.symbol}] 보유 수량 0, 과거 이력 없음 -> 신규 사이클 시작 매수")
            if self.config.market == "KR" and order_filter == "SELL_LIMIT_ONLY":
                return None
            buy_offset = 10 if self.config.market == "KR" else 0.01
            self._buy(self.config.unit_buy_amount, current_price + buy_offset, f"{self.config.strategy_name} 시작 매수", price_type="00", preview=preview)
            if not preview: 
                self.state.total_shares = 0 # 상태 동기화
                self._save_and_finish(current_price=current_price, shares=0, avg_price=0)
            return self.planned_orders if preview else None

        # 2. 진행 정보 계산
        cumulative_buy_amount = self.broker.get_cumulative_buy_amount(self.config.symbol)
        progress_rate = 0.0
        if self.state.cycle_budget > 0:
            progress_rate = cumulative_buy_amount / self.state.cycle_budget
        
        # T(회차) 계산 = 실제 누적 매수액 / 1회 매수 계획금액
        if self.config.unit_buy_amount > 0:
            raw_turn = cumulative_buy_amount / self.config.unit_buy_amount
            self.state.current_turn = raw_turn # [V4.0 원칙] 반올림/올림 없음

        # [ADD] 당일 로직 수행을 위한 T값 및 Star% 스냅샷 고정
        if not preview:
            self.state.day_start_turn = self.state.current_turn
            self.state.day_start_star_pct = self._calculate_star_percent(self.state.current_turn)
            logger.info(f"📍 당일 기준값 고정: T={self.state.day_start_turn:.1f}, Star%={self.state.day_start_star_pct*100:.2f}%")

        # 로직에서는 고정된 day_start_turn을 사용하도록 유도
        display_t = self.state.day_start_turn if self.state.day_start_turn > 0 else self.state.current_turn
        logger.info(f"상태: {self.state.mode} | T={display_t:.1f} | 진행률={progress_rate*100:.1f}%")

        cur_sym = "₩" if self.config.market == "KR" else "$"
        def fmt(v):
            if self.config.market == "KR": return f"{int(v):,}"
            return f"{v:,.2f}"
        logger.info(f"평단: {cur_sym}{fmt(self.state.avg_price)} | 현재가: {cur_sym}{fmt(current_price)} | 보유량: {self.state.total_shares:.2f}")

        # 3. 매도 조건 체크 및 실행
        sold_all = self._check_and_sell(current_price, date, preview=preview)
        
        # [자동재진입] 전량 매도 시 _check_and_sell 내부에서 이미 새 전략 생성 및 1회차 매수를 수행함
        if sold_all:
            if not preview:
                self._save_and_finish(current_price=current_price, shares=0, avg_price=0)
                return None
            progress_rate = 0.0 # Preview일 때는 계획 주문 합산을 위해 진행률 초기화

        # 4. 매수 로직 실행
        self._buy_routine(current_price, progress_rate, preview=preview, use_snapshot=True)
        
        if preview:
            logger.info(f"[Preview] 생성된 주문 계획: {len(self.planned_orders)}건")
            return self.planned_orders

        self._save_and_finish(current_price=current_price, shares=shares, avg_price=avg_price)
        return None

    def _handle_cycle_finish_and_restart(self, current_price: float, date, preview: bool = False, profit: float = 0.0) -> Optional[List[dict]]:
        """사이클을 종료하고 다음 차수로 전환하는 공통 로직"""
        if preview:
            next_alias = f"{self.config.strategy_name}_NEXT" 
            buy_offset = 10 if self.config.market == "KR" else 0.01
            self.planned_orders.append({
                "strategy": "CA", "side": "BUY", "symbol": self.config.symbol,
                "qty": int(self.config.unit_buy_amount / (current_price + buy_offset)),
                "price": self._round_price(current_price + buy_offset, "BUY"), 
                "type": "00", "desc": f"🚀 {next_alias} 시작 매수 (자동재진입)"
            })
            return self.planned_orders

        # 1. 기존 전략 종료 처리
        self._log_cycle_end(date, profit)
        old_alias = self.config.strategy_name
        finish_strategy_db(self.config.symbol, "CA", self.config.market, old_alias)
        
        # 2. 다음 차수 생성
        next_alias = get_next_strategy_name(self.config.symbol, "CA", self.config.market)
        
        # [수정] 1회 매수금(unit_buy_amount) 보호 로직
        # 사용자가 설정한 고정 매수금이 있다면(config.unit_buy_amount > 0) 이를 최우선으로 사용합니다.
        if self.config.unit_buy_amount > 0:
            new_unit_buy = self.config.unit_buy_amount
            new_budget = new_unit_buy * 40 # 기본 40회차 기준으로 예산 설정
        else:
            new_budget = self.state.pool if self.state.pool > 0 else self.config.initial_budget
            new_unit_buy = new_budget / 40 # 새 사이클은 항상 기본 40분할로 시작
        
        new_state = CAState(
            symbol=self.config.symbol,
            strategy_type="CA",
            version=self.config.version,
            strategy_name=next_alias,
            market=self.config.market,
            cycle_budget=new_budget,
            pool=new_budget,
            unit_buy_amount=new_unit_buy,
            a_default=40, # 새 사이클 시작 시 분할 횟수 초기화
            total_profit=self.state.total_profit,
            mode="NORMAL",
            pending_cycle_transition=False, # 플래그 초기화
            current_turn=0.0
        )
        
        # 3. 새 전략 저장 및 엔진 상태 전환
        save_state_db(new_state, market=self.config.market, strategy_name=next_alias)
        self.config.strategy_name = next_alias
        self.state = new_state
        
        # 4. 자동 재진입 매수 (현재가 + 오프셋 지정가)
        buy_offset = 10 if self.config.market == "KR" else 0.01
        buy_price = self._round_price(current_price + buy_offset, "BUY")
        logger.info(f"🚀 [자동재진입] {next_alias} 시작 (1회 매수금: {new_unit_buy:,.2f} @ {buy_price})")
        self._buy(new_unit_buy, buy_price, f"{next_alias} 시작 매수", price_type="00")
        
        self._save_and_finish(current_price=current_price)
        return None

    def _run_reverse_mode_routine(self, current_price: float, date, preview: bool = False):
        """V4.0 리버스 모드(소진 모드) 실행 로직"""
        # [KR Market Split Logic] 한국 시장의 경우 15:10(LOC_ONLY) 세션에서만 실행
        if self.config.market == "KR":
            if self.current_order_filter == "SELL_LIMIT_ONLY":
                logger.info(f"[{self.config.symbol}] 리버스 모드 - 오전 세션 스킵 (15:10에 실행 예정)")
                return None

        logger.info(f"🔄 [{self.config.symbol}] 리버스 모드 가동 중... (T={self.state.current_turn:.2f})")
        
        # 1. 종료 조건 확인: 종가가 평단 대비 회복되었는지 (TQQQ -15%, SOXL -20% 등)
        recovery_threshold = -0.15 if "TQQQ" in self.config.symbol else -0.20
        loss_pct = (current_price - self.state.avg_price) / self.state.avg_price if self.state.avg_price > 0 else 0
        
        if loss_pct > recovery_threshold:
            logger.info(f"✨ 리버스 모드 종료 및 일반 모드 회귀 (손실률 {loss_pct*100:.2f}% > {recovery_threshold*100:.2f}%)")
            if not preview: send_telegram_message(f"✨ <b>[CA V4.0 일반 모드 복귀]</b> {format_symbol_display(self.config.symbol, self.config.market)}\n주가 회복(손실률 {loss_pct*100:.2f}%)에 따라 리버스 모드를 종료합니다.")
            self.state.mode = "NORMAL"
            return self.run_cycle(date, preview=preview)

        # 2. 리버스 별지점 설정: 직전 5거래일 종가 평균 (실제 구현 시 브로커를 통해 5일치 데이터를 가져와야 함)
        # 여기선 단순화를 위해 현재가와 캐시된 가격 활용 (추후 고도화 필요)
        self.state.reverse_star_price = current_price # Fallback
        
        # 3. 매도 로직 (무한 매도)
        total_qty = self.state.total_shares
        sell_unit_divisor = self.config.a_default / 2
        # [점검] math.floor를 사용하여 소수점 이하를 버리고 항상 자연수 수량을 산출합니다.
        sell_qty = math.floor(total_qty / sell_unit_divisor)

        if sell_qty > 0:
            # 첫날은 MOC, 이후는 별지점 LOC
            is_first_day = self.state.current_turn > (self.config.a_default - 0.1) # 소진 직후
            p_type = "33" if is_first_day else "34"
            desc = "리버스 처음매도(MOC)" if is_first_day else "리버스 무한매도(LOC)"
            
            profit = self._sell(sell_qty, self.state.reverse_star_price, desc, price_type=p_type, preview=preview)
            if not preview:
                # T값 업데이트: T * (1 - 1/(a/2))
                self.state.current_turn *= (1 - (1 / sell_unit_divisor))

        # 4. 매수 로직 (쿼터 매수): 둘째날부터 잔금/4 만큼 별지점 아래에서 매수
        is_first_day = self.state.current_turn > (self.config.a_default - 0.1)
        if not is_first_day and self.state.pool > 0:
            buy_budget = self.state.pool / 4
            buy_price = self.state.reverse_star_price * 0.9999 # 별지점 아래
            
            if self._buy(buy_budget, buy_price, "리버스 쿼터매수(LOC)", price_type="34", preview=preview):
                if not preview:
                    # T값 업데이트: T + (a-T)*0.25
                    self.state.current_turn += (self.config.a_default - self.state.current_turn) * 0.25

        if not preview:
            self._save_and_finish(current_price=current_price)
        return self.planned_orders if preview else None

    def _check_and_sell(self, current_price: float, date, preview: bool = False) -> bool:
        """매도 및 모드 변경 조건 확인 및 실행"""
        
        cycle_profit = 0.0
        # [수정] 당일 고정된 별값 사용
        star = self.state.day_start_star_pct if self.state.day_start_star_pct != 0 else self._calculate_star_percent(self.state.current_turn)

        current_loss_pct = (current_price - self.state.avg_price) / self.state.avg_price if self.state.avg_price > 0 else 0
        
        # (1) 쿼터 손절 모드 진입 조건 (NORMAL 모드일 때)
        if self.config.use_quarter_stop and self.state.mode == "NORMAL":
            trigger_turn = self.config.a_default - 1
            if self.state.current_turn >= trigger_turn and current_loss_pct < -0.10:
                logger.info(f"쿼터손절 진입 조건 만족 (T={self.state.current_turn}, 손실={current_loss_pct*100:.1f}%)")
                qty_to_sell = int(self.state.total_shares * 0.25)
                if qty_to_sell > 0:
                    profit = self._sell(qty_to_sell, current_price, "쿼터 손절 진입 매도", price_type="33", preview=preview) # MOC 매도
                    cycle_profit += profit
                
                if not preview:
                    self.state.mode = "QUARTER"
                    self.state.quarter_turn = 0
                # 쿼터 진입 당일은 매수 안함 (backtest_v2_2.py 참조)
                return False # Not sold all

        # [ADD] SELL_LIMIT_ONLY 필터인 경우 지정가 매도(75%)만 처리하고 LOC 매도는 건너뜁니다.
        if self.current_order_filter == "SELL_LIMIT_ONLY":
            logger.info(f"[{self.config.symbol}] SELL_LIMIT_ONLY 필터 적용. 10% 지정가 매도(75%)만 처리합니다.")

        # (2) 매도 로직
        if self.state.mode == 'QUARTER':
            # 쿼터 모드 리셋
            if self.state.quarter_turn >= 10 and current_loss_pct < -0.10:
                logger.info("쿼터 모드 10회 종료 후 여전히 손실. 재진입(리셋)합니다.")
                qty_to_sell = int(self.state.total_shares * 0.25)
                if qty_to_sell > 0:
                    profit = self._sell(qty_to_sell, current_price, "쿼터 손절 재진입 매도", price_type="33", preview=preview) # MOC 매도
                    cycle_profit += profit
                if not preview: self.state.quarter_turn = 0
                return False

            # 매도1: 평단 +10% AFTER (75%)
            target_price = self.state.avg_price * (1.0 + self.config.target_profit_pct)
            target_price = self._round_price(target_price, "SELL")

            # [수정] 현재가 조건(current_price >= target_price) 제거 -> 예약 매도 주문 상시 제출
            qty_75 = int(self.state.total_shares * 0.75)
            if qty_75 > 0:
                profit = self._sell(qty_75, target_price, "(쿼터) 10% 지정가 매도", price_type="00", preview=preview) # 지정가 매도
                cycle_profit += profit

            stop_price = self.state.avg_price * 0.9
            stop_price = self._round_price(stop_price, "SELL")
            # [수정] 현재가 조건 제거. LOC는 장마감시 조건 만족하면 체결되므로 미리 제출.
            # [KR TPS 최적화] REST API 조회 대신 로컬 변수 차감 사용 (웹소켓이 사후 업데이트)
            remaining_shares = max(0, self.state.total_shares - qty_75)

            if remaining_shares > 0:
                profit = self._sell(remaining_shares, stop_price, "(쿼터) -10% LOC 매도", price_type="34", preview=preview) # LOC 매도
                cycle_profit += profit
                
        else: # NORMAL 모드
            total_shares = self.state.total_shares

            # [수정] 수량 계산 우선순위 변경: 25%를 먼저 계산(버림)하고 나머지를 75%에 할당
            qty_25 = int(total_shares * 0.25)
            qty_75 = total_shares - qty_25
            
            # 매도1: 평단 +10% 지정가 매도 (75% 물량)
            target_price = self.state.avg_price * (1.0 + self.config.target_profit_pct)
            target_price = self._round_price(target_price, "SELL")
            
            if qty_75 > 0:
                profit = self._sell(qty_75, target_price, "(V2.2) 10% 지정가 매도", price_type="00", preview=preview)
                cycle_profit += profit
            
            # [ADD] SELL_LIMIT_ONLY 필터인 경우 LOC 매도는 건너뜁니다.
            if self.current_order_filter == "SELL_LIMIT_ONLY":
                return False # Only processed limit sell
            
            # 매도2: 평단 * (1+Star%) 지정가 매도 (25% 물량)
            loc_sell_price = self.state.avg_price * (1.0 + star) # 매도 시에는 오프셋 적용 안함 (자전거래 방지를 위해 매수에서 차감)
            loc_sell_price = self._round_price(loc_sell_price, "SELL") # type: ignore
            
            # [KR TPS 최적화] 주문 직후 잔고조회 생략. 로컬 변수에서 차감하여 계산.
            current_shares = max(0, total_shares - qty_75)
            
            # 첫 번째 매도 후 남은 수량과 계산된 25% 중 작은 값 실행
            # (2주 보유 시 qty_25가 0이므로 실행되지 않음 - 사용자 요청 정상 동작)
            exec_qty_25 = min(current_shares, qty_25) if not preview else qty_25

            if exec_qty_25 > 0:
                profit = self._sell(exec_qty_25, loc_sell_price, "(V2.2) Star% LOC 매도", price_type="34", preview=preview) # price_type 34는 KR 시장에서 00으로 자동 변환됨
                cycle_profit += profit

        # (3) 사이클 종료 체크
        # [KR TPS 최적화] 루프 내 REST API 호출 제거. 
        # 실제 전량 매도 및 차수 전환은 WebSocket 신호 수신 시 또는 
        # 다음 날 Prepare(장 시작 전 동기화) 단계에서 안전하게 처리됩니다.
        # 여기서는 로직상의 일관성을 위해 state 기준으로만 체크합니다.
        if self.state.total_shares < 0.0001 and total_shares > 0:
            logger.info(f"전량 매도 완료. 사이클 종료. 총 실현손익: ${cycle_profit:.2f}")
            
            if not preview:
                self._handle_cycle_finish_and_restart(current_price, date, preview, cycle_profit)

            return True # Sold all
            
        return False
    
    def _buy_routine(self, current_price: float, progress_rate: float = 0.0, preview: bool = False, use_snapshot: bool = False):
        """매수 로직"""
        # [ADD] SELL_LIMIT_ONLY 필터인 경우 매수 로직은 건너뜁니다.
        if getattr(self, 'current_order_filter', None) == "SELL_LIMIT_ONLY": return
        base_price = self.state.avg_price if self.state.avg_price > 0 else current_price
        
        # 자전거래 방지를 위한 매수 오프셋 결정
        loc_buy_offset = -10 if self.config.market == "KR" else -0.01

        # [수정] 당일 고정된 별값 및 회차 사용 여부 결정
        star = self.state.day_start_star_pct if (use_snapshot and self.state.day_start_star_pct != 0) else self._calculate_star_percent(self.state.current_turn)
        turn_to_log = self.state.day_start_turn if (use_snapshot and self.state.day_start_turn > 0) else self.state.current_turn

        # 1. 쿼터 손절 모드 매수
        if self.state.mode == "QUARTER":
            if self.state.quarter_turn < 10:
                buy_amount = self.config.unit_buy_amount
                
                # LOC 매수: min(평단*0.9, 현재가*1.15)
                limit_price = min(base_price * 0.9, current_price * 1.15)
                if current_price <= limit_price:
                    self._buy(buy_amount, limit_price, "쿼터 손절 매수", price_type="34", preview=preview) # LOC
                    if not preview:
                        self.state.quarter_turn += 1
            return

        # 2. 일반 모드 매수
        # 전반전 (진행률 < 50%)
        logger.debug(f"[DEBUG] 매수 루틴: Progress={progress_rate:.2f}, Star={star:.4f}, NextTurn={self.state.current_turn + 0.5}")
        # 주의: sold_all 직후에는 progress_rate가 0.0이므로 전반전 로직을 타게 됨
        if progress_rate < 0.5:
            half_amount = self.config.unit_buy_amount * 0.5
            
            # 평단 LOC: min(평단, 현재가*1.15)
            limit_price_1 = min(base_price, current_price * 1.15)
            self._buy(half_amount, limit_price_1, f"(전반전) 평단가 매수 ({ORDER_TYPE_MAP['34']})", turn=turn_to_log, price_type="34", preview=preview) # LOC

            # 큰수 LOC: min(평단*(1+Star%), 현재가*1.15)
            limit_price_2 = min((base_price * (1.0 + star)) + loc_buy_offset, current_price * 1.15)
            self._buy(half_amount, limit_price_2, f"(전반전) 큰수LOC 매수 ({ORDER_TYPE_MAP['34']})", turn=turn_to_log, price_type="34", preview=preview) # LOC
                
        # 후반전 (진행률 >= 50%)
        else:
            # 큰수 LOC: min(평단*(1+Star%), 현재가*1.15)
            limit_price = min((base_price * (1.0 + star)) + loc_buy_offset, current_price * 1.15)
            self._buy(self.config.unit_buy_amount, limit_price, f"(후반전) 큰수LOC 매수 ({ORDER_TYPE_MAP['34']})", turn=turn_to_log, price_type="34", preview=preview) # LOC

    def _save_and_finish(self, current_price: float = 0.0, silent: bool = False, shares=None, avg_price=None):
        # 핵심 수정: 사이클 종료 직전 T(Turn)값 재계산하여 저장 및 로그 반영
        # [최적화] 이미 확보된 잔고 정보가 있다면 활용하여 TPS 부하 감소
        self._update_current_turn_from_broker(shares=shares, avg_price=avg_price)
        self.state.unit_buy_amount = self.config.unit_buy_amount # Update in state
         
        # 상세 파라미터 상태 보고 준비 (당일 고정값 우선 표시)
        star_pct = self.state.day_start_star_pct if self.state.day_start_star_pct != 0 else self._calculate_star_percent(self.state.current_turn)
        display_t = self.state.day_start_turn if self.state.day_start_turn > 0 else self.state.current_turn

        target_sell_price = self.state.avg_price * (1.0 + self.config.target_profit_pct) if self.state.avg_price > 0 else 0

        symbol_display = format_symbol_display(self.config.symbol, self.config.market)
        # 시장별 통화 및 포맷 정의
        cur_sym = "₩" if self.config.market == "KR" else "$"
        def fmt(v):
            if self.config.market == "KR": return f"{int(v):,}"
            return f"{v:,.2f}"

        # 상태 요약 보고
        prev_close = self.broker.get_previous_close(self.config.symbol)
        summary = (
            f"📊 <b>[{symbol_display} CA 상태 보고]</b>\n"
            f"일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"전날 종가: {cur_sym}{fmt(prev_close)} | 현재가: {cur_sym}{fmt(current_price)}\n"
            f"회차(T): <b>{display_t:.1f}</b> | 모드: {self.state.mode}\n"
            f"보유: {self.state.total_shares:.2f}주 | 평단: {cur_sym}{fmt(self.state.avg_price)}\n"
            f"현재 Star%: {star_pct*100:.2f}% | 목표매도가: {cur_sym}{fmt(target_sell_price)}\n"
            f"1회 매수금: {cur_sym}{fmt(self.config.unit_buy_amount)}"
        )
        logger.info(summary.replace('\n', ' '))
        if self.config.use_db and not silent:
            send_telegram_message(summary)

        logger.info("--- 사이클 로직 종료 ---")
        if self.config.use_db:
            save_state_db(self.state, market=self.state.market, strategy_name=self.state.strategy_name, only_if_exists=True)
        else:
            save_state(self.state, self.config.save_path)


class ValueRebalancingEngine:
    """밸류리밸런싱(VR) '실력공식' 실행 로직 (기존 유지)"""
    def __init__(self, config: VRConfig, broker: Optional[Broker] = None):
        self.config = config
        if broker is None:
            raise ValueError("Broker instance must be provided to the ValueRebalancingEngine.")
        self.broker = broker
        
        # DB 초기화 (DB 모드일 경우)
        if self.config.use_db:
            init_db()

        self.state = self._initialize_state()

        # State에 저장된 설정이 있으면 Config 덮어씌움
        if hasattr(self.state, 'periodic_accumulation') and self.state.periodic_accumulation != 0:
            self.config.periodic_accumulation = self.state.periodic_accumulation
        if hasattr(self.state, 'initial_budget') and self.state.initial_budget > 0:
            self.config.initial_budget = self.state.initial_budget
        if hasattr(self.state, 'G') and self.state.G > 0:
            self.config.G = self.state.G
        if hasattr(self.state, 'band_low_pct'):
            self.config.band_low_pct = self.state.band_low_pct
        if hasattr(self.state, 'band_high_pct'):
            self.config.band_high_pct = self.state.band_high_pct
        if hasattr(self.state, 'investment_type'):
            self.config.investment_type = self.state.investment_type
            
    def _is_already_ordered(self, side: str, price: float, qty: int, open_orders: list) -> bool:
        """VR용 중복 주문 확인 및 소모 (Preview용)"""
        if not open_orders:
            return False
        for i, oo in enumerate(open_orders):
            oo_price = float(oo.get('ft_ord_unpr3') or oo.get('ord_unpr') or oo.get('unpr') or oo.get('pdno_unpr') or 0)
            oo_qty = float(oo.get('nccs_qty') or oo.get('ft_ord_qty3') or oo.get('ord_qty') or 0)
            side_raw = str(oo.get('sll_buy_dvsn_cd', ''))
            oo_side = "SELL" if side_raw in ['01', '1', '매도'] else "BUY"
            
            if oo_side == side and int(oo_qty) == int(qty) and abs(oo_price - price) < 0.01:
                open_orders.pop(i)
                return True
        return False
    
    def _initialize_state(self) -> VRState:
        if self.config.use_db:
            state_data = load_state_db(self.config.symbol, self.config.strategy_type, market=self.config.market, strategy_name=self.config.strategy_name)
        else:
            state_data = load_state(self.config.save_path)
            
        if state_data and state_data.get("strategy_type") == "VR":
            return VRState(**state_data)

        # 초기 상태
        _, _, eval_amt = self.broker.get_account_equity(self.config.symbol)
        cash_pool = self.broker.get_cash_pool()
        initial_E = eval_amt + cash_pool

        return VRState(
            symbol=self.config.symbol,
            strategy_type="VR",
            strategy_name="",
            market=self.config.market,
            V=initial_E,
            pool=cash_pool,
            last_E=initial_E
        )

    def _update_V_skill(self, E: float, contribution: float = 0.0) -> float:
        V1 = self.state.V
        pool = self.state.pool
        G = self.config.G
        # G가 0이 되는 경우 방지
        if G <= 0: G = 10.0

        term1 = pool / G
        term2 = (E - V1) / (2.0 * math.sqrt(G))
        V2 = V1 + term1 + term2 + contribution
        return V2

    def place_daily_limit_orders(self, date, contribution: float = 0.0, is_cycle_start_day: bool = False, check_existing_orders: bool = False, preview: bool = False, order_filter: str = None):
        """
        실전 투자용: 장 시작 전, 당일의 지정가 매수/매도 주문을 제출합니다.
        preview=True일 경우 주문을 제출하지 않고 주문 계획 리스트를 반환합니다.
        """
        symbol = self.config.symbol
        logger.info(f"\n--- [{date.strftime('%Y-%m-%d')}] VR 실전 주문 {'계획' if preview else '제출'} ---")

        current_price = self.broker.get_price(symbol)
        if current_price <= 0:
            logger.warning(f"[{symbol}] 현재가 조회 실패 또는 0 이하의 가격({current_price}). 이번 사이클 주문 로직을 건너뜁니다.")
            return [] if preview else None
        
        planned_orders = []
        open_orders_pool = []
        if check_existing_orders:
            open_orders_pool = self.broker.fetch_open_orders(symbol)
            if open_orders_pool:
                logger.info(f"[{symbol}] VR 미체결 주문 {len(open_orders_pool)}건 감지. 중복 주문은 제외합니다.")

        # 1. 최신 잔고 및 시세 정보 조회
        shares, _, eval_amt = self.broker.get_account_equity(self.config.symbol)
        
        # [수정] VR 엔진에서도 할당된 POOL을 기준으로 평가금(E) 계산
        allocated_pool = self.state.pool
        E = eval_amt + allocated_pool

        # 2. 사이클 시작일인 경우 V값 업데이트
        if is_cycle_start_day and self.state.mode == 'RUNNING':
            logger.info("새로운 VR 사이클 시작. V 및 밴드를 재계산합니다.")
            if contribution != 0:
                logger.info(f"주기적 입출금: ${contribution:.2f} 반영됨.")

            new_V = self._update_V_skill(E, contribution=contribution)
            self.state.V = new_V
            self.state.last_E = E
            self.state.cycle_V = new_V
            self.state.cycle_start_pool = allocated_pool
            logger.info(f"새로운 목표밸류(V): ${self.state.V:,.2f}, E: ${E:,.2f}")
            
            # V값 갱신 알림
            symbol_display = format_symbol_display(symbol, self.config.market)
            prev_close = self.broker.get_previous_close(symbol)
            cur_sym = "₩" if self.config.market == "KR" else "$"
            def fmt_price(v):
                if self.config.market == "KR": return f"{int(v):,}"
                return f"{v:,.2f}"
                
            msg = f"📅 <b>[VR 사이클 갱신]</b> {symbol_display}\n"
            msg += f"일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            msg += f"전날 종가: {cur_sym}{fmt_price(prev_close)} | 현재가: {cur_sym}{fmt_price(current_price)}\n"
            msg += f"새 목표 V: <b>${self.state.V:,.2f}</b>\nPool: ${allocated_pool:,.2f}"
            if self.config.use_db:
                send_telegram_message(msg)

            # 상태 저장
            if self.config.use_db and not preview:
                save_state_db(self.state, market=self.state.market, strategy_name=self.state.strategy_name)
            elif not preview:
                save_state(self.state, self.config.save_path)

        # 3. BOOTSTRAP 모드 처리
        if self.state.mode == 'BOOTSTRAP':
            logger.info("초기 매수(Bootstrap) 단계입니다. 자동 주문을 건너뜁니다. 대시보드나 수동으로 진행해주세요.")
            if preview: return []
            return

        # 4. 주문 계산 및 제출 (RUNNING 모드)
        if self.state.mode != 'RUNNING':
            logger.warning(f"현재 모드({self.state.mode})에서는 자동 주문을 실행할 수 없습니다.")
            if preview: return []
            return

        logger.info(f"리밸런싱 주문 계산. 목표 V: ${self.state.cycle_V:,.2f}")
        # [ADD] SELL_LIMIT_ONLY 필터인 경우 매수 로직은 건너뜁니다.
        if order_filter == "SELL_LIMIT_ONLY":
            logger.info(f"[{self.config.symbol}] SELL_LIMIT_ONLY 필터 적용. 매수 로직은 건너뛰고 매도만 진행합니다.")
        low_band = self.state.cycle_V * (self.config.band_low_pct / 100.0)
        high_band = self.state.cycle_V * (self.config.band_high_pct / 100.0)

        # --- 매도 주문 제출 ---
        if shares > 0:
            for n in range(min(5, int(shares))): # 5개 정도의 지정가 매도 주문 제출
                if (shares - n) <= 0: break
                target_stock_val = high_band - allocated_pool
                if target_stock_val <= 0: break
                limit_price = target_stock_val / (shares - n)
                
                # KR 대응: 호가 단위 보정 및 지정가(00) 강제
                limit_price = self.broker.adjust_price_by_tick(symbol, limit_price, "SELL")
                ptype = "00"

                if limit_price > 0:
                    if preview:
                        if not self._is_already_ordered("SELL", limit_price, 1, open_orders_pool):
                            planned_orders.append({
                                "strategy": "VR", "side": "SELL", "symbol": symbol,
                                "qty": 1, "price": limit_price, "type": ptype, "desc": f"VR_Sell_{n}th"
                            })
                    else:
                        if not self._is_already_ordered("SELL", limit_price, 1, open_orders_pool):
                            self.broker.place_order(self.config.symbol, limit_price, 1, "SELL", price_type=ptype, strategy="VR")
                            time.sleep(0.2) # API rate limit
                        else: # type: ignore
                            logger.info(f"-> [VR 매도] {limit_price} 동일 주문 존재. 스킵.")

        # [ADD] SELL_LIMIT_ONLY 필터인 경우 매수 로직은 건너뜁니다.
        if order_filter == "SELL_LIMIT_ONLY": return planned_orders if preview else None

        # --- 매수 주문 제출 ---
        for n in range(1, 6): # 5개 정도의 지정가 매수 주문 제출
            target_stock_val = low_band - allocated_pool
            if target_stock_val <= 0: break
            limit_price = target_stock_val / (shares + n)

            # KR 대응: 호가 단위 보정 및 지정가(00) 강제
            limit_price = self.broker.adjust_price_by_tick(symbol, limit_price, "BUY")
            ptype = "00"

            if limit_price > 0:
                if preview:
                    if not self._is_already_ordered("BUY", limit_price, 1, open_orders_pool):
                         planned_orders.append({
                                "strategy": "VR", "side": "BUY", "symbol": symbol,
                                "qty": 1, "price": limit_price, "type": ptype, "desc": f"VR_Buy_{n}th"
                            })
                else:
                    if not self._is_already_ordered("BUY", limit_price, 1, open_orders_pool):
                        self.broker.place_order(self.config.symbol, limit_price, 1, "BUY", price_type=ptype, strategy="VR")
                        time.sleep(0.2) # API rate limit
                    else: # type: ignore
                        logger.info(f"-> [VR 매수] {limit_price} 동일 주문 존재. 스킵.")
        
        return planned_orders if preview else None

    def run_cycle(self, date, contribution: float = 0.0, is_cycle_start_day: bool = False):
        logger.info(f"\n--- [{date.strftime('%Y-%m-%d')}] 밸류리밸런싱(VR) 사이클 ---")
        
        current_price = self.broker.get_price(self.config.symbol)
        if current_price <= 0:
            logger.warning(f"[{self.config.symbol}] 현재가 조회 실패 또는 0 이하의 가격({current_price}). 이번 사이클 매수/매도 로직을 건너뜁니다.")
            self.state.pool = self.broker.get_cash_pool() # Pool은 업데이트
            self._save_state() # 상태 저장 후 종료
            return
        day_high = self.broker.get_current_high(symbol=self.config.symbol)
        day_low = self.broker.get_current_low(symbol=self.config.symbol)
        shares, _, eval_amt = self.broker.get_account_equity(self.config.symbol)
        cash_on_account = self.broker.get_cash_pool()
        E = eval_amt + cash_on_account

        # --- 사이클 시작일: V값 및 밴드 갱신 ---
        if is_cycle_start_day and self.state.mode == 'RUNNING':
            logger.info("새로운 VR 사이클 시작. V 및 밴드를 재계산합니다.")
            if contribution != 0:
                logger.info(f"주기적 입출금: ${contribution:.2f} 반영됨.")

            new_V = self._update_V_skill(E, contribution=contribution)
            self.state.V = new_V
            self.state.last_E = E
            self.state.cycle_V = new_V
            self.state.cycle_start_pool = cash_on_account
            logger.info(f"새로운 목표밸류(V): ${self.state.V:,.2f}")

        # --- 모드별 로직 실행 ---
        if self.state.mode == 'BOOTSTRAP':
            logger.info("초기 매수(Bootstrap) 단계 진행 중...")
            if self.state.bootstrap_day_count < 10:
                buy_amount = (self.config.initial_budget * 0.75) / 10.0
                if cash_on_account >= buy_amount and current_price > 0:
                    qty = int(buy_amount / current_price)
                    if qty > 0:
                        self.broker.place_order(self.config.symbol, current_price, qty, "BUY", price_type="00")
                        logger.info(f"  -> 초기 매수 {self.state.bootstrap_day_count + 1}/10: {qty}주 매수")
                self.state.bootstrap_day_count += 1
            
            if self.state.bootstrap_day_count >= 10:
                logger.info("초기 매수(Bootstrap) 완료. 일반 리밸런싱 모드로 전환합니다.")
                self.state.mode = 'RUNNING'
                # 첫 사이클 V값 계산을 위해 is_cycle_start_day를 True로 간주
                self.run_cycle(date, contribution, is_cycle_start_day=True)
                return # 재귀 호출 후 종료

        elif self.state.mode == 'RUNNING':
            logger.info(f"리밸런싱 실행. 목표 V: ${self.state.cycle_V:,.2f}")
            cycle_low_band = self.state.cycle_V * (self.config.band_low_pct / 100.0)
            cycle_high_band = self.state.cycle_V * (self.config.band_high_pct / 100.0)
            
            # --- 매도 로직 (지정가 예약 매도) ---
            # 하루 중 '고가'가 지정가에 도달하면 체결
            shares_start = int(shares)
            if shares_start > 0:
                for n in range(shares_start):
                    # (보유수량 - n)이 0이 되는 것 방지
                    if (shares_start - n) <= 0: break
                    
                    # [BUG FIX] 매도 지정가 계산 시 현금(Pool) 고려 (E = Stock + Pool)
                    # 목표: E >= High_Band  =>  Stock_Val + Pool >= High_Band  =>  Stock_Val >= High_Band - Pool
                    target_stock_val = cycle_high_band - cash_on_account
                    
                    if target_stock_val <= 0:
                        # 현금만으로 이미 밴드 초과 -> 즉시 매도 필요 (가격을 0으로 설정하여 무조건 체결 유도)
                        limit_sell_price = 0.0
                    else:
                        limit_sell_price = target_stock_val / (shares_start - n)
                    
                    # 백테스트: 당일 고가가 지정가 이상이면 체결
                    if day_high >= limit_sell_price:
                        # 체결가는 지정가와 당일 저가 중 큰 값 (지정가가 터무니없이 낮을 경우 Market Sell 효과 시뮬레이션)
                        exec_price = max(limit_sell_price, day_low)
                        logger.info(f"  -> 밴드 상단 지정가 매도 체결: 1주 @ ${exec_price:.2f} (Limit: ${limit_sell_price:.2f}, High: ${day_high:.2f})")
                        self.broker.place_order(self.config.symbol, exec_price, 1, "SELL", price_type="00")
                    else:
                        # 가격 오름차순(지정가가 높아짐)이므로, 낮은 가격이 체결 안 되면 더 높은 가격도 체결 안 됨
                        break

            # --- 매수 로직 (지정가 예약 매수) ---
            # 매도 후 현금이 늘어났을 수 있으므로 잔고 재확인 (VR은 보통 아침에 일괄 주문이지만, 백테스트는 순차 처리)
            current_shares_after_sell, _, _ = self.broker.get_account_equity(self.config.symbol)
            
            # 최대 100단계의 매수 주문을 시뮬레이션 (무한 루프 방지)
            for n in range(1, 101):
                # [BUG FIX] 매수 지정가 계산 시 현금(Pool) 고려
                # 목표: E <= Low_Band  =>  Stock_Val + Pool <= Low_Band  =>  Stock_Val <= Low_Band - Pool
                target_stock_val = cycle_low_band - cash_on_account
                
                if target_stock_val <= 0:
                    # 현금이 이미 Low Band보다 많음 -> 주가가 0원이 되어도 E > Low_Band -> 매수 불필요
                    limit_buy_price = 0.0
                else:
                    limit_buy_price = target_stock_val / (current_shares_after_sell + n)
                
                # 백테스트: 당일 저가가 지정가 이하이면 체결
                # limit_buy_price가 0.0이면 체결되지 않음 (day_low > 0 가정)
                if day_low <= limit_buy_price and limit_buy_price > 0:
                    # 1주 매수 시도
                    # 체결가는 지정가와 당일 고가 중 작은 값
                    exec_price = min(limit_buy_price, day_high)
                    
                    # place_order 내에서 POOL 한도 및 현금 부족 체크 후 True/False 반환
                    success = self.broker.place_order(self.config.symbol, exec_price, 1, "BUY", price_type="00")
                    if success:
                        logger.info(f"  -> 밴드 하단 지정가 매수 체결: 1주 @ ${exec_price:.2f} (Limit: ${limit_buy_price:.2f}, Low: ${day_low:.2f})")
                    else:
                        # 현금 부족이나 한도 초과로 매수 실패 시 더 낮은 가격 주문도 불가능하다고 가정하거나 루프 종료
                        # (VR 원칙상 한도 도달하면 중단)
                        break
                else:
                    # 가격 내림차순(지정가가 낮아짐)이므로, 높은 가격이 체결 안 되면 더 낮은 가격도 체결 안 됨
                    break

        # 최종 상태 업데이트
        self.state.pool = self.broker.get_cash_pool()

        self._save_state()
        logger.info("--- 밸류리밸런싱(VR) 사이클 종료 ---")

    def _save_state(self):
        if self.config.use_db:
            save_state_db(self.state, market=self.state.market, strategy_name=self.state.strategy_name)
        else:
            save_state(self.state, self.config.save_path)

def load_etf_list(path: str = None) -> List[str]:
    if path is None:
        path = os.path.join(PROJECT_ROOT, "env", "ETF_list_us.txt")
    try:
        with open(path, "r", encoding="utf-8") as f:
            tickers = []
            for line in f:
                clean_line = line.split('#')[0].strip()
                if clean_line:
                    tickers.append(clean_line.split()[0])
            return tickers
    except FileNotFoundError:
        return ["TQQQ", "SOXL"]

if __name__ == "__main__":
    pass
