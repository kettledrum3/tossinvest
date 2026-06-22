import os
import time
import json
import uuid
import math
import logging
import requests
from typing import Tuple, List, Literal, Optional
from datetime import datetime, timedelta
from dotenv import load_dotenv

from core.brokers.base import Broker
from core.database import log_order_db, get_connection
from core.notifier import send_telegram_message

logger = logging.getLogger(__name__)

# 프로젝트 루트 경로 계산
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(CURRENT_DIR))

class TossBroker(Broker):
    _notified = {}  # 시장별 최초 알림 여부 플래그

    def __init__(self, market: str):
        self.market = market.upper()
        
        # 시장별 환경 변수 명시적 로드
        env_suffix = "kr" if self.market == "KR" else "us"
        env_path = os.path.join(PROJECT_ROOT, "env", f".env.{env_suffix}")
        if os.path.exists(env_path):
            load_dotenv(env_path, override=True)
            logger.info(f"[TossBroker] 환경 변수 로드 완료: {env_path}")
        else:
            logger.error(f"[TossBroker] 환경 변수 파일을 찾을 수 없습니다: {env_path}")

        self.base_url = os.getenv("TOSS_BASE_URL", "https://openapi.tossinvest.com").strip()
        self.client_id = os.getenv("TOSS_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("TOSS_CLIENT_SECRET", "").strip()
        self.account_no = os.getenv("TOSS_ACCOUNT_NO", "").strip()
        
        # 계좌번호 포맷 체크 및 로깅
        logger.info(f"[TossBroker] {self.market} 연결 계좌 번호: {self.account_no}")

        if self.market not in TossBroker._notified:
            send_telegram_message(f"🦊 <b>[Toss {self.market} 브로커 시작]</b>\n연결 계좌: <code>{self.account_no}</code>")
            TossBroker._notified[self.market] = True

        # accountSeq 캐시
        self.account_seq = None
        self._init_account_seq()

    def _init_account_seq(self):
        """TOSS 계좌Seq(accountSeq)를 가져와 캐싱합니다."""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                token = self.get_access_token()
                if not token:
                    logger.warning(f"⚠️ [Toss API] 토큰 발급 실패로 계좌 일련번호 조회를 대기합니다. ({attempt+1}/{max_retries})")
                    time.sleep(2.0)
                    continue

                url = f"{self.base_url}/api/v1/accounts"
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"
                }
                res = requests.get(url, headers=headers, timeout=10)
                res.raise_for_status()
                data = res.json()
                
                # 계좌 목록에서 accountNo와 일치하는 accountSeq 추출
                accounts = data.get("result", [])
                for acc in accounts:
                    # 공백이나 대쉬(-) 제거 후 비교하여 정확도 향상
                    clean_api_acc = str(acc.get("accountNo", "")).replace("-", "").strip()
                    clean_local_acc = str(self.account_no).replace("-", "").strip()
                    if clean_api_acc == clean_local_acc:
                        self.account_seq = int(acc.get("accountSeq"))
                        logger.info(f"✅ [TossBroker] 계좌 {self.account_no} 의 accountSeq 설정 완료: {self.account_seq}")
                        return
                
                logger.error(f"❌ [TossBroker] API 계좌 목록에서 설정된 계좌번호 {self.account_no}를 찾을 수 없습니다. 목록: {accounts}")
                break
            except Exception as e:
                logger.error(f"❌ [TossBroker] 계좌 일련번호 조회 중 오류 발생: {e}")
                time.sleep(2.0)

    def get_access_token(self) -> str:
        """api_tokens 테이블을 활용하여 인증 토큰을 발급/관리합니다."""
        now = time.time()
        
        # 1. DB에서 캐시된 토큰 로드 (TOSS 계좌번호 기준)
        from core.database import load_api_token_db, save_api_token_db
        db_token_info = load_api_token_db(self.account_no)
        
        if db_token_info:
            db_token = db_token_info.get("token")
            db_expires_at = db_token_info.get("expires_at", 0)
            
            # 만료 60초 전까지는 캐시된 토큰 유효
            if db_token and now < db_expires_at - 60:
                return db_token

        # 2. 토큰 신규 발급 시도
        url = f"{self.base_url}/oauth2/token"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret
        }
        
        try:
            logger.info(f"[TossBroker] TOSS Open API 토큰 신규 발급 요청")
            res = requests.post(url, headers=headers, data=payload, timeout=10)
            res.raise_for_status()
            data = res.json()
            
            token = data["access_token"]
            expires_in = int(data.get("expires_in", 3600))
            expires_at = now + expires_in
            
            save_api_token_db(self.account_no, self.market, token, expires_at, now)
            logger.info(f"[TossBroker] TOSS Access Token 발급 성공 (만료: {datetime.fromtimestamp(expires_at)})")
            return token
        except Exception as e:
            logger.error(f"❌ [TossBroker] 토큰 발급 API 오류: {e}")
            # 발급 실패 시 캐시된 토큰이 조금이라도 살아있으면 그대로 사용 (Fallback)
            if db_token_info and db_token_info.get("token") and now < db_token_info.get("expires_at", 0) - 10:
                logger.warning(f"[TossBroker] 토큰 재발급 실패로 기존 DB 캐시 토큰 임시 연장 사용")
                return db_token_info.get("token")
            return ""

    def _get_headers(self) -> dict:
        """기본 인증 및 계좌 식별 정보가 담긴 HTTP 헤더를 생성합니다."""
        token = self.get_access_token()
        if not token:
            raise ValueError("[TossBroker] 유효한 인증 토큰을 획득하지 못했습니다.")
        
        if self.account_seq is None:
            self._init_account_seq()
            if self.account_seq is None:
                raise ValueError("[TossBroker] 계좌 일련번호(accountSeq)를 가져올 수 없습니다.")

        return {
            "Authorization": f"Bearer {token}",
            "X-Tossinvest-Account": str(self.account_seq),
            "Content-Type": "application/json"
        }

    def _call_api(self, method: str, path: str, params=None, data=None) -> dict:
        """TOSS Open API 공통 호출 메서드"""
        url = f"{self.base_url}{path}"
        headers = self._get_headers()
        
        try:
            res = requests.request(method, url, headers=headers, params=params, json=data, timeout=15)
            # 401 Unauthorized 시 토큰 강제 만료 후 재시도
            if res.status_code == 401:
                logger.warning(f"⚠️ [Toss API] 401 Unauthorized 감지. 토큰 만료 처리 후 재시도")
                from core.database import delete_api_token_db
                delete_api_token_db(self.account_no)
                headers = self._get_headers()
                res = requests.request(method, url, headers=headers, params=params, json=data, timeout=15)
                
            res.raise_for_status()
            return res.json()
        except Exception as e:
            logger.error(f"❌ [Toss API 오류] {method} {path} 실패: {e}")
            if 'res' in locals() and res is not None:
                logger.error(f"Response Body: {res.text}")
            raise e

    def get_price(self, symbol: str) -> float:
        """현재가 조회 (GET /api/v1/prices)"""
        try:
            data = self._call_api("GET", "/api/v1/prices", params={"symbols": symbol})
            results = data.get("result", [])
            if results:
                return float(results[0].get("lastPrice", 0.0))
        except Exception as e:
            logger.error(f"[TossBroker] 현재가 조회 실패 ({symbol}): {e}")
        return 0.0

    def get_previous_close(self, symbol: str) -> float:
        """전일 종가 조회 (GET /api/v1/candles, count=2)"""
        try:
            data = self._call_api("GET", "/api/v1/candles", params={
                "symbol": symbol,
                "interval": "1d",
                "count": 2,
                "adjusted": True
            })
            candles = data.get("result", {}).get("candles", [])
            
            # API 응답에서 가장 최신 봉이 0번째 인덱스
            if len(candles) >= 2:
                # 0번째가 오늘일 수 있으므로 1번째를 전일 종가로 사용
                return float(candles[1].get("closePrice", 0.0))
            elif len(candles) == 1:
                return float(candles[0].get("closePrice", 0.0))
        except Exception as e:
            logger.error(f"[TossBroker] 전일 종가 조회 실패 ({symbol}): {e}")
        return 0.0

    def get_last_5_day_avg_close(self, symbol: str) -> float:
        """직전 5거래일의 종가 평균 조회 (GET /api/v1/candles, count=6)"""
        try:
            data = self._call_api("GET", "/api/v1/candles", params={
                "symbol": symbol,
                "interval": "1d",
                "count": 6,
                "adjusted": True
            })
            candles = data.get("result", {}).get("candles", [])
            
            # 오늘 봉 제외 처리 (오늘 영업일 캔들이 있을 경우를 감안)
            today_str = datetime.now().strftime("%Y-%m-%d")
            
            valid_closes = []
            for c in candles:
                ts = c.get("timestamp", "")
                # ISO 8601의 날짜 부만 비교
                date_part = ts.split("T")[0]
                if date_part != today_str:
                    valid_closes.append(float(c.get("closePrice", 0.0)))
                    
            closes = valid_closes[:5]
            if len(closes) >= 5:
                return sum(closes) / 5.0
            elif len(closes) > 0:
                logger.warning(f"[TossBroker] 5일 평균가 계산용 데이터 부족 ({symbol}): {len(closes)}건만 사용")
                return sum(closes) / len(closes)
        except Exception as e:
            logger.error(f"[TossBroker] 5일 종가 평균 조회 실패 ({symbol}): {e}")
        return 0.0

    def get_current_high(self, symbol: str) -> float:
        """당일 고가 조회 (GET /api/v1/candles, count=1)"""
        try:
            data = self._call_api("GET", "/api/v1/candles", params={
                "symbol": symbol,
                "interval": "1d",
                "count": 1,
                "adjusted": True
            })
            candles = data.get("result", {}).get("candles", [])
            if candles:
                return float(candles[0].get("highPrice", 0.0))
        except Exception as e:
            logger.error(f"[TossBroker] 당일 고가 조회 실패 ({symbol}): {e}")
        return self.get_price(symbol)

    def get_current_low(self, symbol: str) -> float:
        """당일 저가 조회 (GET /api/v1/candles, count=1)"""
        try:
            data = self._call_api("GET", "/api/v1/candles", params={
                "symbol": symbol,
                "interval": "1d",
                "count": 1,
                "adjusted": True
            })
            candles = data.get("result", {}).get("candles", [])
            if candles:
                return float(candles[0].get("lowPrice", 0.0))
        except Exception as e:
            logger.error(f"[TossBroker] 당일 저가 조회 실패 ({symbol}): {e}")
        return self.get_price(symbol)

    def _get_account_equity_impl(self, symbol: str) -> Tuple[float, float, float]:
        """보유 주식 잔고 정보 조회 (GET /api/v1/holdings) -> (shares, avg_price, eval_amt)"""
        try:
            data = self._call_api("GET", "/api/v1/holdings", params={"symbol": symbol})
            overview = data.get("result", {})
            items = overview.get("items", [])
            for item in items:
                if str(item.get("symbol", "")).strip().upper() == symbol.strip().upper():
                    qty = float(item.get("quantity", 0.0))
                    qty = float(int(qty)) # Requirement 4: 수량은 자연수
                    avg_price = float(item.get("averagePurchasePrice", 0.0))
                    eval_amt = float(item.get("marketValue", {}).get("amount", 0.0))
                    curr_price = self.get_price(symbol)
                    if curr_price > 0:
                        eval_amt = qty * curr_price
                    return qty, avg_price, eval_amt
        except Exception as e:
            logger.error(f"[TossBroker] 잔고 조회 실패 ({symbol}): {e}")
        return 0.0, 0.0, 0.0


    def get_cash_pool(self) -> float:
        """예수금 조회 (GET /api/v1/buying-power)"""
        try:
            currency = "KRW" if self.market == "KR" else "USD"
            data = self._call_api("GET", "/api/v1/buying-power", params={"currency": currency})
            return float(data.get("result", {}).get("cashBuyingPower", 0.0))
        except Exception as e:
            logger.error(f"[TossBroker] 예수금 조회 실패: {e}")
        return 0.0

    def adjust_price_by_tick(self, symbol: str, price: float, order_type: Literal["BUY", "SELL"]) -> float:
        """호가 단위를 가격에 맞추어 보정"""
        if self.market == "US":
            # 미국 주식: $1 이상 소수점 2자리, $1 미만 소수점 4자리 절사/올림
            if price >= 1.0:
                return math.ceil(price * 100) / 100.0 if order_type == "BUY" else math.floor(price * 100) / 100.0
            else:
                return math.ceil(price * 10000) / 10000.0 if order_type == "BUY" else math.floor(price * 10000) / 10000.0
        else:
            # 한국 주식 호가단위
            # KOSPI/KOSDAQ 공용 간소화 틱 사이즈 (원 단위 정수)
            p = int(price)
            if p < 2000:
                tick = 1
            elif p < 5000:
                tick = 5
            elif p < 20000:
                tick = 10
            elif p < 50000:
                tick = 50
            elif p < 200000:
                tick = 100
            elif p < 500000:
                tick = 500
            else:
                tick = 1000

            if order_type == "BUY":
                # 호가 단위에 맞게 올림
                return float(math.ceil(price / tick) * tick)
            else:
                # 호가 단위에 맞게 내림
                return float(math.floor(price / tick) * tick)

    def place_order(self, symbol: str, price: float, qty: float, order_type: Literal["BUY", "SELL"], price_type: str = "00", strategy: str = "MANUAL") -> bool:
        """주문 발송 (POST /api/v1/orders)"""
        path = "/api/v1/orders"
        
        # 중복 주문 방지용 고유 clientOrderId 생성
        client_order_id = str(uuid.uuid4())
        
        # KIS 타입 호환 매핑
        # price_type "34" -> LIMIT + CLS (LOC 주문)
        # price_type "00" -> LIMIT + DAY (지정가 주문)
        # price_type "01" -> MARKET + DAY (시장가 주문)
        if price_type == "34" or price_type == "CLS":
            o_type = "LIMIT"
            tif = "CLS"
        elif price_type == "00" or price_type == "LIMIT":
            o_type = "LIMIT"
            tif = "DAY"
        elif price_type == "01" or price_type == "MARKET" or price_type == "33": # MOC(33)의 경우 시장가 주문으로 처리
            o_type = "MARKET"
            tif = "DAY"
        else:
            o_type = "LIMIT"
            tif = "DAY"

        # TOSS 수량 기준 주문 페이로드 구성
        payload = {
            "clientOrderId": client_order_id,
            "symbol": symbol.strip().upper(),
            "side": order_type.upper(),
            "orderType": o_type,
            "timeInForce": tif,
            "quantity": str(int(qty))
        }
        
        # 지정가일 때만 price 필드 필요
        if o_type == "LIMIT":
            if self.market == "KR":
                payload["price"] = str(int(price))
            else:
                # 소수점 포맷팅 규칙 적용
                payload["price"] = f"{price:.4f}" if price < 1.0 else f"{price:.2f}"

        try:
            data = self._call_api("POST", path, data=payload)
            res_obj = data.get("result", {})
            order_id = res_obj.get("orderId")
            
            if order_id:
                # 로컬 DB 주문 기록
                # type 컬럼에는 TIF를 기록하여 구분
                log_order_db(symbol, strategy, order_type, price, qty, tif, "SUCCESS", order_id, "Toss Order Placed", market=self.market)
                
                # 텔레그램 알림
                side_icon = "🔵" if order_type == "BUY" else "🔴"
                cur_sym = "₩" if self.market == "KR" else "$"
                price_fmt = f"{int(price):,}" if self.market == "KR" else f"{price:,.2f}"
                
                send_telegram_message(
                    f"📝{side_icon} <b>[Toss 주문 전송]</b>\n"
                    f"일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"종목: {symbol}\n"
                    f"구분: {'매수' if order_type == 'BUY' else '매도'} ({tif})\n"
                    f"수량: {int(qty)}주\n"
                    f"가격: {cur_sym}{price_fmt}\n"
                    f"주문 ID: <code>{order_id}</code>"
                )
                return True
        except Exception as e:
            logger.error(f"❌ [TossBroker] 주문 제출 실패 ({symbol}): {e}")
            log_order_db(symbol, strategy, order_type, price, qty, tif, "REJECTED", "N/A", str(e), market=self.market)
        return False

    def fetch_open_orders(self, symbol: str) -> List[dict]:
        """미체결 주문 조회 (GET /api/v1/orders?status=OPEN) -> KIS 호환 딕셔너리로 어댑팅"""
        try:
            data = self._call_api("GET", "/api/v1/orders", params={"status": "OPEN", "symbol": symbol})
            orders = data.get("result", {}).get("orders", [])
            
            adapted_orders = []
            for o in orders:
                # KIS 스타일 미체결 주문 필드 변환
                # 날짜 및 시간 분해 (orderedAt ISO 8601 -> YYYYMMDD, HHMMSS)
                ordered_at_str = o.get("orderedAt", "")
                # 예: "2026-03-28T09:30:00+09:00"
                date_part = "00000000"
                time_part = "000000"
                if "T" in ordered_at_str:
                    d_p, t_p = ordered_at_str.split("T")
                    date_part = d_p.replace("-", "")
                    time_part = t_p.split("+")[0].split("-")[0].replace(":", "")[:6]

                qty_total = int(float(o.get("quantity", 0)))
                qty_filled = int(float(o.get("execution", {}).get("filledQuantity", 0)))
                qty_unfilled = qty_total - qty_filled

                adapted_orders.append({
                    "pdno": symbol,
                    "symbol": symbol,
                    "odno": o.get("orderId"),
                    "ord_unpr": float(o.get("price") or 0.0),
                    "ft_ord_unpr3": float(o.get("price") or 0.0),
                    "ord_qty": float(qty_total),
                    "ft_ord_qty3": float(qty_total),
                    "nccs_qty": float(qty_unfilled), # 미체결 수량
                    "sll_buy_dvsn_cd": "02" if o.get("side") == "BUY" else "01",
                    "ord_dt": date_part,
                    "ord_tmd": time_part,
                    "ord_dvsn": "34" if o.get("timeInForce") == "CLS" else "00"
                })
            return adapted_orders
        except Exception as e:
            logger.error(f"[TossBroker] 미체결 주문 조회 실패 ({symbol}): {e}")
        return []

    def fetch_execution_history(self, symbol: str, start_date: str, end_date: str) -> List[dict]:
        """
        체결 이력 동기화 (로컬 DB의 미체결 주문번호들을 순회하며 개별 주문 API를 호출하여 상태를 확인 및 KIS 스타일로 변환)
        """
        adapted_executions = []
        try:
            # 1. 로컬 DB에서 'order_history'의 status='SUCCESS'인 주문번호들을 조회
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT odno, strategy_name FROM order_history 
                WHERE symbol=? AND market=? AND status='SUCCESS' AND odno != 'N/A'
            ''', (symbol, self.market))
            rows = cursor.fetchall()
            conn.close()
            
            if not rows:
                return []

            # 2. 각 주문번호에 대해 개별 getOrder API(/api/v1/orders/{orderId}) 호출
            for row in rows:
                order_id = row[0]
                try:
                    data = self._call_api("GET", f"/api/v1/orders/{order_id}")
                    order_obj = data.get("result", {})
                    
                    status = order_obj.get("status")
                    exec_info = order_obj.get("execution", {})
                    filled_qty = int(float(exec_info.get("filledQuantity", 0)))
                    
                    # 체결 수량이 존재하고 상태가 FILLED / PARTIAL_FILLED 인 경우
                    if filled_qty > 0 and status in ["FILLED", "PARTIAL_FILLED"]:
                        # 체결 완료 시각 파싱
                        filled_at_str = exec_info.get("filledAt") or order_obj.get("orderedAt", "")
                        date_part = "00000000"
                        time_part = "000000"
                        if "T" in filled_at_str:
                            d_p, t_p = filled_at_str.split("T")
                            date_part = d_p.replace("-", "")
                            time_part = t_p.split("+")[0].split("-")[0].replace(":", "")[:6]

                        # KIS 체결 기록 규격으로 어댑팅
                        adapted_executions.append({
                            "odno": order_id,
                            "sll_buy_dvsn_cd": "02" if order_obj.get("side") == "BUY" else "01",
                            "ft_ccld_unpr3": float(exec_info.get("averageFilledPrice") or order_obj.get("price") or 0.0),
                            "pndn_unpr": float(exec_info.get("averageFilledPrice") or order_obj.get("price") or 0.0),
                            "ft_ccld_qty": float(filled_qty),
                            "pndn_qty": float(filled_qty),
                            "ft_ccld_amt3": float(exec_info.get("filledAmount") or 0.0),
                            "pndn_amt": float(exec_info.get("filledAmount") or 0.0),
                            "ord_dt": date_part,
                            "stck_bsop_date": date_part,
                            "ord_tmd": time_part,
                            "ft_ccld_tm": time_part,
                            "stck_cntg_hour": time_part
                        })
                    elif status in ["CANCELED", "REJECTED"]:
                        # 주문이 취소되거나 거부된 경우 order_history를 업데이트하기 위해,
                        # dummy 체결 내역 대신 database.py 쪽 sync_open_orders_db에서 
                        # 걸러낼 수 있도록 active_odnos 에서 탈락시킵니다.
                        # (sync_open_orders_db가 미체결 목록을 인자로 받아, 여기에 없는 SUCCESS 주문은 CANCELED로 만듬)
                        pass
                except Exception as e:
                    logger.error(f"[TossBroker] 개별 주문 {order_id} 상세 조회 오류: {e}")
                    
        except Exception as e:
            logger.error(f"[TossBroker] 체결 이력 수집 중 오류: {e}")
            
        return adapted_executions

    def get_period_profit(self, start_date: str, end_date: str) -> float:
        """로컬 DB의 trade_history를 활용하여 지정된 기간 동안의 총 실현 손익을 구합니다."""
        total_profit = 0.0
        try:
            # start_date 및 end_date 포맷 정규화 (YYYYMMDD -> YYYY-MM-DD)
            s_dt = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]} 00:00:00"
            e_dt = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]} 23:59:59"
            
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT SUM(realized_profit) FROM trade_history
                WHERE market=? AND side='SELL' AND date >= ? AND date <= ?
            ''', (self.market, s_dt, e_dt))
            res = cursor.fetchone()
            conn.close()
            if res and res[0] is not None:
                total_profit = float(res[0])
        except Exception as e:
            logger.error(f"[TossBroker] 로컬 DB 기반 기간 손익 조회 실패: {e}")
        return total_profit

    def get_stock_info(self, symbol: str) -> Optional[dict]:
        """종목 기본 정보 조회 (GET /api/v1/stocks) -> KIS 호환 딕셔너리로 변환"""
        try:
            data = self._call_api("GET", "/api/v1/stocks", params={"symbols": symbol})
            results = data.get("result", [])
            if results:
                info = results[0]
                # KIS 형태의 딕셔너리로 마샬링하여 반환
                return {
                    "prdt_name": info.get("name", "이름 없음"),
                    "prdt_clsf_name": info.get("securityType", "분류 정보 없음"),
                    "ivst_prdt_type_cd_name": info.get("market", "유형 정보 없음")
                }
        except Exception as e:
            logger.error(f"[TossBroker] 종목 정보 조회 실패 ({symbol}): {e}")
        return None

    def get_exchange_rate_history(self, start_date: str, end_date: str) -> List[dict]:
        """과거 환율 이력 조회 (TOSS API 미지원으로 빈 리스트 반환)"""
        logger.warning("[TossBroker] TOSS Open API는 기간별 환율 이력 조회 기능을 제공하지 않습니다.")
        return []

