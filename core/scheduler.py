import os
import sys
import time
import logging
from datetime import datetime, timedelta, time as dtime
import requests
import shutil
import threading
import asyncio
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from logging.handlers import RotatingFileHandler
from pytz import timezone

# 프로젝트 루트 경로 추가
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

# --- 로깅 설정 (모듈 임포트 전 최상단 배치) ---
log_dir = os.path.join(PROJECT_ROOT, "logs")
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, "scheduler.log")

logger = logging.getLogger()
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# [수정] scheduler.log도 RotatingFileHandler를 사용하여 2MB 단위로 실시간 자동 로테이션 처리
sh = RotatingFileHandler(log_file, maxBytes=2*1024*1024, backupCount=5, encoding="utf-8")
sh.setFormatter(formatter)
logger.addHandler(sh)

# 2. error_log.txt (대시보드용)
err_log = os.path.join(log_dir, "error_log.txt")
eh = RotatingFileHandler(err_log, maxBytes=2*1024*1024, backupCount=5, encoding="utf-8")
eh.setLevel(logging.ERROR)
eh.setFormatter(formatter)
logger.addHandler(eh)

# 3. message_log.txt (대시보드용)
msg_log = os.path.join(log_dir, "message_log.txt")
mh = RotatingFileHandler(msg_log, maxBytes=2*1024*1024, backupCount=5, encoding="utf-8")
mh.setLevel(logging.INFO)
mh.setFormatter(formatter)
logger.addHandler(mh)

# 콘솔 출력용
console = logging.StreamHandler()
console.setFormatter(formatter)
logger.addHandler(console)

# apscheduler 로그 레벨 조정 (Heartbeat 등 1분 단위 반복 작업의 INFO 로그 노이즈 제거)
logging.getLogger('apscheduler').setLevel(logging.WARNING)

logger.info("--- cavr 스케줄러 초기화 시작 ---")

# .env 파일 명시적 로드 (모든 모듈 임포트 전)
from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, "env", ".env"))

from core.database import sync_trade_history_db, sync_open_orders_db
from core.database import get_all_states_db, init_db, get_config, set_config
from core.cavr import CAConfig, CostAveragingEngine, VRConfig, ValueRebalancingEngine, fetch_kr_holiday, fetch_us_holiday
from core.brokers.toss import TossBroker
from core.notifier import send_telegram_message
from core.email_service import job_send_daily_report, job_send_monthly_report

# [ADD] 403 Forbidden 에러 추적을 위한 전역 변수
forbidden_error_tracker = {"US": 0, "KR": 0}

def handle_api_error_tracking(market, error):
    """403 에러 지속 여부를 확인하고 텔레그램 알림 발송"""
    global forbidden_error_tracker
    err_str = str(error)
    if "403" in err_str or "Forbidden" in err_str:
        forbidden_error_tracker[market] += 1
        logger.warning(f"⚠️ [{market}] 403 Forbidden 에러 감지 ({forbidden_error_tracker[market]}/3)")
        if forbidden_error_tracker[market] >= 3:
            send_telegram_message(
                f"🚨 <b>[KIS API 차단 위험]</b> {market} 시장 API에서 403 Forbidden 에러가 3회 연속 발생했습니다.\n"
                f"과도한 요청으로 인해 IP가 임시 차단되었을 수 있으니 시스템 상태를 즉시 점검하십시오."
            )
            forbidden_error_tracker[market] = 0 # 알림 발송 후 카운트 리셋

def load_market_env(market: str):
    env_path = os.path.join(PROJECT_ROOT, "env", f".env.{market.lower()}")
    load_dotenv(env_path, override=True)

def is_biweekly_friday(d: datetime) -> bool:
    if d.weekday() != 4:  # 4 is Friday
        return False
    week_number = d.isocalendar()[1]
    return week_number % 2 == 0

def update_heartbeat():
    """스케줄러가 살아있음을 DB에 기록 (매 1분)"""
    try:
        set_config("scheduler_heartbeat", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as e:
        logger.error(f"Heartbeat update failed: {e}")

def job_check_kr_holiday():
    """한국 시장 개장 여부를 확인하여 DB에 저장"""
    is_open = fetch_kr_holiday()
    status = "Y" if is_open else "N"
    set_config("kr_market_opnd_yn", status)
    logger.info(f"📅 [KR Holiday] 오늘 개장 여부 확인 완료: {status}")
    if not is_open:
        send_telegram_message("📅 <b>[시장 안내]</b> 오늘은 한국 시장 휴장일입니다.")

def job_check_us_holiday():
    """미국 시장 개장 여부를 확인하여 DB에 저장"""
    try:
        is_open = fetch_us_holiday()
    except Exception as e:
        logger.error(f"US Holiday check failed: {e}")
        # API 장애 시 주말이 아니면 개장으로 간주하되 알림 발송
        is_open = datetime.now(timezone('America/New_York')).weekday() < 5
        send_telegram_message("⚠️ <b>[점검]</b> 미국 휴장일 API 조회에 실패하여 요일 기준으로 개장 여부를 추정합니다.")
    
    status = "Y" if is_open else "N"
    set_config("us_market_opnd_yn", status)
    logger.info(f"📅 [US Holiday] 오늘 개장 여부 확인 완료: {status}")
    if not is_open:
        send_telegram_message("📅 <b>[시장 안내]</b> 오늘은 미국 시장 휴장일입니다.")

def run_ca_strategies(market: str = "US", check_existing: bool = False, force: bool = False, order_filter: str = None, transition_only: bool = False, broker=None):
    """DB에 저장된 모든 CA 전략을 실행합니다."""
    if broker is None:
        broker = TossBroker(market=market)

    # [ADD] 시장 운영 시간 외에는 주문 제출을 건너뜁니다.
    if not force and not is_market_active_for_orders(market):
        logger.info(f"⏸️ [CA-{market}] 시장 운영 시간 외이므로 주문 제출을 건너뜁니다.")
        return

    # 1. 스케줄러 활성화 여부 체크
    if not force and get_config("scheduler_status") != "running":
        logger.info(f"⏸️ [CA-{market}] 스케줄러가 정지 상태이므로 작업을 건너뜁니다.")
        return
    # 2. CA 전략 활성화 여부 체크
    if get_config("enable_ca") != "true":
        logger.info(f"⏸️ [CA-{market}] CA 전략이 비활성화되어 있어 건너뜁니다.")
        return

    logger.info(f"--- [CA-{market}] 전략 자동 실행 시작 ---")
    ca_states = get_all_states_db("CA", market=market)
    if not ca_states:
        logger.info("실행할 CA 전략이 없습니다.")
        return

    for i, state_data in enumerate(ca_states):
        symbol = state_data.get('symbol')
        alias = state_data.get('strategy_name', '')

        if i > 0:
            logger.info(f"⏳ 다음 전략 실행 전 60초간 대기합니다... ({i+1}/{len(ca_states)})")
            time.sleep(60)

        if not state_data.get('is_active', True):
            logger.info(f"⏭️ [CA-{market}] '{symbol}' ({alias}) 전략이 일시 정지 상태입니다. 건너뜁니다.")
            continue

        logger.info(f"==> [{market}] '{symbol}' ({alias}) CA 전략 처리 시작...")
        
        cash = broker.get_cash_pool()
        unit_buy = state_data.get('unit_buy_amount', 0)
        if cash < unit_buy and unit_buy > 0:
            send_telegram_message(f"⚠️ <b>[잔고 부족]</b> {symbol} 매수 필요금(${unit_buy:.2f})보다 예수금(${cash:.2f})이 적습니다. 매도 주문 위주로 진행됩니다.")

        try:
            config = CAConfig(symbol=symbol, use_db=True, market=market, strategy_name=alias)
            engine = CostAveragingEngine(config, broker=broker)
            engine.run_cycle(datetime.now(), check_existing_orders=check_existing, order_filter=order_filter, transition_only=transition_only)
            forbidden_error_tracker[market] = 0 # 성공 시 초기화
            logger.info(f"'{symbol}' CA 전략 처리 완료.")
        except Exception as e:
            handle_api_error_tracking(market, e)
            logger.error(f"'{symbol}' CA 전략 실행 중 오류 발생: {e}", exc_info=True)
            send_telegram_message(f"🚨 <b>[CA 에러]</b> {symbol} 전략 실행 중 오류:\n{str(e)}")
    logger.info(f"--- [CA-{market}] 전략 자동 실행 종료 ---")

def run_vr_strategies(market: str = "US", check_existing: bool = False, force: bool = False, order_filter: str = None, broker=None):
    """DB에 저장된 모든 VR 전략의 지정가 주문을 제출합니다."""
    if broker is None:
        broker = TossBroker(market=market)

    # [ADD] 시장 운영 시간 외에는 주문 제출을 건너뜁니다.
    if not force and not is_market_active_for_orders(market):
        logger.info(f"⏸️ [VR-{market}] 시장 운영 시간 외이므로 주문 제출을 건너뜁니다.")
        return

    # 1. 스케줄러 활성화 여부 체크
    if not force and get_config("scheduler_status") != "running":
        logger.info(f"⏸️ [VR-{market}] 스케줄러가 정지 상태이므로 작업을 건너뜁니다.")
        return
    # 2. VR 전략 활성화 여부 체크
    if get_config("enable_vr") != "true":
        logger.info(f"⏸️ [VR-{market}] VR 전략이 비활성화되어 있어 건너뜁니다.")
        return

    logger.info(f"--- [VR-{market}] 전략 자동 주문 시작 ---")
    vr_states = get_all_states_db("VR", market=market)
    if not vr_states:
        logger.info("실행할 VR 전략이 없습니다.")
        return

    today = datetime.now()
    is_cycle_day = is_biweekly_friday(today)

    for i, state_data in enumerate(vr_states):
        symbol = state_data.get('symbol')
        alias = state_data.get('strategy_name', '')

        if i > 0:
            logger.info(f"⏳ 다음 전략 실행 전 60초간 대기합니다... ({i+1}/{len(vr_states)})")
            time.sleep(60)

        if not state_data.get('is_active', True):
            logger.info(f"⏭️ [VR-{market}] '{symbol}' ({alias}) 전략이 일시 정지 상태입니다. 건너뜁니다.")
            continue

        logger.info(f"==> [{market}] '{symbol}' ({alias}) VR 전략 처리 시작...")

        if broker.get_cash_pool() < 50: # 최소 안전 마진
             send_telegram_message(f"⚠️ <b>[잔고 부족]</b> {symbol} VR 매수를 위한 예수금이 부족합니다. 매도 주문만 시도합니다.")

        try:
            config = VRConfig(symbol=symbol, use_db=True, market=market, strategy_name=alias, investment_type="accumulation")
            engine = ValueRebalancingEngine(config, broker=broker)
            engine.place_daily_limit_orders(
                date=today,
                contribution=config.periodic_accumulation if is_cycle_day else 0.0,
                is_cycle_start_day=is_cycle_day,
                check_existing_orders=check_existing,
                order_filter=order_filter
            )
            forbidden_error_tracker[market] = 0 # 성공 시 초기화
            logger.info(f"'{symbol}' VR 전략 처리 완료.")
        except Exception as e:
            handle_api_error_tracking(market, e)
            logger.error(f"'{symbol}' VR 전략 실행 중 오류 발생: {e}", exc_info=True)
            send_telegram_message(f"🚨 <b>[VR 에러]</b> {symbol} 전략 실행 중 오류:\n{str(e)}")
    logger.info(f"--- [VR-{market}] 전략 자동 주문 종료 ---")

def is_market_active_for_orders(market: str) -> bool:
    """주문 제출이 가능한 시장 운영 시간인지 확인 (정규장 + 프리/애프터 마켓)"""
    if market == "KR":
        tz = timezone('Asia/Seoul')
        # 한국 시장은 정규장 시간만 주문 가능 (09:00 ~ 15:30)
        open_time, close_time = dtime(9, 0), dtime(15, 31, 0)
    else: # US Market
        tz = timezone('America/New_York')
        # 미국 시장은 실질적인 프리마켓 활성화부터 정규장 종료 후 1분까지 (07:00 ~ 16:01 ET)
        open_time, close_time = dtime(7, 0), dtime(16, 1, 0)

    now = datetime.now(tz)
    if now.weekday() >= 5: # 토, 일
        return False
    
    return open_time <= now.time() <= close_time

def job_market_prepare(market: str = "US"):
    """정규장 시작 3분 후: 체결 동기화 및 차수 전환 처리"""
    if get_config("scheduler_status") != "running": return
    if get_config(f"{market.lower()}_market_opnd_yn", "Y") == "N": return

    logger.info(f"🛠️ [{market}] 정규장 시작 3분 후 준비 작업(차수 전환 등)을 시작합니다.")
    broker = TossBroker(market=market)
    
    try:
        # 1. 전일 종가 및 전일 미체결 결과 반영을 위해 동기화
        start_date = (datetime.now() - timedelta(days=3)).strftime("%Y%m%d")
        end_date = datetime.now().strftime("%Y%m%d")
        
        # [ADD] 미국 시장 준비 시 환율 정보 업데이트
        if market == "US":
            job_update_exchange_rate(broker)

        for st in get_all_states_db(market=market):
            execs = broker.fetch_execution_history(st['symbol'], start_date, end_date)
            sync_trade_history_db(st['symbol'], execs, strategy=st.get('strategy_type'), market=market, strategy_name=st.get('strategy_name'), silent=True)
        
        # 2. CA 전략 차수 전환만 수행 (transition_only=True)
        # 이 과정에서 전일 전량 매도된 종목은 '다음 차수 1회차 매수'가 나갑니다.
        run_ca_strategies(market=market, check_existing=True, transition_only=True, broker=broker)
        
        logger.info(f"✅ [{market}] 준비 작업 완료. 2분 후 주문 제출을 시작합니다.")
    except Exception as e:
        logger.error(f"[{market}] Job Market Prepare Failed: {e}")

def job_update_exchange_rate(broker=None, force=False):
    """Toss API를 사용하여 달러 환율을 조회하고 DB에 저장"""
    try:
        # 당일 이미 업데이트했다면 건너뜁니다 (force=True인 경우 제외)
        today_str = datetime.now().strftime("%Y-%m-%d")
        last_update_date = get_config("USDKRW_UPDATE_DATE", "")
        if not force and last_update_date == today_str:
            logger.info(f"💵 [ExchangeRate] 오늘({today_str}) 환율 업데이트가 이미 완료되었습니다.")
            return

        if broker is None:
            broker = TossBroker(market="US")

        # 토스 환율 API 호출
        data = broker._call_api("GET", "/api/v1/exchange-rate", params={
            "baseCurrency": "USD",
            "quoteCurrency": "KRW"
        })
        
        res_obj = data.get("result", {})
        rate_val = res_obj.get("rate")
        
        if rate_val:
            rate = float(rate_val)
            set_config("USDKRW", f"{rate:.2f}")
            # 업데이트 날짜 및 시간(KST) 저장
            set_config("USDKRW_UPDATE_DATE", today_str)
            set_config("USDKRW_UPDATE_TIME", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            
            # bp 값을 가져와서 diff 계산
            bp = float(res_obj.get("basisPoint", 0))
            diff = (bp / 10000.0) * rate  # 변동 금액 추정
            set_config("USDKRW_DIFF", f"{diff:+.2f}")
            set_config("USDKRW_PCT", "0.00")
            logger.info(f"💵 [ExchangeRate] TOSS API 환율 업데이트 완료: 1$ = ₩{rate:,.2f}")
            send_telegram_message(f"💵 <b>[환율 업데이트 완료]</b>\n시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n현재 환율: 1$ = <b>₩{rate:,.2f}</b>")
        else:
            logger.warning("⚠️ [ExchangeRate] TOSS 환율 응답 데이터가 올바르지 않습니다.")
    except Exception as e:
        logger.error(f"환율 업데이트 중 오류 발생: {e}")
        send_telegram_message(f"❌ <b>[환율 업데이트 에러]</b>\n시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n시스템 사유: {str(e)}")

def job_market_open(market: str = "US"):
    """정규장 시작 직후 (5분후) 실행할 작업"""
    # [ADD] 시장 운영 시간 외에는 주문 제출을 건너뜁니다.
    if not is_market_active_for_orders(market):
        logger.info(f"⏸️ [MarketOpen-{market}] 시장 운영 시간 외이므로 주문 제출을 건너뜁니다.")
        return
    if get_config("scheduler_status") != "running":
        logger.info(f"⏸️ [MarketOpen-{market}] 스케줄러가 정지 상태입니다.")
        return

    # 한국 시장인 경우 휴장 여부 최종 확인
    if market == "KR" and get_config("kr_market_opnd_yn") == "N":
        logger.info(f"⏭️ [MarketOpen-KR] 오늘은 휴장일입니다. 작업을 건너뜁니다.")
        return

    # 미국 시장인 경우 휴장 여부 최종 확인
    if market == "US" and get_config("us_market_opnd_yn") == "N":
        logger.info(f"⏭️ [MarketOpen-US] 오늘은 휴장일입니다. 작업을 건너뜁니다.")
        return

    logger.info(f"🔔 [{market}] 정규장 시작 5분 후 자동매매 작업을 시작합니다.")
    send_telegram_message(f"🔔 <b>[{market} 자동매매 시작]</b>\n시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n정규장 개장 5분 후 주문 작업을 시작합니다.")
    
    broker = TossBroker(market=market)
    try:
        start_date = (datetime.now() - timedelta(days=5)).strftime("%Y%m%d")
        end_date = datetime.now().strftime("%Y%m%d")
        for st in get_all_states_db(market=market): # 해당 시장의 모든 전략에 대해 동기화
            execs = broker.fetch_execution_history(st['symbol'], start_date, end_date)
            sync_trade_history_db(st['symbol'], execs, strategy=st.get('strategy_type'), market=market, strategy_name=st.get('strategy_name'), silent=True)
        forbidden_error_tracker[market] = 0 # 성공 시 초기화

        is_auto = get_config("planning_mode") == "auto"
        
        # 한국 시장의 경우 장 시작 전에는 지정가(LIMIT) 매도 주문만 제출
        if market == "KR":
            run_ca_strategies(market=market, check_existing=True, order_filter="SELL_LIMIT_ONLY", broker=broker)
        else:
            run_ca_strategies(market=market, check_existing=True, broker=broker)
            
        run_vr_strategies(market=market, check_existing=True, broker=broker)
        
        if is_auto:
            logger.info(f"[{market}] 자동 주문 모드: 주문이 정상적으로 제출되었습니다.")
        else:
            logger.info(f"[{market}] 수동 주문 모드: 대시보드에서 계획 확인 후 승인이 필요합니다.")
            
        logger.info(f"[{market}] 오늘의 투자 사이클 점검을 완료했습니다.")
    except Exception as e:
        handle_api_error_tracking(market, e)
        logger.error(f"[{market}] Job Market Open Failed: {e}", exc_info=True)

def job_kr_market_late_open():
    """한국 시장 개장 1시간 후(10:00): 신규 차수 진입 및 초기 매수 실행"""
    market = "KR"
    if get_config("scheduler_status") != "running": return
    if get_config("kr_market_opnd_yn") == "N": return

    logger.info(f"🔔 [{market}] 개장 1시간 후 신규 차수 진입 체크를 시작합니다.")
    broker = TossBroker(market="KR")
    try:
        # [수정] transition_only=True를 추가하여 잔고가 있을 때의 일반 매수 로직 작동을 방지하고
        # 오직 잔고 0인 종목의 신규 차수 진입 및 초기 매수만 수행하도록 제한합니다.
        run_ca_strategies(market=market, check_existing=True, transition_only=True, broker=broker)
        logger.info(f"[{market}] 신규 차수 진입 점검 완료.")
    except Exception as e:
        logger.error(f"[{market}] Job KR Late Open Failed: {e}")

def job_us_pre_market_limit_sells():
    """미국 시장 프리마켓 시작 시 지정가 매도 주문 제출"""
    market = "US"
    if get_config("scheduler_status") != "running":
        logger.info(f"⏸️ [US-PreMarket] 스케줄러가 정지 상태입니다.")
        return
    if get_config("us_market_opnd_yn") == "N":
        logger.info(f"⏭️ [US-PreMarket] 오늘은 미국 시장 휴장일입니다. 작업을 건너뜁니다.")
        return

    logger.info(f"🔔 [{market}] 프리마켓 활성화에 따른 지정가 매도 주문을 제출합니다. (07:05 ET)")
    send_telegram_message(f"🔔 <b>[{market} 프리마켓 주문]</b>\n시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n07:05 ET 지정가 매도 주문 제출을 시작합니다.")
    
    try:
        # CA와 VR 전략 모두에서 지정가 매도 주문만 제출
        run_ca_strategies(market=market, check_existing=True, order_filter="SELL_LIMIT_ONLY")
        run_vr_strategies(market=market, check_existing=True, order_filter="SELL_LIMIT_ONLY")
    except Exception as e:
        logger.error(f"[{market}] Job US Pre-Market Limit Sells Failed: {e}", exc_info=True)

def job_sync_and_report(market: str):
    """장 마감 후 모든 내역을 최종 동기화하고 보고서를 발송합니다."""
    # [ADD] 휴장일 체크: 휴장일이면 작업을 진행하지 않음
    # 휴장일 정보가 아직 업데이트되지 않았을 수 있으므로, 해당 시장의 현재 요일도 함께 확인합니다.
    tz = timezone('Asia/Seoul') if market == "KR" else timezone('America/New_York')
    now_in_market_tz = datetime.now(tz)
    if now_in_market_tz.weekday() >= 5: # 토, 일요일
        logger.info(f"⏭️ [{market}] 오늘은 주말입니다. 장 마감 동기화 및 보고서 작업을 건너뜁니다.")
        return
    opnd_yn = get_config(f"{market.lower()}_market_opnd_yn", "Y") # DB에 저장된 휴장일 정보
    if opnd_yn == "N":
        logger.info(f"⏭️ [{market}] 오늘은 휴장일입니다. 장 마감 동기화 및 보고서 작업을 건너뜁니다.")
        return

    logger.info(f"🏁 [{market}] 장 마감 데이터 최종 동기화 및 보고서 생성 시작")
    load_market_env(market)
    broker = TossBroker(market=market)
    
    try:
        # 최근 2일간의 모든 내역을 훑어 누락된 체결/주문 상태 확인
        start_date = (datetime.now() - timedelta(days=2)).strftime("%Y%m%d")
        end_date = datetime.now().strftime("%Y%m%d")
        
        for st in get_all_states_db(market=market):
            symbol = st['symbol']
            # 1. 체결 내역(Execution) 동기화
            execs = broker.fetch_execution_history(symbol, start_date, end_date)
            sync_trade_history_db(symbol, execs, strategy=st.get('strategy_type'), market=market, strategy_name=st.get('strategy_name'))
            # 2. 미체결/취소 주문(Open Orders) 동기화
            open_orders = broker.fetch_open_orders(symbol)
            sync_open_orders_db(symbol, open_orders)
            
        # 동기화 완료 후 보고서 발송
        job_send_daily_report(market)
    except Exception as e:
        logger.error(f"[{market}] 최종 동기화 실패: {e}")
        job_send_daily_report(market) # 실패하더라도 보고서는 일단 발송 시도

def job_hourly_market_sync(market: str):
    """장중 매시간 45분: 체결 및 미체결 주문 REST API 동기화 백업 잡"""
    if get_config("scheduler_status") != "running": return
    if get_config(f"{market.lower()}_market_opnd_yn", "Y") == "N": return

    logger.info(f"🔄 [{market}] 장중 정기 동기화 작업을 시작합니다.")
    load_market_env(market)
    broker = TossBroker(market=market)
    
    try:
        # 최근 1일(어제 및 오늘) 데이터 동기화
        start_date = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        end_date = datetime.now().strftime("%Y%m%d")
        
        for st in get_all_states_db(market=market):
            symbol = st['symbol']
            # 1. 체결 내역(Execution) 동기화 (REST API)
            execs = broker.fetch_execution_history(symbol, start_date, end_date)
            sync_trade_history_db(symbol, execs, strategy=st.get('strategy_type'), market=market, strategy_name=st.get('strategy_name'), silent=True)
            # 2. 미체결/취소 주문(Open Orders) 동기화
            open_orders = broker.fetch_open_orders(symbol)
            sync_open_orders_db(symbol, open_orders)
            
        logger.info(f"✅ [{market}] 장중 정기 동기화 완료.")
    except Exception as e:
        logger.error(f"[{market}] 장중 정기 동기화 중 오류 발생: {e}")

def job_kr_loc_simulation():
    """한국 시장 장 종료 직전(15:15) LOC 모사 주문 실행"""
    market = "KR"
    
    if get_config("scheduler_status") != "running":
        return

    # 휴장일 체크
    if get_config("kr_market_opnd_yn") == "N":
        logger.info(f"⏭️ [LOC-Simulation] 오늘은 한국 휴장일이므로 시뮬레이션을 건너뜁니다.")
        return

    logger.info(f"🔔 [{market}] 장 종료 전 LOC 모사 주문을 시작합니다. (15:10)")
    send_telegram_message(f"🔔 <b>[{market} LOC 모사 주문]</b> 15:10 주문 제출을 시작합니다.")
    
    try:
        # 15:15에는 LOC 매수, LOC Star% 매수, LOC 매도 주문만 제출
        run_ca_strategies(market=market, check_existing=False, order_filter="LOC_ONLY")
    except Exception as e:
        logger.error(f"[{market}] Job Market Open Failed: {e}", exc_info=True)

def job_check_shutdown():
    """DB의 종료 플래그를 확인하여 프로세스 종료"""
    if get_config("system_shutdown_flag") == "true":
        logger.info("🛑 시스템 종료 신호를 감지했습니다. 스케줄러를 종료합니다.")
        # 다음 실행을 위해 플래그 초기화
        set_config("system_shutdown_flag", "false")
        time.sleep(1)
        os._exit(0)

def job_intraday_check(market: str = "US"):
    """장중 주기적 감시 작업"""
    if get_config("scheduler_status") == "running" and get_config("enable_ca") == "true":
        logger.info(f"🔍 [Intraday-{market}] 장중 급락 감시를 시작합니다.")
        ca_states = get_all_states_db("CA", market=market)
        if not ca_states: return

        broker = TossBroker(market=market)
        for state_data in ca_states:
            symbol = state_data.get('symbol')
            alias = state_data.get('strategy_name', '')
            try:
                config = CAConfig(symbol=symbol, use_db=True, market=market, strategy_name=alias)
                engine = CostAveragingEngine(config, broker=broker)
                engine.run_intraday_check()
                forbidden_error_tracker[market] = 0 # 성공 시 초기화
            except Exception as e:
                handle_api_error_tracking(market, e)
                logger.error(f"[Intraday-{market}] {symbol} 감시 중 오류: {e}")

def job_db_backup():
    """DB 파일을 매일 백업 폴더로 복사합니다."""
    backup_dir = os.path.join(PROJECT_ROOT, "data", "backup")
    os.makedirs(backup_dir, exist_ok=True)
    
    db_path = os.path.join(PROJECT_ROOT, "data", "cavr.db")
    if os.path.exists(db_path):
        # 백업 전 DB 및 로그 유지보수(오래된 취소 주문 삭제 등) 수행
        init_db(run_maintenance=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(backup_dir, f"cavr_backup_{timestamp}.db")
        try:
            shutil.copy2(db_path, backup_path)
            logger.info(f"📁 [Backup] DB 백업 완료: {backup_path}")
            # 오래된 백업 파일 관리(예: 7일 경과 삭제) 로직을 추가할 수도 있습니다.
        except Exception as e:
            logger.error(f"📁 [Backup] DB 백업 실패: {e}")

def start_websocket_client(market: str = "US"):
    """웹소켓 클라이언트 비가동 (TOSS 웹소켓 미지원)"""
    logger.info(f"📡 [{market}] 토스증권 웹소켓 미지원으로 모니터링 스레드를 가동하지 않습니다.")

def main():
    init_db(run_maintenance=True) # 스케줄러 시작 시에만 1회 유지보수 실행
    
    # --- 초기 설정 (설정이 없는 경우에만 초기화) ---
    if get_config("scheduler_status") is None:
        set_config("scheduler_status", "stopped")
        set_config("enable_ca", "true")
        set_config("enable_vr", "false")
        # 주문 모드 초기화: manual(수동 확인), auto(자동 주문)
        set_config("planning_mode", "manual")
        logger.info("🛑 스케줄러 초기 설정을 'stopped'로 완료했습니다.")
    
    # [설정] 작업 지연 경고 억제를 위해 유예 시간을 30초로 설정
    scheduler = BlockingScheduler(job_defaults={'misfire_grace_time': 30})

    # 타임존 설정
    ny_tz = timezone('America/New_York')
    kr_tz = timezone('Asia/Seoul')

    # --- 준비 작업 (개장 3분 후) ---
    scheduler.add_job(
        lambda: job_market_prepare(market="US"),
        trigger=CronTrigger(hour=9, minute=35, day_of_week='mon-fri', timezone=ny_tz), # 09:30 개장 5분 후
        id='us_market_prepare',
        name='미국장 차수 전환 준비'
    )
    scheduler.add_job(
        lambda: job_market_prepare(market="KR"),
        trigger=CronTrigger(hour=9, minute=5, day_of_week='mon-fri', timezone=kr_tz), # 09:00 개장 5분 후
        id='kr_market_prepare', 
        name='한국장 차수 전환 준비'
    )

    # 1. 미국 시장 스케줄
    scheduler.add_job(
        lambda: job_market_open(market="US"),
        trigger=CronTrigger(hour=9, minute=35, day_of_week='mon-fri', timezone=ny_tz), # 09:30 개장 5분 후
        id='us_market_open',
        name='미국장 개장 후 주문'
    )

    # 2. 한국 시장 스케줄
    scheduler.add_job(
        lambda: job_market_open(market="KR"),
        trigger=CronTrigger(hour=9, minute=10, day_of_week='mon-fri', timezone=kr_tz), # 09:00 개장 10분 후
        id='kr_market_open',
        name='한국장 개장 후 주문'
    )
    
    # 한국 시장 개장 1시간 후 신규 진입 스케줄 추가
    scheduler.add_job(
        job_kr_market_late_open,
        trigger=CronTrigger(hour=10, minute=0, day_of_week='mon-fri', timezone=kr_tz),
        id='kr_market_late_open',
        name='한국장 1시간 후 신규진입'
    )

    scheduler.add_job(
        job_check_kr_holiday,
        trigger=CronTrigger(hour=8, minute=0, day_of_week='mon-fri', timezone=kr_tz), # 장 시작 전 확인
        id='kr_holiday_check',
        name='한국 휴장일 조회'
    )

    scheduler.add_job(
        job_check_us_holiday,
        trigger=CronTrigger(hour=7, minute=0, day_of_week='mon-fri', timezone=ny_tz), # [수정] 프리마켓 시작 시점 07:00 ET
        name='미국 휴장일 조회', id='us_holiday_check'
    )

    scheduler.add_job(
        job_kr_loc_simulation,
        trigger=CronTrigger(hour=15, minute=10, day_of_week='mon-fri', timezone=kr_tz),
        id='kr_loc_simulation',
        name='한국장 종료 전 LOC 모사 주문'
    )
    
    # [ADD] 미국 시장 프리마켓 시작 시 지정가 매도 주문
    scheduler.add_job(
        job_us_pre_market_limit_sells,
        trigger=CronTrigger(hour=7, minute=5, day_of_week='mon-fri', timezone=ny_tz), # [수정] 휴장 확인 5분 후인 07:05 ET
        id='us_pre_market_limit_sells',
        name='미국 프리마켓 지정가 매도 주문'
    )

    # 3. 심박(Heartbeat) 업데이트 (30초 간격) - 실시간성 향상
    scheduler.add_job(
        update_heartbeat,
        trigger=IntervalTrigger(seconds=30),
        id='heartbeat',
        name='Heartbeat'
    )

    # 환율 정보 업데이트 (매일 한국시간 20:00 - 미국 프리마켓 주문 제출 전)
    scheduler.add_job(
        job_update_exchange_rate,
        trigger=CronTrigger(hour=20, minute=0, timezone=kr_tz),
        id='periodic_exchange_rate',
        name='Exchange Rate Update (Daily 20:00 KST)'
    )

    # 4. DB 일일 백업 (매일 05:10 KST)
    scheduler.add_job(
        job_db_backup,
        trigger=CronTrigger(hour=5, minute=10, timezone=kr_tz),
        id='db_backup',
        name='Daily DB Backup'
    )

    # 5. 한국 시장 일일 보고서 (15:40 KST)
    scheduler.add_job(
        lambda: job_sync_and_report(market="KR"),
        trigger=CronTrigger(hour=15, minute=40, timezone=kr_tz),
        id='kr_daily_email',
        name='KR Market Daily Report Email'
    )

    # 한국 시장 15분 정기 동기화 (09:00 ~ 15:00 KST 내 매 15분)
    scheduler.add_job(
        lambda: job_hourly_market_sync(market="KR"),
        trigger=CronTrigger(hour='9-15', minute='*/15', day_of_week='mon-fri', timezone=kr_tz),
        id='kr_hourly_sync',
        name='KR Market Regular Sync (Every 15m KST)'
    )

    # 6. 미국 시장 일일 보고서 (16:10 ET)
    scheduler.add_job(
        lambda: job_sync_and_report(market="US"),
        trigger=CronTrigger(hour=16, minute=10, timezone=ny_tz),
        id='us_daily_email',
        name='US Market Daily Report Email'
    )

    # 미국 시장 15분 정기 동기화 (09:00 ~ 17:00 ET 내 매 15분)
    scheduler.add_job(
        lambda: job_hourly_market_sync(market="US"),
        trigger=CronTrigger(hour='9-17', minute='*/15', day_of_week='mon-fri', timezone=ny_tz),
        id='us_hourly_sync',
        name='US Market Regular Sync (Every 15m ET)'
    )

    # 7. 월간 투자 리포트 (매월 1일 08:00 KST)
    scheduler.add_job(
        job_send_monthly_report,
        trigger=CronTrigger(day=1, hour=8, minute=0, timezone=kr_tz),
        id='monthly_report_email',
        name='Monthly Investment Report Email'
    )

    # 5. 시스템 종료 플래그 감시 (10초 간격)
    scheduler.add_job(
        job_check_shutdown,
        trigger=IntervalTrigger(seconds=10),
        id='system_shutdown_check',
        name='System Shutdown Check'
    )

    # 4. (옵션) 프로그램 시작 직후 주문 로직은 '실행' 상태일 때만 수행하도록 변경
    # start_checks() -> status 확인 후 run_ca/vr 호출 함수로 분리 가능하나
    # 여기서는 스케줄러 루프 내에서 처리하도록 둠.
    # 만약 즉시 실행을 원하면 대시보드에서 "수동 실행" 버튼을 누르는 것이 안전함.

    logger.info("🚀 cavr 자동매매 스케줄러 프로세스가 시작되었습니다. (현재 상태: STOPPED)")
    send_telegram_message("🚀 <b>[시스템]</b> 스케줄러 프로세스 시작 (대기 모드)")
    
    # 시작 시 한국 휴장일 상태 즉시 갱신
    job_check_kr_holiday()
    
    # 시작 시 환율 정보 초기화 (오늘 이미 업데이트했다면 건너뜀)
    job_update_exchange_rate()
    
    # 시작 시점에 미국 장 운영 시간(07:00~18:00 ET) 내라면 미국 휴장 여부도 즉시 확인
    # 한국 시간 기준 오전 9시~오후 8시 사이(NY 20:00~07:00)의 불필요한 시도를 방지함.
    ny_now = datetime.now(timezone('America/New_York'))
    if 7 <= ny_now.hour < 18:
        job_check_us_holiday()

    # 5. 웹소켓 실시간 체결 감시 비활성화 (TOSS 웹소켓 미지원)
    logger.info("📡 TOSS Open API는 웹소켓을 지원하지 않으므로 감시 스레드를 시작하지 않습니다.")
    
    logger.info(f"Target Timezone: {ny_tz}")
    logger.info("Press Ctrl+C to exit")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("스케줄러를 종료합니다.")

if __name__ == "__main__":
    main()
