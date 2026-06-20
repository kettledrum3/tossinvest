import streamlit as st
import sqlite3
st.set_page_config(page_title="cavr Dashboard", layout="wide")

import pandas as pd
import os
import math
import sys
import time
import re
import plotly.graph_objects as go
from datetime import datetime, time as dtime, timedelta
import pytz
import logging
from logging.handlers import RotatingFileHandler
import html
from dotenv import load_dotenv, set_key


# 로거 설정
logger = logging.getLogger(__name__)

# 프로젝트 루트 경로 추가
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.append(PROJECT_ROOT)

# 공통 환경 변수 로드
load_dotenv(os.path.join(PROJECT_ROOT, "env", ".env"))

# --- 로깅 설정 및 핸들러 정의 ---
def setup_logging():
    log_dir = os.path.join(PROJECT_ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "error_log.txt")

    root_logger = logging.getLogger()
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    root_logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    err_handler = RotatingFileHandler(log_file, maxBytes=2*1024*1024, backupCount=5, encoding="utf-8")
    err_handler.setLevel(logging.ERROR)
    err_handler.setFormatter(formatter)
    root_logger.addHandler(err_handler)

    msg_log_file = os.path.join(log_dir, "message_log.txt")
    msg_handler = RotatingFileHandler(msg_log_file, maxBytes=2*1024*1024, backupCount=5, encoding="utf-8")
    msg_handler.setLevel(logging.INFO)
    msg_handler.setFormatter(formatter)
    root_logger.addHandler(msg_handler)
    
    st_handler = StreamlitLogHandler()
    st_handler.setFormatter(formatter)
    root_logger.addHandler(st_handler)

class StreamlitLogHandler(logging.Handler):
    def emit(self, record):
        msg = self.format(record)
        # 로컬 세션에도 남기지만, 이제 파일에서 읽어오므로 보조 수단으로 사용
        try:
            if "log_messages" not in st.session_state:
                st.session_state.log_messages = []
            st.session_state.log_messages.append(msg)
        except: pass

setup_logging()

# --- 모듈 임포트 (데이터베이스 관련 임포트를 호출부 위로 이동) ---
from core.database import (
    get_all_states_db, save_state_db, get_detailed_trade_history_db, migrate_sync_trades_db, get_order_history_db, sync_open_orders_db,
    load_state_db, init_db, get_config, set_config, delete_strategy_db, finish_strategy_db, get_finished_states_db, rename_strategy_db, sync_trade_history_db, migrate_manual_trades_db,
    save_ui_settings_db, load_ui_settings_db, cleanup_invalid_orders_db, cleanup_processed_orders_db,
    recalculate_all_profits_db
)

# --- 세션 상태 초기화 (AttributeError 방지) ---
init_db(run_maintenance=False) 

# --- 세션 상태 초기화 (AttributeError 방지) ---
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
if "temp_pass_mode" not in st.session_state:
    st.session_state.temp_pass_mode = False
if "log_messages" not in st.session_state:
    st.session_state.log_messages = []
if "planned_orders" not in st.session_state:
    st.session_state.planned_orders = []
if "last_activity" not in st.session_state:
    st.session_state.last_activity = time.time()
if "remembered_username" not in st.session_state:
    st.session_state.remembered_username = get_config("last_logged_in_user", "")
if "auth_trace" not in st.session_state:
    st.session_state.auth_trace = []
if "pending_market_selector_update" not in st.session_state:
    st.session_state.pending_market_selector_update = None
if "pending_symbol_update" not in st.session_state:
    st.session_state.pending_symbol_update = None
if "pending_alias_update" not in st.session_state:
    st.session_state.pending_alias_update = None

st.session_state.auth_trace.append(f"Run started at {datetime.now().strftime('%H:%M:%S')}")

# --- [FIX] 위젯이 렌더링되기 전에 대기 중인 UI 업데이트 프로세싱 ---
if st.session_state.pending_market_selector_update is not None:
    st.session_state["market_selector"] = st.session_state.pending_market_selector_update
    st.session_state.pending_market_selector_update = None

if st.session_state.pending_symbol_update is not None:
    m_key = f"{st.session_state.pending_symbol_update['m_code'].lower()}_symbol_sel"
    st.session_state[m_key] = st.session_state.pending_symbol_update['symbol']
    st.session_state.pending_symbol_update = None

if st.session_state.pending_alias_update is not None:
    m_key = f"{st.session_state.pending_alias_update['m_code'].lower()}_alias_sel"
    st.session_state[m_key] = st.session_state.pending_alias_update['alias']
    st.session_state.pending_alias_update = None

# 자동 로그아웃 체크 (1시간 = 3600초)
if st.session_state.authenticated:
    if time.time() - st.session_state.last_activity > 3600:
        st.session_state.authenticated = False
        st.warning("⚠️ 세션이 만료되었습니다. 다시 로그인해주세요.")
        time.sleep(2)
        st.rerun()
    else:
        # 활동 시간 업데이트
        st.session_state.last_activity = time.time()

# --- 모듈 임포트 (상단으로 이동) ---
from dotenv import load_dotenv, set_key
from core.cavr import CAConfig, CAState, CostAveragingEngine, VRConfig, VRState, ValueRebalancingEngine
from core.backtest import run_backtest
from core.fetch_data import update_ticker_data
from core.auth import update_user_email, generate_otp, verify_otp, check_login, register_user, update_password, reset_password_request
from core.notifier import send_telegram_message
from core.scheduler import run_ca_strategies, run_vr_strategies, job_update_exchange_rate # 스케줄러 로직 직접 호출을 위해 임포트
import glob
import json

# KIS 주문 유형 코드 매핑 (cavr.py와 동일하게 유지)
ORDER_TYPE_MAP = {
    "00": "지정가",
    "01": "시장가",
    "31": "최유리 지정가", # LOO (Limit On Open)
    "32": "최유리 시장가", # MOO (Market On Open)
    "33": "MOC",          # Market On Close
    "34": "LOC",          # Limit On Close
}

st.session_state.auth_trace.append(f"Auth Check: auth={st.session_state.authenticated}, temp={st.session_state.temp_pass_mode}")

if not st.session_state.authenticated:
    st.title("🔐 CAVR 시스템 로그인")
    auth_mode = st.radio("모드 선택", ["로그인", "회원가입"], horizontal=True)
    
    with st.container(border=True):
        if auth_mode == "로그인":
            with st.form("login_form", clear_on_submit=False):
                login_user = st.text_input("사용자 이름", value=st.session_state.remembered_username, key="login_username")
                password = st.text_input("비밀번호", type="password", key="login_pw")
                submit_btn = st.form_submit_button("로그인", use_container_width=True, type="primary")
            
            if submit_btn:
                st.session_state.auth_trace.append(f"Login button clicked for {login_user}")
                res = check_login(login_user, password)
                if res and res["status"] == "success":
                    st.session_state.remembered_username = login_user # 사용자명 기억
                    set_config("last_logged_in_user", login_user) # DB에 영구 기억
                    if res["is_temp"]:
                        st.session_state.auth_trace.append("Temporary password detected")
                        st.session_state.temp_pass_mode = True
                        st.session_state.user_email = res["email"]
                        # 임시 비번 모드 활성화를 위해 리런하지 않고 아래 비번 변경 UI를 보여줌
                        st.rerun() 
                    else:
                        st.session_state.auth_trace.append("Auth success, setting session state")
                        st.session_state.authenticated = True
                        st.session_state.user_email = res["email"]
                        st.session_state.username = res["username"]
                        st.session_state.last_activity = time.time() # 세션 시작 시간 설정
                        send_telegram_message(f"🔓 <b>{res['username']}</b>님이 대시보드에 접속했습니다.")
                        
                        # [UI 복원 로직 추가]
                        l_market, l_symbol, l_alias = load_ui_settings_db(res["email"])
                        
                        # [FIX] 시장 자동 활성화 로직 개선 (한국 우선순위, 휴장일 고려)
                        kr_now = datetime.now(pytz.timezone('Asia/Seoul'))
                        kr_opnd_yn = get_config("kr_market_opnd_yn", "Y")
                        us_opnd_yn = get_config("us_market_opnd_yn", "Y")
                        
                        selected_market_for_session = None
                        
                        # 1. 한국 시장 우선 감지 (오전 8시 ~ 오후 5시 사이이며 개장일인 경우)
                        if 8 <= kr_now.hour < 17 and kr_opnd_yn == "Y":
                            selected_market_for_session = "한국 시장"
                        # 2. 그 외 시간대이고 미국 시장이 개장일인 경우 (미국 프리마켓 포함)
                        elif us_opnd_yn == "Y":
                            selected_market_for_session = "미국 시장"
                        
                        # [보정] 만약 위 조건으로 결정되지 않았거나 기존 저장된 값이 있다면 저장값 우선순위 고려
                        if not selected_market_for_session:
                            selected_market_for_session = l_market if l_market else "미국 시장"
                        
                        st.session_state["market_selector"] = selected_market_for_session if selected_market_for_session else "미국 시장" # 기본값 설정
                        
                        if l_symbol: st.session_state["last_loaded_symbol"] = l_symbol
                        if l_alias: st.session_state["last_loaded_alias"] = l_alias

                        st.rerun()
                elif res and res["status"] == "locked":
                    # [ADD] 시장 자동 활성화 시간대 (한국 우선순위)
                    kr_now = datetime.now(pytz.timezone('Asia/Seoul'))
                    ny_now = datetime.now(pytz.timezone('America/New_York'))
                    if 8 <= kr_now.hour < 17: st.session_state["market_selector"] = "한국 시장"
                    elif 4 <= ny_now.hour < 20: st.session_state["market_selector"] = "미국 시장"
                    st.error(res["msg"])
                else:
                    st.error(res["msg"] if res else "계정 정보가 없습니다.")
            
            if st.button("🔑 비밀번호 재설정", use_container_width=True):
                if login_user and "@" in login_user: # 사용자 이름 칸에 이메일 형식을 입력한 경우
                    if reset_password_request(login_user):
                        st.info("이메일로 임시 비밀번호를 발송했습니다. 확인 후 로그인하세요.")
                    else: st.error("등록되지 않은 이메일입니다.")
                else: st.warning("비밀번호 재설정을 위해 '사용자 이름' 칸에 가입 시 사용한 이메일을 입력해주세요.")

            if st.session_state.temp_pass_mode:
                st.divider()
                st.subheader("🆕 새 비밀번호 설정")
                new_pw = st.text_input("새 비밀번호", type="password")
                confirm_pw = st.text_input("새 비밀번호 확인", type="password")
                if st.button("비밀번호 변경 및 완료"):
                    if new_pw != confirm_pw:
                        st.error("비밀번호가 일치하지 않습니다.")
                    else:
                        success, message = update_password(st.session_state.user_email, new_pw)
                        if success:
                            st.success(message)
                            st.session_state.temp_pass_mode = False
                            time.sleep(1)
                            st.rerun()
                        else:
                            st.error(message)
                            
        else:
            username = st.text_input("사용자 이름")
            email = st.text_input("이메일 주소") # 회원가입 시 이메일 입력 필드 추가
            if st.button("회원가입 신청", use_container_width=True):
                if email and username:
                    if register_user(username, email):
                        st.success("회원가입 완료! 이메일로 발송된 임시 비밀번호로 로그인하세요.")
                        time.sleep(2)
                    else: st.error("이미 등록된 이메일이거나 오류가 발생했습니다.")
                else: st.error("이름과 이메일을 모두 입력해주세요.")
    st.sidebar.expander("Debug Trace").write(st.session_state.auth_trace)
    st.stop() # 인증되지 않은 경우 여기서 실행 중단

st.session_state.auth_trace.append("Authentication Passed - Starting Main App")

# 로그아웃 버튼 (사이드바 하단용 미리 정의)
def handle_logout():
    st.session_state.authenticated = False
    st.rerun()

# --- 데이터 로드 함수 (시장 코드에 따라 동적 호출) ---
def get_market_tickers(m_code):
    """m_code: 'US' 또는 'KR'"""
    etf_data = {}
    try:
        file_name = f"ETF_list_{m_code.lower()}.txt"
        etf_path = os.path.join(PROJECT_ROOT, "env", file_name)
        if os.path.exists(etf_path):
            with open(etf_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.split('#')[0].strip()
                    if line:
                        # '122630 "KODEX 레버리지"' 또는 'TQQQ' 형식 처리
                        parts = line.split(None, 1)
                        ticker = parts[0].upper()
                        name = parts[1].strip().strip('"') if len(parts) > 1 else ticker
                        etf_data[ticker] = name
                # 만약 파일이 비어있거나 읽지 못했다면 기본값 반환
                if not etf_data:
                    if m_code == "US": return {"TQQQ": "TQQQ", "SOXL": "SOXL"}
                    if m_code == "KR": return {"005930": "삼성전자", "122630": "KODEX 레버리지"}
    except Exception as e:
        logger.error(f"[{m_code}] ETF 리스트 로드 실패: {e}")
        if m_code == "US": return {"TQQQ": "TQQQ", "SOXL": "SOXL"}
        if m_code == "KR": return {"005930": "삼성전자", "122630": "KODEX 레버리지"}
    
    # 파일 로드가 끝난 후에도 비어있으면 기본값 반환
    if not etf_data:
        if m_code == "US": return {"TQQQ": "TQQQ", "SOXL": "SOXL"}
        if m_code == "KR": return {"005930": "삼성전자", "122630": "KODEX 레버리지"}
    return etf_data

def check_market_active(m_code):
    """시장별 운영 시간 및 주말 여부 체크"""
    if m_code == "KR":
        tz = pytz.timezone('Asia/Seoul')
        open_time, close_time = dtime(7, 20), dtime(16, 40)
    else:
        tz = pytz.timezone('America/New_York')
        open_time, close_time = dtime(7, 0), dtime(18, 0) # [수정] US 시장 활성화 시간 조정 (07:00 ET ~ 18:00 ET)
    
    now = datetime.now(tz)
    if now.weekday() >= 5: # 토, 일
        return False, now
    return open_time <= now.time() <= close_time, now

def format_currency(val, m_code):
    """시장별 통화 포맷팅 (KR은 소수점 제거, US는 소수점 2자리, 공통 천단위 콤마)"""
    try:
        num_val = float(val)
        if m_code == "KR":
            return f"{int(num_val):,}"
        return f"{num_val:,.2f}"
    except (ValueError, TypeError):
        return str(val)

def format_ticker_display(t, m_code):
    """종목 코드와 이름을 보기 좋게 포맷팅"""
    m_tickers = get_market_tickers(m_code)
    name = m_tickers.get(t, t)
    if name and name != t:
        return f"{t} ({name})"
    return t

# --- 사이드바 스케줄러 제어 ---
def display_scheduler_control():
    st.sidebar.markdown("---")
    st.sidebar.subheader("🕹️ 스케줄러 제어")

    # 1. 심박 확인 (30초마다 독립적으로 갱신되는 프래그먼트)
    @st.fragment(run_every=30)
    def display_heartbeat():
        last_hb_str = get_config("scheduler_heartbeat")
        is_alive = False
        if last_hb_str:
            try:
                last_hb = datetime.strptime(last_hb_str, "%Y-%m-%d %H:%M:%S")
                # 2분 이내에 심박이 있으면 살아있는 것으로 간주
                if datetime.now() - last_hb < timedelta(minutes=2):
                    is_alive = True
            except:
                pass
                
        if is_alive:
            st.success(f"프로세스 동작 중 (Heartbeat: {last_hb_str.split()[-1]})")
        else:
            st.error("프로세스 응답 없음 (scheduler.py 실행 필요)")

    with st.sidebar:
        display_heartbeat()

    # 2. 동작 상태 제어 (ON/OFF)
    current_status = get_config("scheduler_status", "stopped")
    
    col_s1, col_s2 = st.sidebar.columns(2)
    if col_s1.button("▶ 실행", type="primary" if current_status=="stopped" else "secondary", use_container_width=True):
        set_config("scheduler_status", "running")
        st.rerun()
    if col_s2.button("⏹ 정지", type="primary" if current_status=="running" else "secondary", use_container_width=True):
        set_config("scheduler_status", "stopped")
        st.rerun()
        
    status_msg = "🟢 가동 중 (Running)" if current_status == "running" else "🔴 정지됨 (Stopped)"
    st.sidebar.info(f"상태: **{status_msg}**")

    # 2.1 주문 실행 모드 (Manual vs Auto)
    st.sidebar.markdown("---")
    plan_mode = get_config("planning_mode", "manual")
    new_plan_mode = st.sidebar.radio("주문 실행 모드", ["manual", "auto"], 
                                     index=0 if plan_mode == "manual" else 1,
                                     help="auto 설정 시 스케줄러가 장 시작 전 자동으로 주문을 전송합니다.")
    if new_plan_mode != plan_mode:
        set_config("planning_mode", new_plan_mode)
        st.rerun()

    # 3. 수동 실행 (Manual Trigger with Preview)
    st.sidebar.markdown("---")
    st.sidebar.subheader("⚡ 수동 주문 제어")
    
    # 주문 계획 세션 상태 초기화
    if "planned_orders" not in st.session_state:
        st.session_state.planned_orders = []

    # [1단계] 주문 계획 생성 (Preview)
    if st.sidebar.button("📋 주문 계획 생성 (Preview)", help="실제 주문 전에 매매 계획을 시뮬레이션하여 보여줍니다."):
        planned = []
        
        # CA 전략 프리뷰
        if get_config("enable_ca", "true") == "true":
            ca_states = get_all_states_db("CA", market=market_code, strategy_name=strategy_alias)
            for state in ca_states:
                sym = state.get('symbol')
                try:
                    # [수정] 현재 선택된 별칭(strategy_alias) 정보를 사용하여 Config 생성
                    cfg = CAConfig(symbol=sym, use_db=True, market=market_code, strategy_name=state.get('strategy_name'))
                    eng = CostAveragingEngine(cfg, broker=ActiveBroker)
                    # preview=True로 호출하여 계획만 가져옴
                    orders = eng.run_cycle(datetime.now(), check_existing_orders=True, preview=True)
                    if orders:
                        planned.extend(orders)
                except Exception as e:
                    st.sidebar.error(f"{sym} CA 계획 생성 오류: {e}")
        
        # VR 전략 프리뷰
        if get_config("enable_vr", "false") == "true":
            vr_states = get_all_states_db("VR", market=market_code, strategy_name=strategy_alias)
            for state in vr_states:
                sym = state.get('symbol')
                try:
                    cfg = VRConfig(symbol=sym, use_db=True, market=market_code, strategy_name=state.get('strategy_name'))
                    eng = ValueRebalancingEngine(cfg, broker=ActiveBroker)
                    # preview=True
                    # VR은 주기에 따라 contribution이 달라지는데, 수동 실행 시 기본적으로 0으로 둠 (혹은 오늘 날짜 체크)
                    orders = eng.place_daily_limit_orders(datetime.now(), contribution=0.0, is_cycle_start_day=False, check_existing_orders=True, preview=True)
                    if orders:
                        planned.extend(orders)
                except Exception as e:
                    st.sidebar.error(f"{sym} VR 계획 생성 오류: {e}")
        
        st.session_state.planned_orders = planned
        if not planned:
            st.sidebar.info("생성된 주문 계획이 없습니다. (조건 미충족 등)")
        else:
            st.sidebar.success(f"총 {len(planned)}건의 주문 계획이 생성되었습니다.")
            st.rerun() # 메인 화면 갱신을 위해 리런

    # 4. 전략별 활성화 설정 (토글)
    st.sidebar.markdown("---")
    st.sidebar.caption("자동매매 대상 전략")
    
    # CA 설정
    ca_enabled = get_config("enable_ca", "true") == "true"
    new_ca = st.sidebar.checkbox("CA 전략 실행", value=ca_enabled)
    if new_ca != ca_enabled:
        set_config("enable_ca", "true" if new_ca else "false")
        st.rerun()
        
    # VR 설정 (기본값 False)
    vr_enabled = get_config("enable_vr", "false") == "true"
    new_vr = st.sidebar.checkbox("VR 전략 실행", value=vr_enabled)
    if new_vr != vr_enabled:
        set_config("enable_vr", "true" if new_vr else "false")
        st.rerun()

# 사이드바 상태 요약 표시 함수
def display_sidebar_summary(m_display, m_code):
    st.sidebar.markdown("---")
    st.sidebar.subheader(f"📊 {m_display} 실전 투자 현황 (전략별)")
    
    # DB에서 모든 전략 상태 로드 (선택된 시장에 맞춰)
    states_ca = get_all_states_db("CA", market=m_code)
    states_vr = get_all_states_db("VR", market=m_code)
    states = states_ca + states_vr
    
    if not states:
        st.sidebar.caption("DB에 저장된 전략이 없습니다.")
        return

    for state in states:
        try:
            stype = state.get('strategy_type', '?')
            sym = state.get('symbol')
            alias = state.get('strategy_name', '')
            is_active = state.get('is_active', True)

            status_prefix = "🟢" if is_active else "⏸️"
            status_suffix = "" if is_active else " (정지)"
            
            summary_text = (
                f"**{status_prefix} {sym} ({stype}) - {alias}{status_suffix}**\n"
            )
            
            if stype == "CA":
                invested = state.get('avg_price', 0) * state.get('total_shares', 0)
                budget = state.get('cycle_budget', 1)
                progress = (invested / budget) * 100 if budget > 0 else 0
                summary_text += f"- T: {state.get('current_turn', 0):.1f} ({progress:.1f}%)\n"
                summary_text += f"- 보유: {state.get('total_shares', 0)}주"
            elif stype == "VR":
                v_curr = state.get('cycle_V', 0)
                pool = state.get('pool', 0)
                summary_text += f"- V: ${v_curr:,.0f}\n"
                summary_text += f"- Pool: ${pool:,.0f}"

            if is_active:
                st.sidebar.info(summary_text)
            else:
                st.sidebar.caption(summary_text)
            
        except Exception as e:
            st.sidebar.error(f"Error loading state: {e}")

with st.sidebar:
    st.title("🚀 CAVR 컨트롤 패널")
    active_market_display = st.selectbox("활성 시장 선택 (운영 대상)", ["미국 시장", "한국 시장"], key="market_selector")
    active_market_code = "US" if active_market_display == "미국 시장" else "KR"
    
    # --- Broker 및 Currency Symbol 정의 (한 번만) ---
    @st.cache_resource
    def get_active_broker_instance(market_code_param: str):
        from core.brokers.toss import TossBroker
        return TossBroker(market_code_param)

    ActiveBroker = get_active_broker_instance(active_market_code)
    currency_symbol = "₩" if active_market_code == "KR" else "$"
    # --- Broker 및 Currency Symbol 정의 끝 ---

    st.divider()
    
    def render_strategy_settings(m_code):
        m_lower = m_code.lower()
        m_curr = "₩" if m_code == "KR" else "$"
        
        st.subheader(f"📍 {m_code} 전략 설정")
        
        s_choice = st.radio("전략 선택", ["CA", "VR"], horizontal=True, key=f"{m_lower}_strat")

        # --- 종목 선택 (신규 입력 기능 추가) ---
        m_tickers = get_market_tickers(m_code)

        # "--- 신규 입력 ---" 옵션을 추가
        t_options_with_new = ["--- 신규 입력 ---"] + list(m_tickers.keys())
        
        def fmt_t_symbol(t):
            if t == "--- 신규 입력 ---":
                return t
            n = m_tickers.get(t, "")
            return f"{n} ({t})" if n and n != t else t
            
        # 세션 상태에서 이전 종목 인덱스 찾기
        prev_symbol = st.session_state.get("last_loaded_symbol")
        def_sym_idx = t_options_with_new.index(prev_symbol) if prev_symbol in t_options_with_new else 0
        selected_symbol_opt = st.selectbox("종목 선택", t_options_with_new, index=def_sym_idx, format_func=fmt_t_symbol, key=f"{m_lower}_symbol_sel")

        s_symbol = "" # s_symbol 초기화
        if selected_symbol_opt == "--- 신규 입력 ---":
            # [Requirement 2 & 3] 검색 및 확인 UI
            c_in1, c_in2 = st.columns([3, 1])
            s_symbol_input = c_in1.text_input("새 종목 코드 입력", value="", key=f"{m_lower}_new_symbol_input", placeholder="예: 122630").upper().strip()
            search_btn = c_in2.button("🔍 검색", key=f"{m_lower}_search_btn")
            
            if search_btn and s_symbol_input:
                with st.spinner("Toss API 종목 정보 조회 중..."):
                    try:
                        info = ActiveBroker.get_stock_info(s_symbol_input)
                        if info:
                            # 검색 성공 시 세션에 임시 저장
                            st.session_state[f"{m_lower}_pending_ticker"] = s_symbol_input
                            st.session_state[f"{m_lower}_pending_name"] = info.get("prdt_name", "이름 없음")
                            st.session_state[f"{m_lower}_pending_clsf"] = info.get("prdt_clsf_name", "분류 정보 없음")
                            st.session_state[f"{m_lower}_pending_type"] = info.get("ivst_prdt_type_cd_name", "유형 정보 없음")
                        else:
                            # [Requirement 3] 실패 시 메시지
                            st.error(f"❌ '{s_symbol_input}' 종목 정보를 찾을 수 없습니다. 코드를 다시 확인해 주세요.")
                            st.session_state[f"{m_lower}_pending_ticker"] = None
                    except Exception as e:
                        st.error(f"조회 중 오류 발생: {e}")
            
            # 검색 결과 확인 및 저장 버튼
            if st.session_state.get(f"{m_lower}_pending_ticker"):
                p_ticker = st.session_state[f"{m_lower}_pending_ticker"]
                p_name = st.session_state[f"{m_lower}_pending_name"]
                p_clsf = st.session_state.get(f"{m_lower}_pending_clsf", "")
                p_type = st.session_state.get(f"{m_lower}_pending_type", "")
                
                st.info(f"🔍 **종목 조회 성공**\n"
                        f"- 종목명: **{p_name}** ({p_ticker})\n"
                        f"- 분류: {p_clsf} / {p_type}")
                
                if st.button(f"✅ 위 종목을 {m_code} 리스트에 추가", key=f"{m_lower}_confirm_add", use_container_width=True, type="primary"):
                    if p_ticker in m_tickers:
                        st.warning(f"⚠️ '{p_ticker}' 종목은 이미 {m_code} 리스트에 존재합니다.")
                        st.session_state[f"{m_lower}_pending_ticker"] = None
                    else:
                        file_name = f"ETF_list_{m_code.lower()}.txt"
                        file_path = os.path.join(PROJECT_ROOT, "env", file_name)
                        
                        try:
                            # 파일의 마지막 문자가 개행인지 확인하여 포맷팅 최적화
                            has_content = False
                            last_char_is_newline = True
                            if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                                has_content = True
                                with open(file_path, "rb") as f:
                                    f.seek(-1, os.SEEK_END)
                                    last_char_is_newline = f.read(1) == b'\n'
                            
                            # [Requirement 2] 코드 "종목명" 형식으로 추가
                            prefix = "" if not has_content or last_char_is_newline else "\n"
                            entry = f'{prefix}{p_ticker} "{p_name}"\n'
                            
                            with open(file_path, "a", encoding="utf-8") as f:
                                f.write(entry)
                            
                            st.success(f"✨ {p_name} ({p_ticker}) 종목이 {file_name}에 추가되었습니다.")
                            st.session_state[f"{m_lower}_pending_ticker"] = None
                            time.sleep(1)
                            st.rerun()
                        except Exception as e:
                            st.error(f"파일 저장 중 오류: {e}")
            
            s_symbol = st.session_state.get(f"{m_lower}_pending_ticker", "")
        else:
            s_symbol = selected_symbol_opt
        s_mode = st.radio("실행 모드", ["실전 투자", "백테스트"], key=f"{m_lower}_mode", horizontal=True)

        # --- 저장된 전략 목록 불러오기 로직 추가 ---
        existing_states = get_all_states_db(strategy_type=s_choice, market=m_code)
        # 현재 종목에 해당하는 전략 필터링 및 최근 수정순 정렬
        symbol_states = [s for s in existing_states if s.get('symbol') == s_symbol]
        symbol_states.sort(key=lambda x: x.get('updated_at', ''), reverse=True)
        
        existing_aliases = [s.get('strategy_name', '') for s in symbol_states]
        
        alias_options = ["--- 신규 입력 ---"] + existing_aliases # 기존 별칭 선택 또는 신규 입력
        
        # 세션 상태에서 이전 별칭 인덱스 찾기
        prev_alias = st.session_state.get("last_loaded_alias")
        default_idx = alias_options.index(prev_alias) if prev_alias in alias_options else (1 if existing_aliases else 0)
        selected_alias_opt = st.selectbox("📁 저장된 전략 선택", alias_options, index=default_idx, key=f"{m_lower}_alias_sel")
        
        if selected_alias_opt == "--- 신규 입력 ---":
            s_alias = st.text_input("전략 별칭 (Alias)", value=f"{m_code}_전략_1", key=f"{m_lower}_alias")
        else:
            s_alias = selected_alias_opt
            st.info(f"선택됨: **{s_alias}**")
        
        # DB에서 기존 설정 로드
        d_state = load_state_db(s_symbol, s_choice, market=m_code, strategy_name=s_alias) if s_mode == "실전 투자" and s_symbol else None

        # --- [통합] 백테스트 전용 설정 ---
        f_days, f_rate, t_rate = 300, 0.0007, 0.0
        if s_mode == "백테스트":
            col_bt1, col_bt2 = st.columns(2)
            f_days = col_bt1.number_input("데이터 기간 (일)", value=300, min_value=30, key=f"{m_lower}_{widget_suffix}_bt_days")
            f_rate = col_bt2.number_input("수수료 (%)", value=0.07, step=0.01, format="%.4f", key=f"{m_lower}_{widget_suffix}_bt_fee") / 100.0
            if m_code == "KR":
                t_rate = st.number_input("매도 세금 (%)", value=0.25, step=0.01, format="%.4f", key=f"{m_lower}_{widget_suffix}_bt_tax") / 100.0
        
        if s_choice == "CA":
            # [CA 전용] 전략 할당 예수금만 표시
            def_pool = d_state.get('pool', 0.0) if d_state else 0.0
            widget_suffix = f"{s_symbol}_{s_alias}"
            s_pool = st.number_input(f"전략 할당 예수금 ({m_curr})", value=float(def_pool), key=f"{m_lower}_{widget_suffix}_pool", help="이 전략에서 사용할 현금 한도입니다.")

            def_ub = d_state.get('unit_buy_amount', 250.0) if d_state else 250.0
            def_a = d_state.get('a_default', 40) if d_state else 40
            def_tp_val = d_state.get('target_profit_pct', 0.07 if m_code == "KR" else 0.10) if d_state else (0.07 if m_code == "KR" else 0.10)
            def_qs = d_state.get('use_quarter_stop', True) if d_state else True
            def_ver = d_state.get('version', "V2.2") if d_state else "V2.2"
            
            s_ver = st.radio("버전 선택", ["V2.2", "V4.0"], index=0 if def_ver == "V2.2" else 1, horizontal=True, key=f"{m_lower}_{widget_suffix}_ca_ver")
            u_buy = st.number_input(f"1회 매수 금액 ({m_curr})", value=float(def_ub), key=f"{m_lower}_{widget_suffix}_ca_ub")
            t_profit = st.slider("목표 수익 (%)", 5, 30, int(def_tp_val * 100), key=f"{m_lower}_{widget_suffix}_ca_tp") / 100.0
            a_val = st.number_input("분할 횟수 (a)", 20, 60, int(def_a), key=f"{m_lower}_{widget_suffix}_ca_a")
            q_stop = st.checkbox("쿼터 손절 사용", value=def_qs, key=f"{m_lower}_{widget_suffix}_ca_qs")
            
            return {
                "mode": s_mode, "strategy": s_choice, "alias": s_alias, "symbol": s_symbol, "pool": s_pool, "version": s_ver,
                "unit_buy": u_buy, "target_profit": t_profit, "a_default": a_val, "use_quarter_stop": q_stop,
                "initial_cash": s_pool, "fetch_days": f_days, "fee_rate": f_rate, "tax_rate": t_rate
            }
        else:
            # [VR 전용] 운용 자본금만 표시 (예수금은 내부 상태 유지)
            def_budget = d_state.get('initial_budget', 10000.0) if d_state else 10000.0
            def_pool = d_state.get('pool', 0.0) if d_state else 0.0
            widget_suffix = f"{s_symbol}_{s_alias}"
            i_cash = st.number_input(f"운용 자본금 ({m_curr})", value=float(def_budget), key=f"{m_lower}_{widget_suffix}_budget", help="밸류리밸런싱 운용 자본금(VR용).")

            def_pa = d_state.get('periodic_accumulation', 250.0) if d_state else 250.0
            def_g = d_state.get('G', 10.0) if d_state else 10.0
            def_b = (d_state.get('band_high_pct', 115.0) if d_state else 115.0) - 100.0
            
            g_val = st.slider("G 값", 10, 30, int(def_g), key=f"{m_lower}_{widget_suffix}_vr_g")
            b_pct = st.slider("밴드 (%)", 10, 30, int(def_b), key=f"{m_lower}_{widget_suffix}_vr_b")
            
            v_type_disp = st.selectbox("투자 방식", ["적립식", "거치식", "인출식"], key=f"{m_lower}_{widget_suffix}_vr_type")
            type_map = {"적립식": "accumulation", "거치식": "deferment", "인출식": "withdrawal"}
            v_type = type_map[v_type_disp]
            
            p_amt = st.number_input(f"주기별 적립/인출액 ({m_curr})", value=float(def_pa), key=f"{m_lower}_{widget_suffix}_vr_pa")
            v_freq = st.selectbox("주기", ["매주 금요일", "격주 금요일 (2주)", "4주마다 금요일"], index=1, key=f"{m_lower}_{widget_suffix}_vr_freq")
            
            return {
                "mode": s_mode, "strategy": s_choice, "alias": s_alias, "symbol": s_symbol, "pool": def_pool,
                "g_value": g_val, "band_pct": b_pct, "periodic_amt": p_amt,
                "initial_cash": i_cash, "investment_type": v_type, "freq": v_freq, "invest_type_disp": v_type_disp,
                "fetch_days": f_days, "fee_rate": f_rate, "tax_rate": t_rate
            }

    # [수정] 사이드바 탭을 제거하고 활성 시장에 맞는 설정만 렌더링하여 UI 혼선 방지
    active_settings = render_strategy_settings(active_market_code)
    
    market_code = active_market_code
    mode = active_settings["mode"]
    strategy_choice = active_settings["strategy"]
    strategy_alias = active_settings["alias"]
    symbol = active_settings["symbol"]
    # [점검] symbol 변수에 "종목명 (코드)" 형태의 포맷팅된 문자열이 들어있는 경우, 괄호 안의 순수 코드만 추출
    if isinstance(symbol, str) and "(" in symbol and ")" in symbol:
        # [FIX] "Ticker (Name)" 형식에서 첫 번째 파트(Ticker)만 추출하도록 수정
        symbol = symbol.split('(')[0].strip()
    
    allocated_pool = active_settings["pool"]
    
    # [UI 상태 저장] 선택이 변경될 때마다 DB 업데이트
    save_ui_settings_db(st.session_state.user_email, active_market_display, symbol, strategy_alias)
    initial_cash = active_settings["initial_cash"]
    fetch_days = active_settings.get("fetch_days", 300)
    fee_rate = active_settings.get("fee_rate", 0.0007)
    tax_rate = active_settings.get("tax_rate", 0.0)

    if strategy_choice == "CA":
        unit_buy = active_settings["unit_buy"]
        target_profit = active_settings["target_profit"]
        ca_version = active_settings["version"]
        a_default = active_settings["a_default"]
        use_quarter_stop = active_settings["use_quarter_stop"]
    else:
        vr_g_value = active_settings["g_value"]
        vr_band_pct = active_settings["band_pct"]
        vr_periodic_amt = active_settings["periodic_amt"]
        vr_investment_type = active_settings["investment_type"]
        vr_freq = active_settings["freq"]
        vr_invest_type_disp = active_settings["invest_type_disp"]

    st.divider()

    # 스케줄러 제어 및 요약 정보 호출
    display_scheduler_control()
    display_sidebar_summary(active_market_display, market_code)

    st.divider()
    # [요청] 전략 관리 버튼 구성
    col_b1, col_b2 = st.columns(2)
    save_run_btn = col_b1.button("💾 신규전략 저장 및 실행", use_container_width=True, type="primary")
    load_strategy_btn = col_b2.button("📂 저장된 전략 불러오기", use_container_width=True)
    
    col_b3, col_b4 = st.columns(2)
    update_strategy_btn = col_b3.button("✏️ 전략 업데이트", use_container_width=True)
    delete_strategy_btn = col_b4.button("🗑️ 전략 삭제", use_container_width=True)

    if load_strategy_btn:
        st.sidebar.info("🔄 최신 전략 데이터를 로드합니다.")
        time.sleep(0.5)
        st.rerun()

    # --- 전략 관리 버튼 로직 수정 ---
    # 전략 이름 변경 버튼 로직
    if strategy_alias and strategy_alias != "--- 신규 입력 ---":
        st.sidebar.markdown("---")
        if st.sidebar.checkbox("📝 전략 이름 변경 모드 활성화", key="rename_mode_toggle"):
            st.sidebar.subheader("전략 이름 변경")
            new_alias_input = st.sidebar.text_input("새 전략 별칭 입력", value=strategy_alias, key="new_alias_for_rename")
            rename_strategy_confirm_btn = st.sidebar.button("🚀 변경 확정", use_container_width=True)

            if 'rename_strategy_confirm_btn' in locals() and rename_strategy_confirm_btn:
                if not new_alias_input or new_alias_input == "--- 신규 입력 ---":
                    st.sidebar.error("유효한 새 전략 별칭을 입력해주세요.")
                elif new_alias_input == strategy_alias:
                    st.sidebar.info("기존 별칭과 동일합니다. 변경할 필요가 없습니다.")
                else:
                    success, msg = rename_strategy_db(symbol, strategy_choice, market_code, strategy_alias, new_alias_input)
                    if success:
                        st.sidebar.success(msg)
                        time.sleep(1)
                        st.rerun()
                    else:
                        st.sidebar.error(msg)

    if save_run_btn:
        if mode == "실전 투자":
            # 중복 전략 확인
            existing_state = load_state_db(symbol, strategy_choice, market=market_code, strategy_name=strategy_alias)
            if existing_state:
                st.sidebar.error(f"'{strategy_alias}' 전략이 이미 존재합니다. 다른 이름을 사용하거나 '전략 이름 변경'을 이용하세요.")
                st.stop()
            # 현재 입력된 파라미터로 새로운 전략 객체 생성
            if strategy_choice == "CA":
                new_state = CAState(
                    symbol=symbol,
                    strategy_type="CA",
                    version=ca_version,
                    strategy_name=strategy_alias,
                    market=market_code,
                    cycle_budget=initial_cash, # 사이드바 초기자본 값 사용
                    pool=allocated_pool,       # 할당 예수금 저장
                    unit_buy_amount=unit_buy,
                    a_default=a_default,
                    # 나머지 필드는 기본값 사용
                )
            elif strategy_choice == "VR":
                new_state = VRState(
                    symbol=symbol,
                    strategy_type="VR",
                    strategy_name=strategy_alias,
                    market=market_code,
                    initial_budget=initial_cash, # 사이드바 초기자본 값 사용
                    pool=allocated_pool,       # 할당 예수금 저장
                    periodic_accumulation=vr_periodic_amt,
                    # 나머지 필드는 기본값 사용
                )
            
            # DB에 저장
            save_state_db(new_state, market=market_code, strategy_name=strategy_alias)
            # 스케줄러 가동 상태로 전환
            set_config("scheduler_status", "running")
            st.sidebar.success(f"✅ '{strategy_alias}' 저장 및 스케줄러 실행 시작!")
            time.sleep(1)
            st.rerun()
        else:
            st.sidebar.warning("백테스트 모드에서는 전략을 저장할 수 없습니다.")
    if update_strategy_btn:
        if mode == "실전 투자":
            # 기존 전략 로드
            existing_state_data = load_state_db(symbol, strategy_choice, market=market_code)
            if existing_state_data:
                # 현재 사이드바 파라미터로 업데이트
                if strategy_choice == "CA":
                    state_obj = CAState(**existing_state_data)
                    state_obj.strategy_name = strategy_alias
                    state_obj.version = ca_version
                    state_obj.cycle_budget = initial_cash
                    state_obj.pool = allocated_pool
                    state_obj.unit_buy_amount = unit_buy
                    state_obj.a_default = a_default
                    state_obj.target_profit_pct = target_profit
                    state_obj.use_quarter_stop = use_quarter_stop
                elif strategy_choice == "VR":
                    state_obj = VRState(**existing_state_data)
                    state_obj.strategy_name = strategy_alias
                    state_obj.initial_budget = initial_cash
                    state_obj.pool = allocated_pool
                    state_obj.periodic_accumulation = vr_periodic_amt
                    state_obj.G = vr_g_value
                    state_obj.band_low_pct = 100.0 - vr_band_pct
                    state_obj.band_high_pct = 100.0 + vr_band_pct
                    state_obj.investment_type = vr_investment_type
                                    
                save_state_db(state_obj, market=market_code, strategy_name=strategy_alias)
                st.sidebar.success(f"✅ '{strategy_alias}' 전략이 업데이트되었습니다.")
                time.sleep(1)
                st.rerun()
            else:
                st.sidebar.warning("업데이트할 기존 전략을 찾을 수 없습니다. '새 전략 저장'을 이용하세요.")
        else:
            st.sidebar.warning("백테스트 모드에서는 전략을 업데이트할 수 없습니다.")
    if delete_strategy_btn:
        if st.sidebar.button("정말 삭제하시겠습니까?", key="confirm_delete_btn"):
            delete_strategy_db(symbol, strategy_choice, market=market_code, strategy_name=strategy_alias)
            st.sidebar.success(f"🗑️ '{strategy_alias}' 전략이 삭제되었습니다.")
            time.sleep(1)
            st.rerun()
        else:
            st.sidebar.warning("🗑️ '전략 삭제' 버튼을 다시 누르면 영구 삭제됩니다. (취소하려면 다른 버튼 클릭)")

    # '취소' 버튼은 별도로 구현하지 않고, 다른 버튼을 누르거나 새로고침하는 것으로 대체
    # if cancel_action:
    #     st.sidebar.info("✖️ '취소'되었습니다. (현재는 새로고침과 동일)")
    #     time.sleep(0.5)
    #     st.rerun()


# --- Sidebar ---
# 사이드바 상태 요약 표시 함수
# [중복 제거] 정의는 상단에 이미 존재하므로 삭제

# --- Main Content ---
# [중복 제거] 인증 섹션은 이미 최상단으로 이동함

# 로그아웃 버튼 (사이드바 하단)
if st.sidebar.button("🔓 로그아웃"):
    handle_logout()

st.title(f"🚀 {mode} - {strategy_choice} 전략")

# --- 시장별 현황 탭 --- (사이드바 선택에 따라 강조 및 필터링)
# [수정] 활성 시장에 따라 탭 순서를 동적으로 변경하여 첫 번째 탭이 항상 활성 시장을 가리키도록 함
if market_code == "KR":
    tab_names = [f"🇰🇷 한국 주식 시장 현황 ✅", f"🇺🇸 미국 주식 시장 현황", "🏁 종료된 전략 내역", "📈 손익분석", "⚙️ 설정"]
    tabs = st.tabs(tab_names)
    tab_kr, tab_us, tab_finished, tab_analysis, tab_settings = tabs[0], tabs[1], tabs[2], tabs[3], tabs[4]
else:
    tab_names = [f"🇺🇸 미국 주식 시장 현황 ✅", f"🇰🇷 한국 주식 시장 현황", "🏁 종료된 전략 내역", "📈 손익분석", "⚙️ 설정"]
    tabs = st.tabs(tab_names)
    tab_us, tab_kr, tab_finished, tab_analysis, tab_settings = tabs[0], tabs[1], tabs[2], tabs[3], tabs[4]


def render_market_tab(m_code):
    # 해당 시장의 종목 리스트 미리 로드 (명칭 표시용)
    tickers_map = get_market_tickers(m_code)
    states = get_all_states_db(market=m_code)
    if not states: # 해당 시장에 등록된 전략이 없으면
        st.info(f"{m_code} 시장에 등록된 전략이 없습니다.")
    else:
        for s in states:
            stype = s.get('strategy_type')
            sym = s.get('symbol')
            disp_name = s.get('strategy_name', '')
            with st.expander(f"[{stype}] {disp_name} - {format_ticker_display(sym, m_code)}"):
                is_active = s.get('is_active', True)
                ver = s.get('version', 'V2.2') if stype == "CA" else ""
                ver_text = f" ({stype} {ver})" if ver else ""
                status_icon = "🟢" if is_active else "⏸️"
                status_label = "운영 중" if is_active else "일시 정지됨"
                st.write(f"상태: {status_icon} **{status_label}**{ver_text}")

                cur_sym = "₩" if m_code == "KR" else "$"
                if stype == "CA":
                    st.write(f"T: **{s.get('current_turn', 0):.1f}** / 평단: {cur_sym}{format_currency(s.get('avg_price', 0), m_code)} / 보유: {s.get('total_shares', 0)}주")
                    st.write(f"할당 예수금: {cur_sym}{format_currency(s.get('pool', 0), m_code)} / 분할수: {s.get('a_default', 40)} / 1회매수금: {cur_sym}{format_currency(s.get('unit_buy_amount', 0), m_code)} / 목표수익률: {s.get('target_profit_pct', 0.1)*100:.1f}%")
                else:
                    st.write(f"목표V: {cur_sym}{format_currency(s.get('cycle_V', 0), m_code)} / Pool: {cur_sym}{format_currency(s.get('pool', 0), m_code)}")
                    st.write(f"운용 자본금: {cur_sym}{format_currency(s.get('initial_budget', 0), m_code)} / G값: {s.get('G', 10)} / 적립액: {cur_sym}{format_currency(s.get('periodic_accumulation', 0), m_code)}")

                # [ADD] 전략 선택 버튼 (사이드바 연동)
                if st.button("🎯 이 전략을 작업 대상으로 선택", key=f"sel_target_{m_code}_{stype}_{sym}_{disp_name}", use_container_width=True, type="primary"):
                    st.session_state["pending_market_selector_update"] = "미국 시장" if m_code == "US" else "한국 시장"
                    st.session_state["pending_symbol_update"] = {"m_code": m_code, "symbol": sym}
                    st.session_state["pending_alias_update"] = {"m_code": m_code, "alias": disp_name}
                    st.session_state["last_loaded_symbol"] = sym
                    st.session_state["last_loaded_alias"] = disp_name
                    st.rerun()

                col_t1, col_t2, col_t3 = st.columns(3)

                # 일시정지 / 다시 시작 버튼
                toggle_label = "⏸️ 전략 일시 정지" if is_active else "▶️ 전략 다시 시작"
                if col_t1.button(toggle_label, key=f"toggle_{m_code}_{stype}_{sym}_{disp_name}", use_container_width=True):
                    raw_state = load_state_db(sym, stype, market=m_code, strategy_name=disp_name)
                    if raw_state:
                        raw_state['is_active'] = not is_active
                        state_obj = CAState(**raw_state) if stype == "CA" else VRState(**raw_state)
                        save_state_db(state_obj, market=m_code, strategy_name=disp_name)
                        st.rerun()

                # 전략 종료 버튼 (History로 이동)
                if col_t2.button(f"🏁 전략 종료", key=f"finish_{m_code}_{stype}_{sym}_{disp_name}", use_container_width=True, help="전략을 완료하고 이력 탭으로 이동시킵니다."):
                    # [ADD] 최종 리포트 생성 및 전송
                    try:
                        _, all_th_rows = get_detailed_trade_history_db(sym, market=m_code)
                        # 해당 별칭의 매도 내역 필터링
                        strat_sells = [r for r in all_th_rows if r[4] == disp_name and r[5] == 'SELL']
                        
                        total_profit = sum(float(r[14] or 0) for r in strat_sells)
                        sell_count = len(strat_sells)
                        
                        # 총 매수액 (간단히 매도된 원금 합산 또는 전체 매수 합산)
                        strat_buys = [r for r in all_th_rows if r[4] == disp_name and r[5] == 'BUY']
                        total_bought = sum(float(r[10] or 0) for r in strat_buys)
                        
                        report_msg = (
                            f"🏁 <b>[전략 종료 리포트]</b>\n"
                            f"종목: {sym} ({disp_name})\n"
                            f"시장: {m_code}\n"
                            f"전략: {stype}\n"
                            f"--------------------\n"
                            f"총 매도 횟수: {sell_count}회\n"
                            f"총 누적 수익: {cur_sym}{format_currency(total_profit, m_code)}\n"
                            f"총 투입 원금: {cur_sym}{format_currency(total_bought, m_code)}\n"
                            f"최종 수익률: {(total_profit/total_bought*100 if total_bought > 0 else 0):.2f}%"
                        )
                        send_telegram_message(report_msg)
                    except Exception as e:
                        logger.error(f"리포트 생성 실패: {e}")

                    finish_strategy_db(sym, stype, m_code, disp_name)
                    st.success(f"전략이 종료되었습니다: {disp_name}")
                    time.sleep(1)
                    st.rerun()

                # [ADD] 전략 이름 변경 버튼
                if col_t3.button("📝 이름 변경", key=f"rename_btn_{m_code}_{stype}_{sym}_{disp_name}", use_container_width=True):
                    st.session_state[f"show_rename_{disp_name}"] = True

                if st.session_state.get(f"show_rename_{disp_name}", False):
                    with st.container(border=True):
                        new_name = st.text_input("새 전략 별칭 입력", value=disp_name, key=f"new_name_val_{disp_name}")
                        c_rn1, c_rn2 = st.columns(2)
                        if c_rn1.button("🚀 변경 확정", key=f"conf_rn_{disp_name}", type="primary", use_container_width=True):
                            success, msg = rename_strategy_db(sym, stype, m_code, disp_name, new_name)
                            if success:
                                st.success(msg)
                                time.sleep(1)
                                st.rerun()
                            else:
                                st.error(msg)
                        if c_rn2.button("취소", key=f"can_rn_{disp_name}", use_container_width=True):
                            st.session_state[f"show_rename_{disp_name}"] = False
                            st.rerun()

                # 개별 전략 삭제 버튼
                if st.button(f"🗑️ 전략 영구 삭제", key=f"del_tab_{m_code}_{stype}_{sym}_{disp_name}", use_container_width=True):
                    delete_strategy_db(sym, stype, market=m_code, strategy_name=disp_name)
                    st.success(f"성공적으로 삭제되었습니다: {sym} ({disp_name})")
                    time.sleep(1)
                    st.rerun()
                    
with tab_analysis:
    st.header("🔍 실현 손익 분석")
    st.caption("DB에 기록된 거래 내역을 기반으로 다양한 통계를 제공합니다.")
    
    # 1. 필터 구성
    col_f1, col_f2, col_f3 = st.columns(3)
    analysis_market = col_f1.selectbox("시장 선택", ["전체", "KR", "US"])
    analysis_range = col_f2.selectbox("분석 단위", ["일별", "주간별", "월간별", "년간별"])
    
    # 데이터 로드
    cols, rows = get_detailed_trade_history_db(None)
    if not rows:
        st.info("분석할 거래 내역이 없습니다.")
    else:
        usd_krw = float(get_config("USDKRW", "1350.00"))
        df_all = pd.DataFrame(rows, columns=cols)
        df_all['date'] = pd.to_datetime(df_all['date'], format='mixed')
        
        # 매도 내역만 필터링
        df_sells = df_all[df_all['side'] == 'SELL'].copy()
        
        # [ADD] "전체" 선택 시 환율 반영 로직
        if analysis_market == "전체":
            # US 시장의 수익금을 원화로 환산 (임시 계산용)
            df_sells.loc[df_sells['market'] == 'US', 'realized_profit'] *= usd_krw
            analysis_currency = "₩"
        else:
            analysis_currency = "₩" if analysis_market == "KR" else "$"

        if analysis_market != "전체":
            df_sells = df_sells[df_sells['market'] == analysis_market]
            
        if df_sells.empty:
            st.warning("선택한 조건의 매도(실현손익) 내역이 없습니다.")
        else:
            # 2. 리샘플링 (시계열 분석)
            freq_map = {"일별": "D", "주간별": "W", "월간별": "MS", "년간별": "Y"}
            df_resampled = df_sells.set_index('date').resample(freq_map[analysis_range])['realized_profit'].sum().reset_index()
            
            # 3. 그래프 시각화
            fig_profit = go.Figure()
            fig_profit.add_trace(go.Bar(
                x=df_resampled['date'], 
                y=df_resampled['realized_profit'],
                marker_color='royalblue',
                name='실현 손익'
            ))
            
            # 누적 손익 라인 추가
            df_resampled['cumulative_profit'] = df_resampled['realized_profit'].cumsum()
            fig_profit.add_trace(go.Scatter(
                x=df_resampled['date'], 
                y=df_resampled['cumulative_profit'],
                line=dict(color='firebrick', width=3),
                name='누적 손익',
                yaxis='y2'
            ))
            
            market_suffix = f" (환율 1$: ₩{usd_krw:,.2f} 반영)" if analysis_market == "전체" else ""
            fig_profit.update_layout(
                title=f"{analysis_range} 수익 추이{market_suffix}",
                xaxis_title="기간",
                yaxis_title=f"실현 손익 ({analysis_currency})",
                yaxis2=dict(title=f'누적 손익 ({analysis_currency})', overlaying='y', side='right'),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                hovermode="x unified"
            )
            st.plotly_chart(fig_profit, use_container_width=True)
            
            # 4. 종목별 비중 파이 차트
            st.divider()
            col_c1, col_c2 = st.columns(2)
            
            with col_c1:
                st.subheader("📦 종목별 수익 비중")
                # [수정] 종목 식별을 쉽게 하기 위해 코드 (종목명) 형태로 라벨 생성
                df_sells['display_name'] = df_sells.apply(lambda x: format_ticker_display(x['symbol'], x['market']), axis=1)
                df_symbol = df_sells.groupby('display_name')['realized_profit'].sum().reset_index()
                fig_pie = go.Figure(data=[go.Pie(labels=df_symbol['display_name'], values=df_symbol['realized_profit'], hole=.3)])
                st.plotly_chart(fig_pie, use_container_width=True)
            
            with col_c2:
                st.subheader("🏷️ 전략별 수익 비중")
                df_strat = df_sells.groupby('strategy')['realized_profit'].sum().reset_index()
                fig_strat = go.Figure(data=[go.Pie(labels=df_strat['strategy'], values=df_strat['realized_profit'], hole=.3)])
                st.plotly_chart(fig_strat, use_container_width=True)

            # [신규] 환율 변동 추이 그래프
            st.divider()
            st.subheader("💹 환율 변동 추이 (USD/KRW)")
            try:
                # 분석 기간에 맞춰 환율 이력 조회 (기본 최근 30일)
                end_dt = datetime.now().strftime("%Y%m%d")
                start_dt = (datetime.now() - timedelta(days=60)).strftime("%Y%m%d")
                
                # ActiveBroker가 US가 아닐 수 있으므로 명시적 생성 또는 확인
                hist_broker = ActiveBroker if market_code == "US" else get_active_broker_instance("US")
                ex_history = hist_broker.get_exchange_rate_history(start_dt, end_dt)
                
                if ex_history:
                    df_ex = pd.DataFrame(ex_history)
                    df_ex['date'] = pd.to_datetime(df_ex['date'])
                    df_ex = df_ex.sort_values('date')
                    
                    fig_ex = go.Figure()
                    fig_ex.add_trace(go.Scatter(
                        x=df_ex['date'], y=df_ex['rate'],
                        mode='lines+markers',
                        name='USD/KRW',
                        line=dict(color='mediumseagreen', width=2)
                    ))
                    fig_ex.update_layout(
                        title="최근 환율 변동 추이",
                        xaxis_title="날짜", yaxis_title="환율 (₩)",
                        hovermode="x unified"
                    )
                    st.plotly_chart(fig_ex, use_container_width=True)
            except Exception as e:
                st.caption(f"환율 추이를 불러올 수 없습니다: {e}")

with tab_us:
    render_market_tab("US")
with tab_kr:
    render_market_tab("KR")
with tab_finished:
    st.subheader("🏁 종료된 전략 이력 관리")
    finished_rows = get_finished_states_db()
    if not finished_rows:
        st.info("아직 종료된 전략이 없습니다.")
    else:
        f_data = []
        # 정확한 실현 손익 합산을 위해 전체 거래 내역을 로드합니다.
        _, all_th_rows = get_detailed_trade_history_db(None)

        for row in finished_rows:
            sym, stype, m_code, alias, s_json, f_at = row
            cur_sym = "₩" if m_code == "KR" else "$"
            
            # 해당 전략 별칭(alias)에 해당하는 모든 매도(SELL) 거래의 실현 손익을 합산합니다.
            # r[1]: symbol, r[3]: market, r[4]: strategy_name, r[5]: side, r[14]: realized_profit
            strat_trades = [r for r in all_th_rows if r[1] == sym and r[3] == m_code and r[4] == alias]
            
            total_buy = sum(float(r[10] or 0) for r in strat_trades if r[5] == 'BUY')
            total_sell = sum(float(r[10] or 0) for r in strat_trades if r[5] == 'SELL')
            realized_sum = total_sell - total_buy
            
            p_rate = (realized_sum / total_buy * 100) if total_buy > 0 else 0
            
            profit_info = f"수익: {cur_sym}{format_currency(realized_sum, m_code)} ({p_rate:+.2f}%)"
            
            f_data.append({
                "종료일시": f_at, "시장": m_code, "종목": sym, "전략": stype, "별칭": alias, 
                "매수원금": total_buy, "매도금액": total_sell, "실현손익": realized_sum, "수익률": p_rate,
                "상태요약": profit_info,
                "s_json": s_json
            })
        
        df_fin = pd.DataFrame(f_data)
        st.dataframe(df_fin[["종료일시", "시장", "종목", "전략", "별칭", "상태요약"]], use_container_width=True)
        
        # 상세 내역 조회를 위한 선택기
        st.divider()
        st.subheader("🔍 종료된 전략 상세 내역 조회")
        target_fin = st.selectbox("조회할 종료된 전략 선택", 
                                  options=[f"{r['별칭']} ({r['종목']})" for r in f_data],
                                  key="fin_detail_selector")
        
        if target_fin:
            # 별칭 추출
            sel_alias = target_fin.split(" (")[0]
            sel_row = next(r for r in f_data if r['별칭'] == sel_alias)
            sel_sym = sel_row['종목']
            sel_m = sel_row['시장']
            
            # 실현 손익 및 거래 내역 표시
            st.info(f"📝 **{sel_alias}** 전략의 기록을 불러옵니다.")
            
            cols_h, rows_h = get_detailed_trade_history_db(sel_sym, market=sel_m)
            if rows_h:
                df_h = pd.DataFrame(rows_h, columns=cols_h)
                # 해당 별칭 내역만 필터링 (strategy_name 컬럼 기준)
                df_h_filtered = df_h[df_h['strategy_name'] == sel_alias].copy()
                
                # [상세 통계 제시]
                df_sells = df_h_filtered[df_h_filtered['side'] == 'SELL'].copy()
                df_buys = df_h_filtered[df_h_filtered['side'] == 'BUY']
                
                t_buy_amt = df_buys['total_amount'].sum()
                t_sell_amt = df_sells['total_amount'].sum()
                t_profit = t_sell_amt - t_buy_amt
                t_rate = (t_profit / t_buy_amt * 100) if t_buy_amt > 0 else 0
                t_max = df_h_filtered['turn'].max()
                
                # [신규] 실현 손익 기반 MDD 계산 (예산 + 누적 수익 곡선 활용)
                mdd_val = 0.0
                try:
                    state_dict = json.loads(sel_row['s_json'])
                    # 예산 정보 추출 (CA는 cycle_budget, VR은 initial_budget 사용)
                    budget = float(state_dict.get('cycle_budget') or state_dict.get('initial_budget') or 0)
                    if budget > 0 and not df_sells.empty:
                        # 날짜(date) 기준으로 정렬하여 실현 수익 곡선 생성 (timestamp 통합 반영)
                        df_s_sorted = df_sells.sort_values('date')
                        cum_profit = df_s_sorted['realized_profit'].cumsum()
                        equity_curve = budget + cum_profit
                        peak = equity_curve.cummax()
                        drawdown = (equity_curve - peak) / peak
                        mdd_val = drawdown.min() * 100
                except:
                    pass

                c_st1, c_st2, c_st3, c_st4, c_st5, c_st6 = st.columns(6)
                currency_unit = "₩" if sel_m == "KR" else "$"
                c_st1.metric("총 매수 금액", f"{currency_unit}{format_currency(t_buy_amt, sel_m)}")
                c_st2.metric("총 매도 금액", f"{currency_unit}{format_currency(t_sell_amt, sel_m)}")
                c_st3.metric("실현 수익금", f"{currency_unit}{format_currency(t_profit, sel_m)}", delta=f"{t_rate:+.2f}%")
                c_st4.metric("거래 횟수", f"{len(df_h_filtered)}회")
                if sel_row['전략'] == "CA":
                    c_st5.metric("최대 회차 (T_max)", f"{t_max:.1f}T")
                else:
                    # VR MDD 시뮬레이션 (단순화: 평가금 기반 MDD는 백테스트에서 수행하므로 여기선 실현손익 MDD 생략)
                    c_st5.metric("전략 유형", "VR")
                c_st6.metric("실현 MDD", f"{mdd_val:.2f}%")

                st.dataframe(df_h_filtered, use_container_width=True)
            else:
                st.caption("관련 거래 내역이 없습니다.")

with tab_settings:
    st.header("⚙️ 시스템 및 사용자 설정")
    
    with st.expander("👤 사용자 정보", expanded=True):
        u_name = st.text_input("사용자 이름", value=get_config("user_name", "Investor"))
        if st.button("사용자 정보 저장"):
            set_config("user_name", u_name)
            st.success("사용자 정보가 저장되었습니다.")
            
    with st.expander("📧 이메일 알림 설정 (SMTP)"):
        st.caption("시스템 중요 알림 및 일일 보고서를 수신할 Zoho SMTP 설정을 관리합니다.")
        env_path = os.path.join(PROJECT_ROOT, "env", ".env")
        
        z_user = st.text_input("Zoho 계정 (Email)", value=os.getenv("ZOHO_SMTP_USER", ""))
        z_pass = st.text_input("Zoho 앱 비밀번호", value=os.getenv("ZOHO_SMTP_PASSWORD", ""), type="password")
        z_recv = st.text_input("수신용 이메일", value=os.getenv("ZOHO_RECEIVER_EMAIL", ""))

        if st.button("SMTP 설정 저장 (.env 업데이트)"):
            set_key(env_path, "ZOHO_SMTP_USER", z_user)
            set_key(env_path, "ZOHO_SMTP_PASSWORD", z_pass)
            set_key(env_path, "ZOHO_RECEIVER_EMAIL", z_recv)
            st.success("환경 변수 파일이 업데이트되었습니다. 시스템 재시작 후 반영됩니다.")
            load_dotenv(env_path, override=True)

    with st.expander("📜 종목 리스트 관리"):
        st.caption("ETF_list 파일을 직접 수정하거나 삭제합니다. (형식: 코드 \"종목명\")")
        m_list_choice = st.radio("시장 선택", ["KR", "US"], horizontal=True, key="m_list_edit_choice")
        list_file_name = f"ETF_list_{m_list_choice.lower()}.txt"
        list_file_path = os.path.join(PROJECT_ROOT, "env", list_file_name)
        
        if os.path.exists(list_file_path):
            # 파일 읽기 및 파싱
            with open(list_file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            list_entries = []
            for line in lines:
                clean_line = line.split('#')[0].strip()
                if clean_line:
                    parts = clean_line.split(None, 1)
                    ticker_code = parts[0].upper()
                    ticker_name = parts[1].strip().strip('"') if len(parts) > 1 else ticker_code
                    list_entries.append({"Ticker": ticker_code, "Name": ticker_name})
            
            df_list_edit = pd.DataFrame(list_entries)
            
            # Data Editor (num_rows="dynamic"을 통해 추가/삭제 가능)
            edited_list_df = st.data_editor(
                df_list_edit,
                num_rows="dynamic",
                column_config={
                    "Ticker": st.column_config.TextColumn("종목 코드", required=True, help="예: 122630 또는 TQQQ"),
                    "Name": st.column_config.TextColumn("종목명", required=True),
                },
                hide_index=True,
                use_container_width=True,
                key=f"list_editor_widget_{m_list_choice}"
            )
            
            if st.button(f"💾 {m_list_choice} 리스트 변경 사항 저장", key=f"save_list_btn_{m_list_choice}", use_container_width=True, type="primary"):
                try:
                    with open(list_file_path, 'w', encoding='utf-8') as f:
                        for _, row in edited_list_df.iterrows():
                            t_val = str(row["Ticker"]).strip().upper()
                            n_val = str(row["Name"]).strip()
                            if t_val and n_val:
                                f.write(f'{t_val} "{n_val}"\n')
                    st.success(f"✅ {list_file_name}에 변경 사항이 저장되었습니다.")
                    time.sleep(1)
                    st.rerun()
                except Exception as e:
                    st.error(f"파일 저장 중 오류 발생: {e}")
        else:
            st.error(f"파일을 찾을 수 없습니다: {list_file_path}")

    with st.expander("📁 데이터베이스(cavr.db) 직접 확인"):
        db_path = os.path.join(PROJECT_ROOT, "data", "cavr.db")
        if os.path.exists(db_path):
            st.write(f"DB 경로: `{db_path}`")
            try:
                conn = sqlite3.connect(db_path)
                
                table_name = st.selectbox("테이블 선택", ["trade_history", "strategy_state", "order_history", "config"], key="db_table_sel")
                
                limit = st.number_input("조회 개수", value=50, step=10)
                query = f"SELECT * FROM {table_name} ORDER BY rowid DESC LIMIT {limit}"
                df_raw = pd.read_sql_query(query, conn)
                st.dataframe(df_raw, use_container_width=True)
                conn.close()
            except Exception as e:
                st.error(f"DB 조회 중 오류: {e}")
        else:
            st.error(f"DB 파일을 찾을 수 없습니다: {db_path}")

    st.divider()
    st.subheader("⚠️ 시스템 관리")

    with st.expander("🛠️ 데이터 정합성 도구", expanded=False):
        st.write("동기화 오류로 인해 가격이나 수량이 0으로 표시되는 주문들을 정리합니다.")
        if st.button("🗑️ 불완전한 주문 내역(0원/0주) 강제 삭제", use_container_width=True):
            count = cleanup_invalid_orders_db()
            if count > 0:
                st.success(f"✅ {count}개의 잘못된 주문 내역을 정리했습니다.")
                time.sleep(1)
                st.rerun()
            else:
                st.info("정리할 대상이 없습니다.")

        if st.button("🧹 중복 주문(이미 체결됨) 정리", use_container_width=True):
            count = cleanup_processed_orders_db()
            if count > 0:
                st.success(f"✅ {count}개의 중복된 미체결 내역을 정리했습니다.")
                time.sleep(1)
                st.rerun()
            else:
                st.info("정리할 중복 내역이 없습니다.")

    with st.expander("🛠️ 실현 손익 데이터 복구 (Cleanup)", expanded=False):
        st.write("DB의 실현 손익이 0이거나 부정확한 경우, 거래 내역을 시간순으로 재추적하여 평단가와 이익을 재계산합니다.")
        target_recalc_sym = st.text_input("대상 종목 코드 (예: 122630)", value=symbol)
        if st.button(f"🔄 {target_recalc_sym} 손익 전체 재계산 실행", use_container_width=True, type="primary"):
            success, msg = recalculate_all_profits_db(target_recalc_sym, market_code)
            if success:
                st.success(msg)
                time.sleep(1)
                st.rerun()
            else:
                st.error(msg)

    st.warning("프로그램 수정 후 재시작이 필요한 경우 아래 버튼을 사용하여 안전하게 종료하세요.")
    
    if st.button("🛑 전체 시스템 종료 (Full Shutdown)", help="대시보드와 백그라운드 스케줄러를 모두 종료합니다.", use_container_width=True):
        # 스케줄러 종료를 위한 DB 플래그 설정
        set_config("system_shutdown_flag", "true")
        st.error("시스템 종료 명령을 전송했습니다. 잠시 후 대시보드가 닫힙니다.")
        
        # 텔레그램 알림 전송 (선택 사항)
        send_telegram_message("🛑 <b>[시스템]</b> 사용자에 의해 시스템 종료 명령이 실행되었습니다.")
        
        time.sleep(3)
        os._exit(0)

st.divider()
st.subheader(f"🛠️ 현재 작업 대상: {active_market_display} / {format_ticker_display(symbol, market_code)} ({strategy_alias})")

# --- 실시간 시세 헤더 (프래그먼트) ---
@st.fragment(run_every=10)
def display_realtime_header():
    if mode == "실전 투자":
        is_active, now = check_market_active(market_code)
        if not is_active:
            st.info(f"😴 {market_code} 시장 운영 시간 외입니다. ({now.strftime('%H:%M')})")
            return
        try:
            broker = ActiveBroker
            price = broker.get_price(symbol)
            
            # [추가] 브로커 조회 실패 시 DB 캐시 확인
            if price is None or price <= 0:
                state = load_state_db(symbol, strategy_choice, market=market_code, strategy_name=strategy_alias)
                price = state.get('current_price', 0) if state else 0
                if price <= 0:
                    st.warning("⚠️ 현재가를 가져올 수 없습니다. (휴장 또는 통신 지연)")
                    return
                
            prev_close = broker.get_previous_close(symbol)
            if price > 0:
                diff = price - prev_close
                pct = (diff / prev_close * 100) if prev_close > 0 else 0
                
                diff_fmt = f"{int(diff):+,}" if market_code == "KR" else f"{diff:+.2f}"
                delta_val = f"{diff_fmt} ({pct:+.2f}%)"
                
                if market_code == "US":
                    usd_krw = float(get_config("USDKRW", "0.00"))
                    fx_time = get_config("USDKRW_UPDATE_TIME", "N/A")
                    col1, col2, col3, col4 = st.columns([1.5, 1.5, 1, 2])
                    col1.metric(label=f"현재가 ({format_ticker_display(symbol, market_code)})", value=f"{currency_symbol}{format_currency(price, market_code)}", delta=delta_val)
                    col2.metric(label="현재 환율 (USD/KRW)", value=f"₩{usd_krw:,.2f}")
                    with col2:
                        st.caption(f"업데이트: {fx_time}")
                        if st.button("🔄 지금 환율 조회", key="manual_fx_update", help="환율 정보를 즉시 갱신합니다."):
                            job_update_exchange_rate(broker=ActiveBroker, force=True)
                            st.rerun()
                    col3.write("") # 간격
                    col4.caption(f"⏱️ [{market_code}] 실시간 자동 갱신 중 (10초) | {datetime.now().strftime('%H:%M:%S')}")
                else:
                    col1, col2, col3 = st.columns([1, 1, 2])
                    col1.metric(label=f"현재가 ({format_ticker_display(symbol, market_code)})", value=f"{currency_symbol}{format_currency(price, market_code)}", delta=delta_val)
                    col2.write("") 
                    col3.caption(f"⏱️ [{market_code}] 실시간 자동 갱신 중 (10초) | {datetime.now().strftime('%H:%M:%S')}")
                st.divider()
        except Exception as e:
            st.error(f"[{market_code}] 시세 갱신 오류: {e}")

if mode == "실전 투자":
    display_realtime_header()


if mode == "실전 투자":
    # === 실전 투자 화면 ===
    
    # 1. 계좌 조회 버튼 및 결과
    col_act, col_msg = st.columns([1, 2])
    with col_act:
        if st.button("🔄 현재 계좌 상태 조회 (갱신)", use_container_width=True, type="primary"):
            with st.spinner(f"[{market_code}] Toss API 조회 중..."):
                broker = ActiveBroker
                
                # 정보 조회
                pool = broker.get_cash_pool()
                shares, avg_price, eval_amt = broker.get_account_equity(symbol)
                current_price = broker.get_price(symbol)
                prev_close = broker.get_previous_close(symbol)
                
                # DB 상태 동기화: 사이드바 요약 및 전략 로직 일관성을 위해 DB 업데이트
                state_data = load_state_db(symbol, strategy_choice, market=market_code, strategy_name=strategy_alias)
                if state_data:
                    if strategy_choice == "CA":
                        state_obj = CAState(**state_data)
                        state_obj.avg_price = avg_price
                        state_obj.total_shares = shares
                        state_obj.pool = pool # [추가] 조회된 실제 예수금을 DB에 저장
                        # T값 재계산 (현재 누적액 기준)
                        if state_obj.unit_buy_amount > 0:
                            invested = avg_price * shares
                            state_obj.current_turn = math.ceil((invested / state_obj.unit_buy_amount) * 10) / 10.0
                        save_state_db(state_obj, market=market_code, strategy_name=strategy_alias) # strategy_name도 함께 저장
                    elif strategy_choice == "VR":
                        state_obj = VRState(**state_data)
                        # VR의 경우 실시간 Pool 정보를 DB에 반영
                        state_obj.pool = pool
                        save_state_db(state_obj, market=market_code, strategy_name=strategy_alias) # strategy_name도 함께 저장

                # [개선] 거래 내역 동기화 쿨다운 적용 (60초)
                last_sync_key = f"last_sync_{market_code}_{symbol}"
                now_ts = time.time()
                sync_period = 7 # 기본 동기화 범위를 7일로 확대
                if now_ts - st.session_state.get(last_sync_key, 0) > 60:
                    from core.database import sync_trade_history_db
                    start_date_sync = (datetime.now() - timedelta(days=sync_period)).strftime("%Y%m%d")
                    end_date_sync = datetime.now().strftime("%Y%m%d")
                    logger.info(f"🔍 [Dash] {symbol} {market_code} 체결 내역 동기화 시도 ({sync_period}일)...")
                    execs = broker.fetch_execution_history(symbol, start_date_sync, end_date_sync)
                    sync_trade_history_db(symbol, execs, strategy=strategy_choice, market=market_code, strategy_name=strategy_alias)
                    st.session_state[last_sync_key] = now_ts
                else:
                    st.caption("ℹ️ 최근 1분 이내에 동기화를 완료했습니다. (로그 생략)")

        # [요청 반영] 과거 1주일 강제 동기화 버튼
        if st.button("📅 과거 1주일 내역 강제 동기화", use_container_width=True, help="쿨다운을 무시하고 최근 7일간의 모든 체결 내역을 다시 불러옵니다."):
            with st.spinner(f"[{market_code}] 과거 1주일 내역 조회 중..."):
                broker = ActiveBroker
                from core.database import sync_trade_history_db
                start_date_sync = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")
                end_date_sync = datetime.now().strftime("%Y%m%d")
                
                logger.info(f"🔍 [DeepSync] {symbol} {market_code} 1주일 체결 내역 강제 동기화 시작...")
                execs = broker.fetch_execution_history(symbol, start_date_sync, end_date_sync)
                
                logger.info(f"📥 [DeepSync] API로부터 {len(execs)}건의 데이터를 수신했습니다.")
                
                sync_trade_history_db(symbol, execs, strategy=strategy_choice, market=market_code, strategy_name=strategy_alias)
                st.success(f"✅ 최근 1주일 내역 동기화가 완료되었습니다. 하단 시스템 로그를 확인하세요.")
                time.sleep(1)
                st.rerun()

                # 세션 상태에 저장 (화면 리프레시 대응)
                st.session_state['live_account'] = {
                    'pool': pool,
                    'shares': shares,
                    'avg_price': avg_price,
                    'eval_amt': eval_amt,
                    'current_price': current_price,
                    'prev_close': prev_close,
                    'symbol': symbol,
                    'market_code': market_code,
                    'time': time.strftime("%H:%M:%S")
                }
    
    # --- [수정] 보유 종목 현황 실시간 업데이트를 위한 프래그먼트 도입 ---
    @st.fragment(run_every=5)
    def display_holdings_metrics_live():
        # 1. DB에서 가장 최신 상태 로드 (웹소켓 체결 업데이트가 실시간으로 반영됨)
        state = load_state_db(symbol, strategy_choice, market=market_code, strategy_name=strategy_alias)
        if not state:
            st.info("💡 등록된 전략 상태가 없습니다. 먼저 '계좌 상태 조회' 버튼을 누르거나 전략을 저장해주세요.")
            return

        # [STOP] 휴장 시간 브로커 호출 방지
        is_active, _ = check_market_active(market_code)
        
        # 2. 실시간 시세 및 잔고 정보 계산
        if is_active:
            broker = ActiveBroker
            curr_price = broker.get_price(symbol)
            prev_close = broker.get_previous_close(symbol)
            broker_cash = broker.get_cash_pool()
        else:
            curr_price = state.get('current_price', 0)
            prev_close = curr_price # 정확한 전일종가 조회가 어려우므로 현재가로 대체 표시
            broker_cash = state.get('pool', 0)

        # 수치 추출 (DB 기반 - 웹소켓 클라이언트가 업데이트한 값)
        pool = state.get('pool', 0.0)
        shares = state.get('total_shares', 0.0)
        avg_price = state.get('avg_price', 0.0)
        
        # 실시간 평가액 및 수익 계산
        eval_amt = shares * curr_price
        profit = eval_amt - (shares * avg_price)
        profit_pct = (profit / (shares * avg_price) * 100) if (shares * avg_price) > 0 else 0
        
        price_diff = curr_price - prev_close
        price_diff_pct = (price_diff / prev_close * 100) if prev_close > 0 else 0
        diff_fmt = f"{int(price_diff):+,}" if market_code == "KR" else f"{price_diff:+.2f}"
        delta_price = f"{diff_fmt} ({price_diff_pct:+.2f}%)"

        st.divider()
        col_p1, col_p2 = st.columns(2)
        col_p1.metric(label=f"💰 전략 할당 예수금 ({currency_symbol})", value=f"{currency_symbol}{format_currency(pool, market_code)}", help="DB에 저장된 이 전략 전용 예수금입니다.")
        col_p2.metric(label=f"🏦 거래소 총 예수금 ({currency_symbol})", value=f"{currency_symbol}{format_currency(broker_cash, market_code)}", help="증권사 계좌의 실제 출금 가능 원금입니다.")

        st.subheader(f"보유 종목 현황: {format_ticker_display(symbol, market_code)}")
        c1, c2, c3, c4, c5 = st.columns(5)
        
        c1.metric(label=f"현재가 ({currency_symbol})", value=f"{currency_symbol}{format_currency(curr_price, market_code)}", delta=delta_price)
        c2.metric("보유 수량", f"{int(shares) if market_code == 'KR' else shares}주")
        c3.metric("평단가", f"{currency_symbol}{format_currency(avg_price, market_code)}")
        c4.metric("평가금액", f"{currency_symbol}{format_currency(eval_amt, market_code)}")
        c5.metric("추정 평가손익", f"{currency_symbol}{format_currency(profit, market_code)}", delta=f"{profit_pct:+.2f}%")
        
        st.caption(f"⏱️ 웹소켓 및 DB 연동 중 (5초 주기 자동 갱신) | DB 최종 갱신: {state.get('updated_at', 'N/A')}")

    # 프래그먼트 실행
    display_holdings_metrics_live()

    # === [신규] 주문 내역 실시간 동기화 (Sync with KIS) ===
    is_active, _ = check_market_active(market_code)
    if is_active:
        try:
            logger.info(f"🔄 [Dash] {symbol} 주문 내역 동기화 시작...")
            broker = ActiveBroker
            open_orders = broker.fetch_open_orders(symbol)
            logger.debug(f"KIS API raw open orders response for {symbol}: {open_orders}") # KIS API 미체결 주문 원본 응답 로그
            # DB와 KIS 서버 실시간 동기화
            sync_open_orders_db(symbol, open_orders)
            logger.info(f"✅ [Dash] {symbol} 주문 내역 동기화 완료")
            
            # 만약 DB에는 없는데 KIS에는 있는 주문이 있다면 (수동 주문 등), 
            # 필요시 여기서 log_order_db를 호출하여 추가할 수도 있습니다.
        except Exception as e:
            logger.error(f"주문 내역 동기화 실패: {e}")

    # === [신규] 주문 계획 확인 및 실행 영역 ===
    if st.session_state.planned_orders:
        st.divider()
        st.subheader("📋 주문 계획 확인 및 수정 (Pending Orders)")
        st.info("아래 테이블에서 **가격/수량을 수정**하거나 **체크박스로 실행할 주문을 선택**하세요.")
        
        # DataFrame 변환 및 표시
        df_plan = pd.DataFrame(st.session_state.planned_orders)
        
        if not df_plan.empty:
            # 'selected' 컬럼 추가 (기본값 True)
            if "selected" not in df_plan.columns:
                df_plan.insert(0, "selected", False) # [수정] 기본값 False로 변경

            # 표시할 컬럼 순서 정의
            cols_order = ["selected", "symbol", "strategy", "side", "qty", "price", "type", "desc"]
            
            # [수정] 시장별로 주문 유형 옵션을 다르게 제공
            # ORDER_TYPE_MAP을 직접 사용
            df_plan["type_display"] = df_plan["type"].map(lambda x: ORDER_TYPE_MAP.get(x, x))
            
            # 주문 제출 시 원래 코드로 역변환하기 위한 맵
            reverse_type_map = {v: k for k, v in ORDER_TYPE_MAP.items()}

            # Data Editor로 표시 (수정 가능)
            edited_df = st.data_editor(
                df_plan[["selected", "symbol", "strategy", "side", "qty", "price", "type", "type_display", "desc"]],
                column_config={
                    "selected": st.column_config.CheckboxColumn("실행", default=True),
                    "symbol": None, # 데이터는 유지하되 화면에서는 숨김
                    "type": None,   # 주문 제출에 필요한 원본 코드는 숨김
                    "price": st.column_config.NumberColumn(
                        f"가격 ({currency_symbol})",
                        min_value=0.0, 
                        format="₩%d" if market_code == "KR" else "$%.4f",
                        help="CAP 조건: 평단가 또는 현재가+15% 중 작은 가격으로 자동 계산됩니다."
                    ),
                    "qty": st.column_config.NumberColumn("수량", min_value=1, step=1), # type: ignore
                    "strategy": st.column_config.TextColumn("전략", disabled=True),
                    "side": st.column_config.TextColumn("구분", disabled=True),
                    "type_display": st.column_config.SelectboxColumn( # [수정] 유형 수정 가능하도록 변경
                        "유형", 
                        options=list(ORDER_TYPE_MAP.values()), # 시장별 옵션 사용
                        required=True
                    ),
                    "desc": st.column_config.TextColumn("설명", disabled=True),
                },
                hide_index=True,
                use_container_width=True,
                key="plan_editor"
            )
            
            # [수정] 주문 실행 및 취소 버튼을 나란히 배치하도록 변경
            col_p_btn1, col_p_btn2 = st.columns(2)
            
            if col_p_btn1.button("🚀 선택된 주문 실행 (Confirm)", type="primary", use_container_width=True):
                with st.spinner("주문을 전송하고 있습니다..."):
                    # 선택된 행만 필터링
                    orders_to_run = edited_df[edited_df["selected"] == True]
                    
                    if orders_to_run.empty:
                        st.warning("선택된 주문이 없습니다.")
                    else:
                        try:
                            broker = ActiveBroker  # ActiveBroker는 이미 인스턴스입니다.
                            success_count = 0
                            
                            for _, row in orders_to_run.iterrows():
                                # Broker 인스턴스를 통해 직접 주문 전송 (수정된 값 반영)
                                submitted_price_type = reverse_type_map.get(row['type'], row['type'])
                                broker.place_order( # type: ignore
                                    symbol=row['symbol'], 
                                    price=float(row['price']), 
                                    qty=int(row['qty']), 
                                    order_type=row['side'],
                                    price_type=submitted_price_type,
                                    strategy=row['strategy'] # 현재 활성화된 전략(CA/VR)을 따르도록 수정
                                )
                                success_count += 1
                                time.sleep(0.2) # API Rate Limit
                                
                            st.success(f"총 {success_count}건의 주문 요청이 완료되었습니다. (로그 확인)")
                            # 계획 초기화 및 리로드
                            st.session_state.planned_orders = []
                            time.sleep(2)
                            st.rerun()
                            
                        except Exception as e:
                            st.error(f"주문 실행 중 오류 발생: {e}")
            
            if col_p_btn2.button("✖️ 주문 계획 취소 (Cancel)", use_container_width=True):
                st.session_state.planned_orders = []
                st.info("📝 생성된 주문 계획이 삭제되었습니다.")
                st.rerun()
        else:
            st.write("주문 계획 데이터가 비어있습니다.")

    # === 매매 실행 상세 (CA 전략 전용) ===
    if strategy_choice == "CA":
        st.divider()
        st.subheader(f"📈 실전 매매 운영: {format_ticker_display(symbol, market_code)} (CA)")
        
        # 1. DB에서 상태 로드 시도
        state_data = load_state_db(symbol, "CA", market=market_code, strategy_name=strategy_alias)
        
        if state_data:
            # === 진행 중인 전략이 있는 경우 ===
            try:
                
                # 계산을 위한 변수 추출
                avg_price = state_data.get('avg_price', 0.0)
                current_t = state_data.get('current_turn', 0.0)
                unit_buy = state_data.get('unit_buy_amount', 0.0)
                total_shares = state_data.get('total_shares', 0.0)
                
                # [중요] 사이드바의 임시 값이 아닌 DB에 저장된 확정 파라미터 사용
                db_target_profit = state_data.get('target_profit_pct', target_profit)
                db_a_default = state_data.get('a_default', a_default)
                # Pool 잔고도 DB 상태 활용
                db_pool = state_data.get('pool', 0.0)

                # [Live Data Overlay] 실시간 계좌 정보가 있다면 화면 표시용 변수를 최신화
                # (DB 상태가 아직 업데이트되지 않았거나 초기화 상태일 경우를 대비)
                if 'live_account' in st.session_state and st.session_state['live_account']['symbol'] == symbol:
                    live_info = st.session_state['live_account']
                    if live_info['shares'] > 0:
                        # 평단과 수량을 실시간 데이터로 대체하여 표시
                        avg_price = live_info['avg_price']
                        total_shares = live_info['shares']
                        
                        # T값이 0이거나 실제 잔고와 차이가 클 경우 재추정하여 표시
                        # T = (평단 * 수량) / 1회매수금
                        if unit_buy > 0:
                            invested_amt = avg_price * total_shares
                            est_t = math.ceil((invested_amt / unit_buy) * 10) / 10.0
                            if current_t == 0 or abs(current_t - est_t) > 0.5:
                                current_t = est_t
                
                # Star% 계산 (CostAveragingEngine 로직 재사용 또는 직접 계산)
                # Star% = Target - (T/2 * (T_def/a_def))/100
                # 여기서는 Config 값을 알 수 없으므로 사이드바 입력을 사용
                # (주의: 사이드바 입력과 실제 실행 설정이 다를 수 있음)
                
                term = (current_t / 2.0) * (40 / db_a_default) 
                star_pct = db_target_profit - (term / 100.0)
                cur_sym = currency_symbol
                
                st.write(f"현재 T: **{current_t:.1f}** | Star%: **{star_pct*100:.2f}%** | 1회 매수금: **{cur_sym}{format_currency(unit_buy, market_code)}**")

                c1, c2 = st.columns(2)
                
                # 1. 매수 조건표
                with c1:
                    st.markdown(f"##### ✅ 매수 조건표 (0.5T 씩)")
                    buy_rows = []
                    
                    # 한국 시장은 LOC 모사 주문으로 표시
                    buy_label_1 = "LOC 모사 평단" if market_code == "KR" else "LOC 평단"
                    buy_label_2 = "LOC 모사 Star%" if market_code == "KR" else "LOC Star%"

                    # LOC 평단 매수
                    if avg_price > 0:
                        qty_avg = int((unit_buy * 0.5) / avg_price)
                        # [개선] 예산 범위 내에서 최소 1주 보장 표시
                        if qty_avg <= 0 and unit_buy > 0 and db_pool >= avg_price:
                            qty_avg = 1 # 최소 1주 매수
                        buy_rows.append({"구분": buy_label_1, f"가격 ({cur_sym})": format_currency(avg_price, market_code), "수량 (주)": qty_avg})
                    
                    # LOC Star% 매수 (평단 * (1+Star) + Offset)
                    # 주의: Star가 음수일 경우 평단보다 낮게 매수
                    if avg_price > 0:
                        # 자전거래 방지를 위해 매수 가격에서 오프셋 차감 (KR: -10, US: -0.01)
                        price_star = (avg_price * (1 + star_pct)) + (-10 if market_code == "KR" else -0.01)
                        if price_star > 0:
                            qty_star = int((unit_buy * 0.5) / price_star)
                            if qty_star <= 0 and unit_buy > 0 and db_pool >= price_star:
                                qty_star = 1
                            buy_rows.append({"구분": buy_label_2, f"가격 ({cur_sym})": format_currency(price_star, market_code), "수량 (주)": qty_star})
                    
                    st.table(pd.DataFrame(buy_rows))

                # 2. 매도 조건표
                with c2:
                    st.markdown("##### ⛔ 매도 조건표")
                    sell_rows = []
                    
                    # 한국 시장은 LOC 모사 주문으로 표시
                    sell_label_1 = "LOC 모사 Star% (25%)" if market_code == "KR" else "LOC Star% (25%)"

                    if total_shares > 0:
                        # [수정] 수량 분할 로직: 25% 버림 후 나머지 75% 할당
                        qty_sell_25 = int(total_shares * 0.25)
                        qty_sell_75 = total_shares - qty_sell_25

                        # Star% 매도 (오프셋 제거)
                        price_sell_loc = avg_price * (1 + star_pct)
                        sell_rows.append({"구분": sell_label_1, f"가격 ({cur_sym})": format_currency(price_sell_loc, market_code), "수량 (주)": qty_sell_25})
                        
                        # 지정가 목표수익률 매도
                        price_sell_limit = avg_price * (1 + db_target_profit)
                        sell_rows.append({"구분": f"지정가 {db_target_profit*100:.0f}% (75%)", f"가격 ({cur_sym})": format_currency(price_sell_limit, market_code), "수량 (주)": int(qty_sell_75)})
                        
                    st.table(pd.DataFrame(sell_rows))

                # === 주문 내역 조회 (Order History) ===
                st.markdown("##### 📑 주문 내역 조회 (Submitted Orders)")
                cols_ord, rows_ord = get_order_history_db(symbol)
                if rows_ord:
                    df_orders = pd.DataFrame(rows_ord, columns=cols_ord)
                    # 주문유형 코드 변환 및 컬럼명 변경
                    df_orders['주문유형'] = df_orders['type'].map(lambda x: ORDER_TYPE_MAP.get(x, x))
                    # 가격 컬럼을 소수점 4자리 문자열로 변환하여 확인 가능하게 함
                    if market_code == "KR":
                        df_orders['가격'] = df_orders['price'].map(lambda x: f"{int(x):,}")
                    else:
                        df_orders['가격'] = df_orders['price'].map(lambda x: f"{x:.4f}")
                    df_orders = df_orders.rename(columns={'strategy_name': '별칭'}) # Rename strategy_name to 별칭
                    disp_cols = ['timestamp', 'symbol', '별칭', 'side', '가격', 'qty', '주문유형', 'status', 'odno']
                    df_orders = df_orders[disp_cols].rename(columns={'timestamp': '시간', 'symbol': 'Ticker', 'side': '구분', 'qty': '수량'})
                    # [KR] Ticker 표시 포맷팅
                    df_orders['Ticker'] = df_orders['Ticker'].apply(lambda x: format_ticker_display(x, market_code))
                    st.dataframe(df_orders, use_container_width=True)
                else:
                    st.caption("최근 제출된 주문 내역이 없습니다.")

                # --- [ADD] 전략 필터 정의 (이익 요약 및 거래 내역 공용) ---
                hist_filter_ca = st.selectbox("전략 필터", ["전체", "CA", "VR"], index=1, key=f"hist_filter_ca_{symbol}")
                target_strat_ca = None if hist_filter_ca == "전체" else hist_filter_ca

                # --- [ADD] 실현 손익 요약 및 별칭별 합계 (CA) ---
                st.markdown("##### 💰 실현 손익 현황 (Realized Profit)")
                logger.debug(f"[Debug] Trade History Query: Symbol={symbol}, Strategy={target_strat_ca}, Market={market_code}")
                cols, rows = get_detailed_trade_history_db(symbol, strategy=target_strat_ca, market=market_code)
                if rows:
                    df_all = pd.DataFrame(rows, columns=cols)
                    df_sells = df_all[df_all['side'] == 'SELL'].copy()
                    
                    if not df_sells.empty:
                        # [요청 반영] 별칭별 실현손익 합계 요약 (정렬 및 상세 지표 추가)
                        st.markdown("###### 💹 별칭별 실현손익 합계 (차수 역순 정렬)")
                        
                        alias_summary = []
                        # df_all을 사용하여 매수/매도 합계를 구함
                        for name, group in df_all.groupby('strategy_name'):
                            buys = group[group['side'] == 'BUY']['total_amount'].sum()
                            sells = group[group['side'] == 'SELL']['total_amount'].sum()
                            realized = group[group['side'] == 'SELL']['realized_profit'].sum()
                            rate = (realized / buys * 100) if buys > 0 else 0
                            
                            # 차수 추출 (정렬용: '이름_3차' -> 3)
                            m = re.search(r'(\d+)차', str(name))
                            turn_num = int(m.group(1)) if m else 0
                            
                            alias_summary.append({
                                '전략 별칭': name,
                                '총 매수액': buys,
                                '총 매도액': sells,
                                f'실현손익 ({currency_symbol})': realized,
                                '이익률(%)': rate,
                                '_turn_num': turn_num
                            })
                        
                        summary_df = pd.DataFrame(alias_summary)
                        if not summary_df.empty:
                            summary_df = summary_df.sort_values('_turn_num', ascending=False).drop('_turn_num', axis=1)
                            st.dataframe(summary_df.style.format({
                                '총 매수액': lambda x: format_currency(x, market_code),
                                '총 매도액': lambda x: format_currency(x, market_code),
                                f'실현손익 ({currency_symbol})': lambda x: format_currency(x, market_code),
                                '이익률(%)': '{:+.2f}%'
                            }), use_container_width=True, hide_index=True)

                        st.markdown("###### 📝 세부 매도 내역 (최신 시간순)")
                        # 사용자 요청 컬럼명으로 변환 및 포맷팅
                        df_sells['Ticker'] = df_sells['symbol'].apply(lambda x: format_ticker_display(x, market_code))
                        df_sells = df_sells.rename(columns={
                            'date': '매도시간', 'strategy_name': '전략 별칭', 'price': '매도단가', 
                            'qty': '매도수량', 'fee': '수수료', 'total_amount': '매도금액', 
                            'avg_price': '평단가', 'realized_profit': '이익', 'realized_profit_rate': '이익률(%)'
                        })
                        
                        disp_sell_cols = ['매도시간', '전략 별칭', 'Ticker', '매도단가', '매도수량', '수수료', '매도금액', '평단가', '이익', '이익률(%)']
                        st.dataframe(df_sells[disp_sell_cols].sort_values('매도시간', ascending=False), use_container_width=True)
                    else:
                        st.caption("최근 실현된 매도 수익 내역이 없습니다.")
                        
                # 3. 거래 내역표
                st.markdown("##### 📜 거래 내역 (Detailed Trade History)")

                
                # 위에서 가져온 rows를 재사용하여 상세 내역 표시
                if rows:
                    df_log = pd.DataFrame(rows, columns=cols)
                    # 시장 및 별칭 컬럼 가시성 확보를 위해 컬럼명 변경
                    df_log = df_log.rename(columns={'market': '시장', 'strategy_name': '별칭'})
                    # [KR] 종목 표시 포맷팅
                    df_log['symbol'] = df_log['symbol'].apply(lambda x: format_ticker_display(x, market_code))
                    
                    # 별칭(Alias) 필터링 기능 추가
                    unique_aliases = sorted([str(a) if a else "N/A" for a in df_log['별칭'].unique()])
                    selected_aliases = st.multiselect("별칭 필터 (Alias Filter)", options=unique_aliases, default=unique_aliases, key=f"alias_filter_ca_{symbol}")
                    df_log = df_log[df_log['별칭'].fillna("N/A").astype(str).isin(selected_aliases)]

                    st.dataframe(df_log, use_container_width=True)
                else:
                    st.info(f"💡 '{symbol}' ({strategy_alias})의 {market_code} 거래 내역이 DB에 없습니다.")
                    st.write("상단의 **'🔄 현재 계좌 상태 조회 (갱신)'** 버튼을 눌러 최근 5일간의 체결 내역을 동기화해보세요.")
                    if st.button("🔍 DB 전체 검색 (필터 없이)", key=f"raw_search_{symbol}"):
                        _, raw_rows = get_detailed_trade_history_db(None)
                        if raw_rows:
                            st.write("DB에 존재하는 전체 거래 내역 (디버깅용):")
                            st.dataframe(pd.DataFrame(raw_rows), use_container_width=True)
                        else:
                            st.error("DB의 trade_history 테이블이 완전히 비어 있습니다.")

            except Exception as e:
                st.error(f"전략 데이터 처리 중 오류: {e}")
                
        else:
            # === 신규 진입 또는 재시작 (상태 데이터 없음) ===
            st.warning(f"'{symbol}'에 대한 진행 중인 CA 전략이 없습니다.")
            
            if 'live_account' in st.session_state:
                # 계좌 정보 기반 T값 역산 및 초기화 제안
                info = st.session_state['live_account']
                current_shares = info['shares']
                current_avg_price = info['avg_price']
                
                # 사용자 입력값(사이드바) 가져오기
                input_unit_buy = unit_buy
                if input_unit_buy <= 0 and a_default > 0:
                     # unit_buy가 0이면 초기자본/분할수로 계산해야 하나, 초기자본을 명확히 알 수 없으므로
                     # 여기서는 사이드바 입력을 필수로 하거나, 추정해야 함.
                     # 편의상 입력값이 있어야 정확하다고 안내.
                     st.error("정확한 T값 계산을 위해 사이드바에서 '1회 매수 금액'을 설정해주세요.")
                
                elif input_unit_buy > 0:
                    invested_amount = current_shares * current_avg_price
                    estimated_t = 0.0
                    if invested_amount > 0:
                        raw_t = invested_amount / input_unit_buy
                        estimated_t = math.ceil(raw_t * 10) / 10.0
                    
                    st.info(f"현재 잔고를 바탕으로 **회차(T)**를 추정했습니다.")
                    
                    col_init1, col_init2 = st.columns(2)
                    with col_init1:
                        st.markdown(f"""
                        **현재 상황**
                        - 보유 수량: {current_shares} 주
                        - 평단가: {currency_symbol}{format_currency(current_avg_price, market_code)}
                        - 총 매수금액: {currency_symbol}{format_currency(invested_amount, market_code)}
                        """)
                    with col_init2:
                        st.markdown(f"""
                        **설정 및 추정**
                        - 1회 매수금: {currency_symbol}{format_currency(input_unit_buy, market_code)}
                        - **추정 회차 (T): {estimated_t:.1f}**
                        """)
                    
                    st.markdown("위 내용으로 실전 투자를 시작하시겠습니까?")
                    
                    if st.button("✅ 네, 이 상태로 실전 투자 시작 (DB 저장)", type="primary"):
                        # 초기 상태 생성 및 DB 저장
                        from core.cavr import CAState
                        new_state = CAState(
                            symbol=symbol,
                            strategy_type="CA",
                            strategy_name=strategy_alias,
                            market=market_code,
                            avg_price=current_avg_price,
                            total_shares=current_shares,
                            pool=info['pool'], # [추가] 초기 예수금 저장
                            current_turn=estimated_t,
                            cycle_budget=initial_cash, # 사이드바 초기자본 값 사용 (참고용)
                            total_profit=0.0,
                            unit_buy_amount=input_unit_buy,
                            daily_bought_amount=0.0,
                            last_intraday_level=0,
                            last_check_date="",
                            mode="NORMAL"
                        )
                        save_state_db(new_state, market=market_code, strategy_name=strategy_alias)
                        st.success(f"**{symbol}** 전략 초기화 완료! 화면을 갱신합니다.")
                        time.sleep(1)
                        st.rerun()
            else:
                st.info("먼저 상단의 '🔄 현재 계좌 상태 조회' 버튼을 눌러 잔고 정보를 가져오세요.")

    # === 매매 실행 상세 (VR 전략 전용) ===
    elif strategy_choice == "VR":
        st.divider()
        st.subheader(f"📈 실전 매매 운영: {format_ticker_display(symbol, market_code)} (VR)")
        
        state_data = load_state_db(symbol, "VR", market=market_code, strategy_name=strategy_alias)
        
        if state_data:
            try:
                # 상태 변수 추출
                current_v = state_data.get('cycle_V', 0.0) # 이번 사이클 목표 V
                last_pool = state_data.get('pool', 0.0)    # 직전 Pool
                current_shares = state_data.get('total_shares', 0.0) # (DB엔 없으므로 Live data 쓰거나 계산 필요하지만, VRState dataclass엔 없음)
                # VRState에는 shares가 없고 Pool과 V만 관리됨. Shares는 실시간 잔고로 파악해야 함.
                
                # Live 정보가 있으면 그것을 우선 사용
                if 'live_account' in st.session_state and st.session_state['live_account']['symbol'] == symbol:
                    live_shares = st.session_state['live_account']['shares']
                    live_pool = st.session_state['live_account']['pool']
                    current_price = st.session_state['live_account']['current_price']
                else:
                    live_shares = 0
                    live_pool = last_pool
                    current_price = 0

                # 밴드 계산 (사이드바 설정값 사용 - 주의: DB에 저장된 설정이 아님)
                # 실제로는 Config도 DB에 저장하거나 해야 하지만, 여기선 사이드바 값 사용
                low_band = current_v * (1 - (vr_band_pct / 100.0))
                high_band = current_v * (1 + (vr_band_pct / 100.0))
                
                # 현재 평가금 (E)
                current_eval = (live_shares * current_price) + live_pool

                col_vr1, col_vr2 = st.columns(2)
                with col_vr1:
                    st.markdown(f"""
                    **현재 상태 (Running)**
                    - 목표 밸류 (V): **{currency_symbol}{format_currency(current_v, market_code)}**
                    - 현재 평가금 (E): **{currency_symbol}{format_currency(current_eval, market_code)}**
                    - 밴드 범위: ({vr_band_pct}%) ***{currency_symbol}{format_currency(low_band, market_code)} ~ {currency_symbol}{format_currency(high_band, market_code)}***
                    """)
                with col_vr2:
                    # 밴드 위치 시각화
                    if current_v > 0:
                        pos_pct = (current_eval - current_v) / current_v * 100
                        st.metric("밴드 위치 (E vs V)", f"{pos_pct:+.2f}%", 
                                  delta="범위 벗어남" if abs(pos_pct) > vr_band_pct else "범위 안에 있음")
                
                st.divider()
                st.markdown("##### 🛒 예약 주문 가이드 (Limit Orders)")
                st.caption(f"보유 수량: {live_shares}주 | 사용 가능 현금: {currency_symbol}{format_currency(live_pool, market_code)} 기준")

                c1, c2 = st.columns(2)
                
                # 1. 매수 주문 (Low Band)
                # Low Band / (Shares + n)
                # 단, Pool 한도(적립식 75%, 거치 50%, 인출 25%) 체크 필요. 여기선 단순 계산만 보여줌.
                with c1:
                    st.markdown("**📉 매수 예약 (지정가)**")
                    buy_orders = []
                    pool_limit_ratio = 0.5 # Default
                    if "적립" in vr_invest_type_disp: pool_limit_ratio = 0.75
                    elif "인출" in vr_invest_type_disp: pool_limit_ratio = 0.25
                    
                    max_use_pool = live_pool * pool_limit_ratio
                    used_pool = 0.0
                    
                    # Pool에서 차감해야 하므로 (LowBand - CurrentPool)로 역산하지 않고
                    # VR 기본 공식: E = Shares*Price + Pool <= LowBand
                    # => Price <= (LowBand - Pool) / Shares (X) -> 이건 E 기준
                    # 라오어 공식: Price = LowBand / (Shares + n)
                    
                    # 현금 고려한 공식: LimitPrice = (LowBand - Pool) / (Shares + n) 
                    # (단, Pool이 변하지 않는다고 가정시. 실제론 매수하면 Pool이 줄어듦)
                    # 여기서는 dashboard.py 로직 단순화를 위해 기본 라오어 공식 + 현금 보정 적용
                    
                    target_val_buy = low_band - live_pool
                    if target_val_buy > 0:
                        for n in range(1, 6): # 5호가 정도만 보여줌
                            limit_price = target_val_buy / (live_shares + n)
                            if limit_price > 0 and used_pool + limit_price <= max_use_pool:
                                buy_orders.append({"구분": f"매수 {n}차", f"가격 ({currency_symbol})": format_currency(limit_price, market_code), "수량": "1주"})
                                used_pool += limit_price
                        st.table(pd.DataFrame(buy_orders))
                    else:
                        st.info("현재 현금이 Low Band보다 많아 추가 매수가 불필요하거나 불가능합니다.")

                # 2. 매도 주문 (High Band)
                # High Band / (Shares - n)
                with c2:
                    st.markdown("**📈 매도 예약 (지정가)**")
                    sell_orders = []
                    target_val_sell = high_band - live_pool
                    
                    if target_val_sell > 0 and live_shares > 0:
                        for n in range(0, min(5, int(live_shares))):
                            if (live_shares - n) > 0:
                                limit_price = target_val_sell / (live_shares - n)
                                sell_orders.append({"구분": f"매도 {n+1}차", f"가격 ({currency_symbol})": format_currency(limit_price, market_code), "수량": "1주"})
                        st.table(pd.DataFrame(sell_orders))
                    elif target_val_sell <= 0:
                         st.warning("현재 현금이 이미 High Band를 초과했습니다. 즉시 리밸런싱(매도)이 필요할 수 있습니다.")
                    else:
                        st.info("보유 수량이 없습니다.")

                # === 주문 내역 조회 (Order History) ===
                st.divider()
                st.markdown("##### 📑 주문 내역 조회 (Submitted Orders)") # type: ignore
                cols_ord, rows_ord = get_order_history_db(symbol)
                if rows_ord:
                    df_orders = pd.DataFrame(rows_ord, columns=cols_ord)
                    df_orders['주문유형'] = df_orders['type'].map(lambda x: ORDER_TYPE_MAP.get(x, x))
                    # 가격 컬럼을 소수점 4자리 문자열로 변환하여 확인 가능하게 함
                    if market_code == "KR":
                        df_orders['가격'] = df_orders['price'].map(lambda x: f"{int(x):,}")
                    else:
                        df_orders['가격'] = df_orders['price'].map(lambda x: f"{x:.4f}")
                    df_orders = df_orders.rename(columns={'strategy_name': '별칭'}) # Rename strategy_name to 별칭
                    disp_cols = ['timestamp', 'symbol', '별칭', 'side', '가격', 'qty', '주문유형', 'status', 'odno']
                    df_orders = df_orders[disp_cols].rename(columns={'timestamp': '시간', 'symbol': 'Ticker', 'side': '구분', 'qty': '수량'})
                    st.dataframe(df_orders, use_container_width=True)
                else:
                    st.caption("최근 제출된 주문 내역이 없습니다.")

                # --- [ADD] 전략 필터 정의 (VR) ---
                hist_filter_vr = st.selectbox("전략 필터", ["전체", "CA", "VR"], index=2, key=f"hist_filter_vr_{symbol}")
                target_strat_vr = None if hist_filter_vr == "전체" else hist_filter_vr

                # --- [ADD] 실현 손익 요약 및 별칭별 합계 (VR) ---
                st.markdown("##### 💰 실현 손익 현황 (Realized Profit)")
                logger.debug(f"[Debug] Trade History Query: Symbol={symbol}, Strategy={target_strat_vr}, Market={market_code}")
                cols, rows = get_detailed_trade_history_db(symbol, strategy=target_strat_vr, market=market_code)
                if rows:
                    df_all = pd.DataFrame(rows, columns=cols)
                    df_sells = df_all[df_all['side'] == 'SELL'].copy()
                    
                    if not df_sells.empty:
                        # [요청 반영] 별칭별 실현손익 합계 요약 (VR)
                        st.markdown("###### 💹 별칭별 실현손익 합계 (차수 역순 정렬)")
                        alias_summary_vr = []
                        for name, group in df_all.groupby('strategy_name'):
                            buys = group[group['side'] == 'BUY']['total_amount'].sum()
                            sells = group[group['side'] == 'SELL']['total_amount'].sum()
                            realized = group[group['side'] == 'SELL']['realized_profit'].sum()
                            rate = (realized / buys * 100) if buys > 0 else 0
                            
                            m = re.search(r'(\d+)차', str(name))
                            turn_num = int(m.group(1)) if m else 0
                            
                            alias_summary_vr.append({
                                '전략 별칭': name,
                                '총 매수액': buys,
                                '총 매도액': sells,
                                f'실현손익 ({currency_symbol})': realized,
                                '이익률(%)': rate,
                                '_turn_num': turn_num
                            })
                        
                        summary_df_vr = pd.DataFrame(alias_summary_vr)
                        if not summary_df_vr.empty:
                            summary_df_vr = summary_df_vr.sort_values('_turn_num', ascending=False).drop('_turn_num', axis=1)
                            st.dataframe(summary_df_vr.style.format({
                                '총 매수액': lambda x: format_currency(x, market_code),
                                '총 매도액': lambda x: format_currency(x, market_code),
                                f'실현손익 ({currency_symbol})': lambda x: format_currency(x, market_code),
                                '이익률(%)': '{:+.2f}%'
                            }), use_container_width=True, hide_index=True)

                        st.markdown("###### 📝 세부 매도 내역 (최신 시간순)")
                        df_sells['Ticker'] = df_sells['symbol'].apply(lambda x: format_ticker_display(x, market_code))
                        df_sells = df_sells.rename(columns={
                            'date': '매도시간', 'strategy_name': '전략 별칭', 'price': '매도단가', 
                            'qty': '매도수량', 'fee': '수수료', 'total_amount': '매도금액', 
                            'avg_price': '평단가', 'realized_profit': '이익', 'realized_profit_rate': '이익률(%)'
                        })
                        disp_sell_cols = ['매도시간', '전략 별칭', 'Ticker', '매도단가', '매도수량', '수수료', '매도금액', '평단가', '이익', '이익률(%)']
                        st.dataframe(df_sells[disp_sell_cols].sort_values('매도시간', ascending=False), use_container_width=True)
                    else:
                        st.caption("최근 실현된 매도 수익 내역이 없습니다.")

                # 3. 거래 내역표
                st.markdown("##### 📜 거래 내역 (Detailed Trade History)")

                # 위에서 가져온 rows를 재사용하여 상세 내역 표시
                if rows:
                    df_log = pd.DataFrame(rows, columns=cols)
                    # 시장 및 별칭 컬럼 가시성 확보를 위해 컬럼명 변경
                    df_log = df_log.rename(columns={'market': '시장', 'strategy_name': '별칭'})
                    
                    # 별칭(Alias) 필터링 기능 추가
                    unique_aliases = sorted([str(a) if a else "N/A" for a in df_log['별칭'].unique()])
                    selected_aliases = st.multiselect("별칭 필터 (Alias Filter)", options=unique_aliases, default=unique_aliases, key=f"alias_filter_vr_{symbol}")
                    df_log = df_log[df_log['별칭'].fillna("N/A").astype(str).isin(selected_aliases)]

                    st.dataframe(df_log, use_container_width=True)
                else:
                    st.info("아직 거래 내역이 없습니다. (실시간 체결 시 자동 기록됩니다)")

            except Exception as e:
                st.error(f"VR 상태 표시 중 오류: {e}")
        
        else:
            # === 신규 진입 (VR) ===
            st.warning(f"'{symbol}'에 대한 진행 중인 VR 전략이 없습니다.")
            if 'live_account' in st.session_state:
                info = st.session_state['live_account']
                init_v = (info['shares'] * info['current_price']) + info['pool']
                
                st.info(f"현재 자산 기준으로 **초기 V값**을 설정합니다.")
                st.write(f"- 총 평가 자산 (Equity + Pool): **{currency_symbol}{format_currency(init_v, market_code)}**")
                
                if st.button("✅ VR 전략 초기화 및 시작 (DB 저장)", type="primary"):
                    from core.cavr import VRState
                    # 이미 주식을 보유중이라면 RUNNING 모드로 바로 시작
                    mode = "RUNNING" if info['shares'] > 0 else "BOOTSTRAP"
                    
                    new_state = VRState(
                        symbol=symbol,
                        strategy_type="VR",
                        strategy_name=strategy_alias,
                        market=market_code,
                        V=init_v,
                        pool=info['pool'],
                        last_E=init_v,
                        mode=mode,
                        bootstrap_day_count=0,
                        cycle_V=init_v,
                        cycle_start_pool=info['pool']
                    )
                    save_state_db(new_state, market=market_code, strategy_name=strategy_alias)
                    st.success(f"**{symbol}** VR 전략 ({mode}) 초기화 완료! 화면을 갱신합니다.")
                    time.sleep(1)
                    st.rerun()
            else:
                st.info("상단의 '🔄 현재 계좌 상태 조회'를 먼저 실행해주세요.")

elif mode == "백테스트":
    # === 백테스트 화면 ===
    # Use session state to manage the flow
    if 'run_state' not in st.session_state:
        st.session_state.run_state = 'idle'

    csv_path = os.path.join("data", f"{symbol}.csv")
    file_exists = os.path.exists(csv_path)

    col1, col2 = st.columns(2)
    with col1:
        if file_exists and st.session_state.run_state == 'idle':
            st.info(f"'{symbol}.csv' 파일 보유 중")
            if st.button("기존 데이터로 실행", use_container_width=True):
                st.session_state.run_state = 'run_with_existing'
                st.rerun()
        elif st.session_state.run_state == 'idle':
            if st.button("데이터 수집 및 실행", type="primary", use_container_width=True):
                st.session_state.run_state = 'run_with_new'
                st.rerun()
    with col2:
        if file_exists and st.session_state.run_state == 'idle':
            if st.button("데이터 새로 수집 후 실행", type="primary", use_container_width=True):
                st.session_state.run_state = 'run_with_new'
                st.rerun()

    # 실행 로직
    if st.session_state.run_state in ['run_with_existing', 'run_with_new']:
        # 1. 데이터 수집
        if st.session_state.run_state == 'run_with_new':
            with st.spinner(f'{symbol} 데이터 수집 중...'):
                success, msg = update_ticker_data(symbol, days=int(fetch_days), market=market_code)
            if not success:
                st.error(msg)
                st.session_state.run_state = 'idle'
                st.stop()
            else:
                st.success(msg)

        # 2. 백테스트 실행
        with st.spinner('시뮬레이션 중...'):
            # 공통 파라미터
            backtest_kwargs = {
                "strategy_type": strategy_choice,
                "symbol": symbol,
                "file_path": csv_path,
                "initial_cash": float(initial_cash),
                "fee_rate": float(fee_rate),
                "market": market_code,
                "tax_rate": float(tax_rate if 'tax_rate' in locals() else 0.0)
            }
            
            # 전략별 파라미터 주입
            if strategy_choice == "CA":
                backtest_kwargs.update({
                    "unit_buy_amount": float(unit_buy),
                    "target_profit_pct": target_profit,
                    "a_default": int(a_default),
                    "use_quarter_stop": use_quarter_stop,
                })
            elif strategy_choice == "VR":
                backtest_kwargs.update({
                    "G": float(vr_g_value),
                    "band_low_pct": 100.0 - vr_band_pct,
                    "band_high_pct": 100.0 + vr_band_pct,
                    "initial_budget": float(initial_cash),
                    "periodic_accumulation": float(vr_periodic_amt),
                    "contribution_frequency": vr_freq,
                    "investment_type": vr_investment_type
                })

            result_text, result_df, trade_history_df = run_backtest(**backtest_kwargs)
            
            # 텔레그램으로 백테스트 결과 요약 전송
            if result_text:
                send_telegram_message(f"🧪 <b>[{symbol} 백테스트 완료]</b>\n<pre>{html.escape(result_text)}</pre>")
        
        # 3. 결과 표시
        st.text_area("결과 요약", value=result_text, height=200)
        
        if not result_df.empty:
            from plotly.subplots import make_subplots
            fig = make_subplots(specs=[[{"secondary_y": True}]])
            
            # 1. 자산 변동 (좌축) - 시장별 통화 기호 적용
            fig.add_trace(go.Scatter(x=result_df['Date'], y=result_df['TotalEquity'], mode='lines', name=f'Total Equity ({currency_symbol})'), secondary_y=False)
            
            # 2. 주가 변동 (우축)
            fig.add_trace(go.Scatter(x=result_df['Date'], y=result_df['Close'], mode='lines', name='Close Price', line=dict(dash='dot', color='grey', width=1)), secondary_y=True)
            
            # VR 정보 표시 (Target V, Pool, Bands)
            if 'Target_V' in result_df.columns:
                # Target V
                fig.add_trace(go.Scatter(
                    x=result_df['Date'], y=result_df['Target_V'], name=f'Target V ({currency_symbol})',
                    mode='lines',
                    line=dict(dash='dash', color='orange')
                ), secondary_y=False)
                
                # Cash Pool (현금) 표시
                if 'Pool' in result_df.columns:
                    fig.add_trace(go.Scatter(
                        x=result_df['Date'], y=result_df['Pool'], name=f'Cash Pool ({currency_symbol})',
                        mode='lines',
                        line=dict(color='cyan', width=1, dash='dot')
                    ), secondary_y=False)

                # Bands Calculation
                # 슬라이더 값(vr_band_pct)을 가져오거나 기본값 사용
                vr_band_val = locals().get('vr_band_pct', 15)
                band_val = vr_band_val / 100.0
                upper_band = result_df['Target_V'] * (1 + band_val)
                lower_band = result_df['Target_V'] * (1 - band_val)
                
                # Band Area (Low ~ High)
                fig.add_trace(go.Scatter(
                    x=result_df['Date'], y=lower_band,
                    mode='lines', line=dict(width=0), showlegend=False,
                ), secondary_y=False)
                fig.add_trace(go.Scatter(
                    x=result_df['Date'], y=upper_band,
                    mode='lines', name=f'Band (±{vr_band_val}%)',
                    line=dict(width=0),
                    fill='tonexty', fillcolor='rgba(255, 165, 0, 0.1)'
                ), secondary_y=False)

            # 3. 매매 마커 (우축 - 주가 위에 표시)
            if not trade_history_df.empty:
                buys = trade_history_df[trade_history_df['type'] == 'BUY']
                sells = trade_history_df[trade_history_df['type'] == 'SELL']
                
                if not buys.empty:
                    fig.add_trace(go.Scatter(
                        x=buys['Date'], y=buys['price'], mode='markers', name='Buy',
                        marker=dict(color='green', size=8, symbol='triangle-up')), secondary_y=True)
                
                if not sells.empty:
                    fig.add_trace(go.Scatter(
                        x=sells['Date'], y=sells['price'], mode='markers', name='Sell',
                        marker=dict(color='red', size=8, symbol='triangle-down')), secondary_y=True)

            fig.update_yaxes(title_text=f"Total Equity ({currency_symbol})", secondary_y=False)
            fig.update_yaxes(title_text=f"Close Price ({currency_symbol})", secondary_y=True)
            st.plotly_chart(fig, use_container_width=True)
            
            with st.expander("상세 데이터"):
                st.dataframe(result_df)
        
        st.session_state.run_state = 'idle'

# --- 하단 로그 영역 ---
st.divider()

# 로그 영역을 5초마다 자동 갱신하는 프래그먼트
@st.fragment(run_every=5)
def display_logs_area():
    st.subheader("📋 시스템 로그")
    msg_log_path = os.path.join(PROJECT_ROOT, "logs", "message_log.txt")
    if os.path.exists(msg_log_path):
        with open(msg_log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            log_text = "".join(lines[-100:][::-1]) # 최근 100줄 역순 표시
            st.text_area("Live Logs", value=log_text, height=400, help="dashboard 및 scheduler의 주요 메시지를 실시간으로 표시합니다.")
    else:
        st.info("로그 파일이 존재하지 않습니다.")

    st.divider()
    st.subheader("📡 스케줄러 로그 (Background Scheduler)")
    scheduler_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "scheduler.log")
    if os.path.exists(scheduler_log_path):
        with open(scheduler_log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            last_logs = "".join(lines[-100:][::-1]) # 최신 로그가 위로 오도록 역순 표시
            st.text_area("Scheduler Logs", value=last_logs, height=400, key="scheduler_logs_live", help="core/scheduler.py 실행 로그입니다.")
    else:
        st.info("스케줄러 로그 파일이 아직 생성되지 않았습니다.")

display_logs_area()
