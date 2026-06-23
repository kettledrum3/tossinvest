import os
import json
import time
import requests
import logging
from dotenv import load_dotenv
from typing import Tuple, List, Literal, Optional
from datetime import datetime, timedelta, time as dtime

from core.brokers.base import Broker # Assuming base.py is in core/brokers
from core.cavr import kis_headers, invalidate_access_token_controlled
from core.database import log_order_db
from core.notifier import send_telegram_message

logger = logging.getLogger(__name__)

# 프로젝트 루트 경로 계산 (core/brokers/kis_us.py 기준 -> core/ -> root/)
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(CURRENT_DIR))

class KisUsBroker(Broker):
    _notified = False  # 프로세스 내 최초 알림 여부 플래그

    def __init__(self):
        # 미국 시장 전용 환경 변수(.env.us) 명시적 로드
        env_path = os.path.join(PROJECT_ROOT, "env", ".env.us")
        if os.path.exists(env_path):
            load_dotenv(env_path, override=True)
            logger.info(f"[KisUsBroker] 환경 변수 로드 완료: {env_path}")

        self.base_url = os.getenv("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443")
        self.account_no = os.getenv("KIS_ACCOUNT_NO", "").strip()

        # 계좌 번호 로드 확인 로그 및 텔레그램 알림 추가
        logger.info(f"[KisUsBroker] 연결 계좌 번호: {self.account_no}")
        
        if not KisUsBroker._notified:
            send_telegram_message(f"🇺🇸 <b>[US 브로커 시작]</b>\n연결 계좌: <code>{self.account_no}</code>")
            KisUsBroker._notified = True

        self.cano, self.acnt_prdt_cd = self.account_no.split("-") if "-" in self.account_no else (self.account_no[:8], self.account_no[8:])
        self.is_simulation = "openapivts" in self.base_url
        self.market = "US"
        self.exchange_map = {"TQQQ": "NASD", "SOXL": "AMEX", "SQQQ": "NASD"}

    def _get_exchange(self, symbol: str) -> str:
        return self.exchange_map.get(symbol.upper(), "NASD")

    def _to_lookup_excd(self, excd: str) -> str:
        mapping = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}
        return mapping.get(excd, "NAS")

    def _call_api(self, method, url, tr_id, params=None, data=None, extra_headers=None):
        max_retries = 3
        for attempt in range(max_retries):
            headers = kis_headers(tr_id, market="US")
            if not headers:
                logger.warning(f"⚠️ [US API] 헤더 생성 실패 (토큰 쿨다운 중). 5초 대기 후 재시도 ({attempt+1}/{max_retries})")
                time.sleep(5.0)
                continue

            if extra_headers:
                headers.update(extra_headers)

            try:
                res = requests.request(method, url, headers=headers, params=params, data=data, verify=False)
                
                # 토큰 만료 대응 (401 Unauthorized 또는 특정 에러 코드)
                # 500 에러는 토큰 문제가 아닐 가능성이 높으므로 삭제하지 않고 재시도만 수행합니다.
                if res.status_code == 401 or (res.status_code != 500 and res.json().get('msg_cd') == 'EGW00123'):
                    logger.warning(f"⚠️ [US API] 토큰 만료 감지. 재발급 및 재시도 ({attempt+1}/{max_retries})")
                    invalidate_access_token_controlled(market="US", account_no=self.account_no)
                    time.sleep(1.0) # 재발급 전 짧은 대기
                    continue
                
                # 서버 에러 대응 (500)
                if res.status_code == 500:
                    logger.warning(f"⚠️ [US API] KIS 서버 에러(500). 1초 대기 후 재시도 ({attempt+1}/{max_retries})")
                    time.sleep(1.0)
                    continue
                return res
            except Exception as e:
                if attempt < max_retries - 1:
                    logger.warning(f"⚠️ [US API] Network/SSL Error: {e}. Retrying ({attempt+1}/{max_retries})...")
                else:
                    logger.error(f"❌ [US API] Final failure after {max_retries} attempts: {e}")
                    raise e
                time.sleep(1.0)
        return res

    def get_price(self, symbol: str) -> float:
        url = f"{self.base_url}/uapi/overseas-price/v1/quotations/price"
        params = {"AUTH": "", "EXCD": self._to_lookup_excd(self._get_exchange(symbol)), "SYMB": symbol}
        res = self._call_api("GET", url, "HHDFS00000300", params=params)
        data = res.json()
        last_val = data.get('output', {}).get('last') if data.get('rt_cd') == '0' else None
        return float(last_val) if data.get('rt_cd') == '0' and last_val else 0.0

    def get_previous_close(self, symbol: str) -> float:
        url = f"{self.base_url}/uapi/overseas-price/v1/quotations/price"
        params = {"AUTH": "", "EXCD": self._to_lookup_excd(self._get_exchange(symbol)), "SYMB": symbol}
        res = self._call_api("GET", url, "HHDFS00000300", params=params)
        data = res.json()
        val = data.get('output', {}).get('base') if data.get('rt_cd') == '0' else None
        return float(val) if val else 0.0

    def get_current_high(self, symbol: str) -> float:
        url = f"{self.base_url}/uapi/overseas-price/v1/quotations/price"
        params = {"AUTH": "", "EXCD": self._to_lookup_excd(self._get_exchange(symbol)), "SYMB": symbol}
        res = self._call_api("GET", url, "HHDFS00000300", params=params)
        data = res.json()
        val = data.get('output', {}).get('high') if data.get('rt_cd') == '0' else None
        return float(val) if val else 0.0

    def get_current_low(self, symbol: str) -> float:
        url = f"{self.base_url}/uapi/overseas-price/v1/quotations/price"
        params = {"AUTH": "", "EXCD": self._to_lookup_excd(self._get_exchange(symbol)), "SYMB": symbol}
        res = self._call_api("GET", url, "HHDFS00000300", params=params)
        data = res.json()
        val = data.get('output', {}).get('low') if data.get('rt_cd') == '0' else None
        return float(val) if val else 0.0

    def get_stock_info(self, symbol: str) -> Optional[dict]:
        """[해외주식] 상품기본조회 (TR: CTPF1604R)"""
        excd = self._get_exchange(symbol)
        # 512: 나스닥, 513: 뉴욕, 529: 아멕스
        type_map = {"NASD": "512", "NYSE": "513", "AMEX": "529"}
        prdt_type = type_map.get(excd, "512")
        
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/search-info"
        params = {"PDNO": symbol, "PRDT_TYPE_CD": prdt_type}
        res = self._call_api("GET", url, "CTPF1604R", params=params)
        data = res.json()
        if data.get('rt_cd') == '0' and data.get('output'):
            return data['output']
        return None

    def get_last_5_day_avg_close(self, symbol: str) -> float:
        """[미국주식] 직전 5거래일 종가 평균 조회 (TR: FHKST03030100)"""
        url = f"{self.base_url}/uapi/overseas-price/v1/quotations/inquire-daily-chartprice"
        params = {
            "FID_COND_MRKT_DIV_CODE": "N", # N: 해외지수/종목
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_DATE_1": (datetime.now() - timedelta(days=15)).strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": datetime.now().strftime("%Y%m%d"),
            "FID_PERIOD_DIV_CODE": "D"
        }
        try:
            res = self._call_api("GET", url, "FHKST03030100", params=params)
            data = res.json()
            if data.get('rt_cd') == '0':
                output2 = data.get('output2', [])
                
                # [정밀화] 오늘 지수를 제외한 '직전' 5거래일 종가 추출
                today_str = datetime.now().strftime("%Y%m%d")
                valid_items = [item for item in output2 if item['stck_bsop_date'] != today_str]
                
                closes = [float(item['ovrs_nmix_prpr']) for item in valid_items[:5]]
                if len(closes) >= 5:
                    return sum(closes) / 5.0
            logger.warning(f"[US] 5일 평균가 조회 실패 ({symbol}): {data.get('msg1')}")
        except Exception as e:
            logger.error(f"[US] 5일 평균가 파싱 오류: {e}")
        return 0.0

    def get_exchange_rate(self) -> dict:
        """
        현재 달러/원 환율 및 변동 정보 조회 (TR: FHKST03030100)
        """
        url = f"{self.base_url}/uapi/overseas-price/v1/quotations/inquire-daily-chartprice"
        today_str = time.strftime("%Y%m%d")
        params = {
            "FID_COND_MRKT_DIV_CODE": "X", # X: 환율
            "FID_INPUT_ISCD": "FX@USDKRW",
            "FID_INPUT_DATE_1": today_str,
            "FID_INPUT_DATE_2": today_str,
            "FID_PERIOD_DIV_CODE": "D"
        }
        res = self._call_api("GET", url, "FHKST03030100", params=params)
        data = res.json()
        out1 = data.get('output1', {})
        if data.get('rt_cd') == '0' and out1:
            return {
                "rate": float(out1.get('ovrs_nmix_prpr', 0)),
                "diff": float(out1.get('ovrs_nmix_prdy_vrss', 0)),
                "pct": float(out1.get('prdy_ctrt', 0))
            }
        return {"rate": 0.0, "diff": 0.0, "pct": 0.0}

    def get_exchange_rate_history(self, start_date: str, end_date: str) -> List[dict]:
        """
        기간별 환율 이력 조회
        """
        url = f"{self.base_url}/uapi/overseas-price/v1/quotations/inquire-daily-chartprice"
        params = {
            "FID_COND_MRKT_DIV_CODE": "X",
            "FID_INPUT_ISCD": "FX@USDKRW",
            "FID_INPUT_DATE_1": start_date,
            "FID_INPUT_DATE_2": end_date,
            "FID_PERIOD_DIV_CODE": "D"
        }
        res = self._call_api("GET", url, "FHKST03030100", params=params)
        data = res.json()
        history = []
        if data.get('rt_cd') == '0':
            for item in data.get('output2', []):
                history.append({
                    "date": item.get('stck_bsop_date'),
                    "rate": float(item.get('ovrs_nmix_prpr', 0))
                })
        return history

    def _get_account_equity_impl(self, symbol: str) -> Tuple[float, float, float]:
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-balance"
        tr_id = "VTTS3012R" if self.is_simulation else "TTTS3012R"
        # 미국 주식 잔고 조회를 위한 페이지네이션 키 수정 (FK100 -> FK200)
        params = {
            "CANO": self.cano, 
            "ACNT_PRDT_CD": self.acnt_prdt_cd, 
            "OVRS_EXCG_CD": "%", 
            "TR_CRCY_CD": "USD", 
            "CTX_AREA_FK200": "", 
            "CTX_AREA_NK200": ""
        }
        try:
            res = self._call_api("GET", url, tr_id, params=params)
            if not res.text or not res.text.strip():
                return 0.0, 0.0, 0.0
            data = res.json()
            for item in data.get('output1', []):
                # 종목번호 비교 시 공백 제거 및 대문자 일치 확인
                if item.get('ovrs_pdno', '').strip() == symbol.strip().upper():
                    qty = float(item.get('ovrs_cblc_qty') or 0.0)
                    qty = float(int(qty)) # Requirement 4: 수량은 자연수
                    avg_price = float(item.get('pchs_avg_pric') or 0.0)
                    eval_amt = float(item.get('frcr_evlu_amt2') or 0.0)
                    curr_price = self.get_price(symbol)
                    if curr_price > 0:
                        eval_amt = qty * curr_price
                    return qty, avg_price, eval_amt
        except Exception as e:
            logger.error(f"[US] 잔고 조회 중 오류 발생: {e}")
        return 0.0, 0.0, 0.0


    def get_cash_pool(self) -> float:
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-psamount"
        tr_id = "VTTS3007R" if self.is_simulation else "TTTS3007R"
        params = {"CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt_cd, "OVRS_EXCG_CD": "%", "OVRS_ORD_UNPR": "0", "ITEM_CD": "TQQQ"}
        res = self._call_api("GET", url, tr_id, params=params)
        data = res.json()
        if data.get('rt_cd') == '0' and data.get('output'):
            return float(data['output'].get('ovrs_ord_psbl_amt', 0.0))
        logger.error(f"[US] 예수금 조회 실패: {data.get('msg1')}")
        return 0.0



    def place_order(self, symbol: str, price: float, qty: float, order_type: Literal["BUY", "SELL"], price_type: str = "00", strategy: str = "MANUAL") -> bool:
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/order"
        tr_id = ("VTTT1002U" if order_type == "BUY" else "VTTT1006U") if self.is_simulation else ("TTTT1002U" if order_type == "BUY" else "TTTT1006U")
        
        # 시장가 주문(01)의 경우, 주문단가는 "0.00"으로 설정되어야 합니다.
        if price_type == "01":
            price = 0.0

        payload = {
            "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt_cd, "OVRS_EXCG_CD": self._get_exchange(symbol),
            "PDNO": symbol, "ORD_QTY": str(int(qty)), "OVRS_ORD_UNPR": f"{price:.2f}", "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": price_type
        }
        if order_type == "SELL": payload["SLL_TYPE"] = "00"
        res = self._call_api("POST", url, tr_id, data=json.dumps(payload))
        data = res.json()
        if data.get('rt_cd') == '0':
            odno = data['output']['ODNO']
            log_order_db(symbol, strategy, order_type, price, qty, price_type, "SUCCESS", odno, data['msg1'])
            
            # [추가] 미국장 실시간 주문 제출 알림 (REST 기반)
            side_icon = "🔵" if order_type == "BUY" else "🔴"
            send_telegram_message(
                f"📝{side_icon} <b>[US 주문상태 확인]</b>\n"
                f"일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"종목: {symbol}\n"
                f"구분: {'매수' if order_type == 'BUY' else '매도'}\n"
                f"수량: {int(qty)}주\n"
                f"가격: ${price:,.2f}\n"
                f"주문번호: <code>{odno}</code>"
            )
            return True
        return False

    def fetch_open_orders(self, symbol: str) -> List[dict]:
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-nccs"
        tr_id = "VTTS3018R" if self.is_simulation else "TTTS3018R"
        params = {
            "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt_cd,
            "OVRS_EXCG_CD": self._get_exchange(symbol), "SORT_SQN": "DS",
            "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""
        }
        res = self._call_api("GET", url, tr_id, params=params)
        data = res.json()
        if data.get('rt_cd') == '0':
            return [item for item in data.get('output', []) if str(item.get('pdno', '')).strip() == symbol.strip()]
        return []

    def fetch_execution_history(self, symbol: str, start_date: str, end_date: str) -> List[dict]:
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-ccnl" # 명세 준수
        tr_id = "VTTS3035R" if self.is_simulation else "TTTS3035R"
        all_executions = []
        ctx_fk = ""
        ctx_nk = ""
        
        while True:
            # API_SPEC.md 명세에 따른 필수 파라미터 전수 매핑
            params = {
                "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "PDNO": symbol if symbol else "%", # 전종목 조회 대응
                "ORD_STRT_DT": start_date, 
                "ORD_END_DT": end_date,
                "SLL_BUY_DVSN": "00",       # 00:전체
                "CCLD_NCCS_DVSN": "01",    # 01:체결
                "OVRS_EXCG_CD": self._get_exchange(symbol) if symbol else "NASD", 
                "SORT_SQN": "DS",          # DS:정순
                "ORD_DT": "",              # 필수 빈값
                "ORD_GNO_BRNO": "",        # 필수 빈값
                "ODNO": "",                # 필수 빈값
                "CTX_AREA_FK200": ctx_fk, 
                "CTX_AREA_NK200": ctx_nk
            }
            
            # 연속 조회 헤더 설정
            extra_headers = {"tr_cont": "N"} if ctx_fk or ctx_nk else None
            
            try:
                res = self._call_api("GET", url, tr_id, params=params, extra_headers=extra_headers)
                data = res.json()
                if data.get('rt_cd') != '0': break
                
                output = data.get('output', [])
                if output:
                    # 특정 종목 필터링 (PDNO 파라미터가 정상 작동하지 않을 경우 대비)
                    if symbol:
                        filtered = [item for item in output if item.get('pdno', '').strip() == symbol]
                        all_executions.extend(filtered)
                    else:
                        all_executions.extend(output)
                
                if res.headers.get('tr_cont') in ['F', 'M']:
                    ctx_fk = data.get('ctx_area_fk200', '')
                    ctx_nk = data.get('ctx_area_nk200', '')
                else:
                    break
            except Exception as e:
                logger.error(f"[KIS-US] History Error: {e}")
                break
        return all_executions

    def get_period_profit(self, start_date: str, end_date: str) -> float:
        """해외주식 기간별 실현 손익 조회 (TTTS3039R)"""
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-period-profit"
        tr_id = "VTTS3039R" if self.is_simulation else "TTTS3039R"
        total_profit = 0.0
        ctx_fk = ""
        ctx_nk = ""
        
        while True:
            params = {
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "OVRS_EXCG_CD": "%",
                "PDNO": "%",
                "ORD_STRT_DT": start_date,
                "ORD_END_DT": end_date,
                "CTX_AREA_FK200": ctx_fk,
                "CTX_AREA_NK200": ctx_nk
            }
            res = self._call_api("GET", url, tr_id, params=params)
            data = res.json()
            if data.get('rt_cd') != '0': break
            
            # 해외 API는 개별 종목별 frcr_pnl을 합산해야 함
            for item in data.get('output', []):
                total_profit += float(item.get('frcr_pnl', 0.0))
            
            if res.headers.get('tr_cont') in ['F', 'M']:
                ctx_fk = data.get('ctx_area_fk200', '')
                ctx_nk = data.get('ctx_area_nk200', '')
            else: break
        return total_profit