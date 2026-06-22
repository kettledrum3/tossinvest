import os
import json
import requests
import logging
import math
import time
from dotenv import load_dotenv
from typing import Tuple, List, Literal, Optional
from datetime import datetime, timedelta, time as dtime

from core.brokers.base import Broker # Assuming base.py is in core/brokers
from core.cavr import kis_headers, invalidate_access_token_controlled
from core.database import log_order_db
from core.utils import format_symbol_display
from core.notifier import send_telegram_message

logger = logging.getLogger(__name__)

# 프로젝트 루트 경로 계산 (core/brokers/kis_kr.py 기준 -> core/ -> root/)
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(CURRENT_DIR))

class KisKrBroker(Broker):
    _notified = False  # 프로세스 내 최초 알림 여부 플래그

    def __init__(self):
        # 한국 시장 전용 환경 변수(.env.kr) 명시적 로드
        env_path = os.path.join(PROJECT_ROOT, "env", ".env.kr")
        if os.path.exists(env_path):
            load_dotenv(env_path, override=True)
            logger.debug(f"[KisKrBroker] 환경 변수 로드 완료: {env_path}")

        self.base_url = os.getenv("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443")
        self.account_no = os.getenv("KIS_ACCOUNT_NO", "").strip()

        logger.debug(f"[KisKrBroker] 연결 계좌 번호: {self.account_no}")
        
        if not KisKrBroker._notified:
            send_telegram_message(f"🇰🇷 <b>[KR 브로커 시작]</b>\n연결 계좌: <code>{self.account_no}</code>")
            KisKrBroker._notified = True

        self.cano, self.acnt_prdt_cd = self.account_no.split("-") if "-" in self.account_no else (self.account_no[:8], self.account_no[8:])
        self.is_simulation = "openapivts" in self.base_url
        self.market = "KR"
        self.etf_tickers = self._load_etf_list()

    def _call_api(self, method, url, tr_id, params=None, data=None, extra_headers=None):
        max_retries = 3
        for attempt in range(max_retries):
            headers = kis_headers(tr_id, market="KR")
            if not headers:
                logger.warning(f"⚠️ [KR API] 헤더 생성 실패 (토큰 쿨다운 중). 5초 대기 후 재시도 ({attempt+1}/{max_retries})")
                time.sleep(5.0)
                continue

            if extra_headers:
                headers.update(extra_headers)

            logger.debug(f"[KR API Call] TR_ID: {tr_id}, URL: {url}, Attempt: {attempt+1}")
            try:
                res = requests.request(method, url, headers=headers, params=params, data=data, verify=False)
            except Exception as e:
                if attempt < max_retries - 1:
                    logger.warning(f"⚠️ [KR API] Network/SSL Error: {e}. Retrying ({attempt+1}/{max_retries})...")
                    time.sleep(1.0)
                    continue
                else:
                    logger.error(f"❌ [KR API] Final failure after {max_retries} attempts: {e}")
                    return None
            
            # 토큰 만료 대응 (401 Unauthorized)
            if res.status_code == 401:
                logger.warning(f"⚠️ [KR API] 토큰 만료(401). 1초 대기 후 재발급 및 재시도.")
                invalidate_access_token_controlled(market="KR", account_no=self.account_no)
                time.sleep(1.0) # 재발급 전 짧은 대기로 TPS 폭증 방지
                continue
            
            # 서버 에러 대응 (500) - 토큰 삭제 없이 재시도
            if res.status_code == 500:
                logger.warning(f"⚠️ [KR API] KIS 서버 에러(500). 1초 대기 후 재시도 ({attempt+1}/{max_retries})")
                time.sleep(1.0)
                continue
            
            # TPS 초과 에러 대응
            if res.status_code == 200:
                res_data = res.json()
                logger.debug(f"[KR API Resp] TR_ID: {tr_id}, rt_cd: {res_data.get('rt_cd')}, msg: {res_data.get('msg1')}")
                if "초당 거래건수를 초과" in res_data.get("msg1", ""):
                    logger.warning(f"⚠️ [KR API] TPS 제한 감지. 2초 후 재시도 ({attempt+1}/{max_retries})")
                    time.sleep(2.0) # 대기 시간을 조금 더 늘려 확실히 회복 유도
                    continue
                return res
            
            return res
        return res # 최종 실패 시 그대로 반환

    def _load_etf_list(self) -> List[str]:
        """env/ETF_list_kr.txt 파일에서 ETF 티커 리스트를 읽어옵니다."""
        tickers = []
        try:
            etf_path = os.path.join(PROJECT_ROOT, "env", "ETF_list_kr.txt")
            
            if os.path.exists(etf_path):
                with open(etf_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.split('#')[0].strip()
                        if line:
                            tickers.append(line.split()[0])
        except Exception as e:
            logger.warning(f"[KisKrBroker] ETF 리스트 로드 실패 (기본 호가 단위 적용): {e}")
        return tickers

    def get_price(self, symbol: str) -> float:
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol}
        res = self._call_api("GET", url, "FHKST01010100", params=params)
        data = res.json()
        val = data.get('output', {}).get('stck_prpr') if data.get('rt_cd') == '0' else None
        if val:
            return float(val)
        logger.error(f"[KR] 현재가 조회 실패 ({symbol}): {data.get('msg1')}")
        return 0.0

    def get_previous_close(self, symbol: str) -> float:
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol}
        res = self._call_api("GET", url, "FHKST01010100", params=params)
        try:
            data = res.json()
            if data.get('rt_cd') == '0' and data.get('output'):
                # stck_sdpr: 주식 기준가(전일 종가), stck_prdy_clpr: 전일 종가
                output = data['output']
                val = output.get('stck_sdpr') or output.get('stck_prdy_clpr') or output.get('prdy_clpr')
                if val:
                    return float(val)
                
                # 계산식 fallback: 현재가 - (전일대비 * 부호)
                # prdy_vrss_sign: 1:상한, 2:상승, 3:보합, 4:하한, 5:하락
                prpr = float(output.get('stck_prpr', 0))
                vrss = float(output.get('prdy_vrss', 0))
                sign = output.get('prdy_vrss_sign', '3')
                
                if sign in ['1', '2']: return prpr - vrss
                if sign in ['4', '5']: return prpr + vrss
                return prpr

            logger.warning(f"[KR] 전일종가 조회 실패 ({symbol}): {data.get('msg1')}")
        except Exception as e:
            logger.error(f"[KR] 전일종가 파싱 오류 ({symbol}): {e}")
        return 0.0

    def get_current_high(self, symbol: str) -> float:
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol}
        res = self._call_api("GET", url, "FHKST01010100", params=params)
        try:
            data = res.json()
            if data.get('rt_cd') == '0' and data.get('output'):
                return float(data['output'].get('stck_hgpr', 0.0))
        except Exception:
            pass
        return 0.0

    def get_current_low(self, symbol: str) -> float:
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol}
        res = self._call_api("GET", url, "FHKST01010100", params=params)
        try:
            data = res.json()
            if data.get('rt_cd') == '0' and data.get('output'):
                return float(data['output'].get('stck_lwpr', 0.0))
        except Exception:
            pass
        return 0.0

    def get_last_5_day_avg_close(self, symbol: str) -> float:
        """[국내주식] 직전 5거래일 종가 평균 조회 (TR: FHKST03010100)"""
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_DATE_1": (datetime.now() - timedelta(days=15)).strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": datetime.now().strftime("%Y%m%d"),
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "0"
        }
        try:
            res = self._call_api("GET", url, "FHKST03010100", params=params)
            if res:
                data = res.json()
                if data.get('rt_cd') == '0':
                    output2 = data.get('output2', [])
                    
                    # [정밀화] 오늘 장중에 호출될 경우 첫 번째 항목이 '오늘'일 수 있음
                    # 오늘 날짜를 제외한 '직전' 5거래일 추출
                    today_str = datetime.now().strftime("%Y%m%d")
                    valid_items = [item for item in output2 if item['stck_bsop_date'] != today_str]
                    
                    closes = [float(item['stck_clpr']) for item in valid_items[:5]]
                    if len(closes) >= 5:
                        avg_close = sum(closes) / 5.0
                        logger.info(f"📊 [{symbol}] 직전 5일 종가 평균: ₩{int(avg_close):,}")
                        return avg_close
            logger.warning(f"[KR] 5일 평균가 조회 실패 ({symbol})")
        except Exception as e:
            logger.error(f"[KR] 5일 평균가 파싱 오류: {e}")
        return 0.0

    def _get_account_equity_impl(self, symbol: str) -> Optional[Tuple[float, float, float]]:
        logger.debug(f"[KR DEBUG] get_account_equity start for {symbol}")
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        tr_id = "VTTC8434R" if self.is_simulation else "TTTC8434R"
        params = {"CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt_cd, "AFHR_FLG": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STCK_GQTY_SBRK_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
        res = self._call_api("GET", url, tr_id, params=params)
        logger.debug(f"[KR DEBUG] get_account_equity end for {symbol}")
        data = res.json()
        if data.get('rt_cd') != '0':
            logger.error(f"[KR] 잔고 조회 실패: {data.get('msg1')}")
            return None # (None, None, None) 대신 None 반환
            
        for item in data.get('output1', []):
            if item.get('pdno') == symbol:
                qty = float(item.get('hldg_qty') or 0.0)
                qty = float(int(qty)) # Requirement 4: 수량은 자연수
                avg_price = float(item.get('pchs_avg_pric') or 0.0)
                eval_amt = float(item.get('evlu_amt') or 0.0)
                curr_price = self.get_price(symbol)
                if curr_price > 0:
                    eval_amt = qty * curr_price
                return qty, avg_price, eval_amt
        return 0.0, 0.0, 0.0


    def get_cash_pool(self) -> float:
        """
        계좌의 예수금(현금)을 조회합니다.
        실전: CTRP6548R (투자계좌자산현황조회)
        모의: VTTC8434R (주식잔고조회) 의 output2.dncl_amt 사용
        """
        if self.is_simulation:
            # 모의투자는 주식잔고조회(VTTC8434R)의 결과물에서 예수금을 가져옴
            url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
            tr_id = "VTTC8434R"
            params = {"CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt_cd, "AFHR_FLG": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STCK_GQTY_SBRK_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
        else:
            url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-account-balance"
            tr_id = "CTRP6548R"
            params = {
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "INQR_DVSN_1": "", # 공백입력
                "BSPR_BF_DT_APLY_YN": "" # 공백입력
            }

        res = self._call_api("GET", url, tr_id, params=params)
        try:
            data = res.json()
            if data.get('rt_cd') == '0' and data.get('output2'):
                # 실전(output2는 dict), 모의(output2는 list 형태)
                out2 = data['output2']
                val = out2[0].get('dncl_amt') if isinstance(out2, list) else out2.get('dncl_amt')
                return float(val or 0.0)
            logger.error(f"[KR] 예수금 조회 실패: {data.get('msg1')}")
        except Exception as e:
            logger.error(f"[KR] 예수금 조회 중 JSON 파싱 오류: {e}")
        return 0.0

    def _get_tick_size(self, symbol: str, price: float) -> int:
        """한국 호가 단위 계산 (2023년 1월 기준)
        Requirement: 종목 파악 시 ETF 여부를 별도로 판별하지 않고 공통 로직 적용.
        """
        p = abs(price)
        
        # [Requirement 1] ETF 여부를 판별하지 않고 2000원 기준 호가 단위를 공통 적용합니다.
        # 무한매수법/VR 대상 종목은 대부분 이 범위를 따릅니다.
        if p < 2000: return 1
        else: return 5

        # 기존 개별 주식용 호가 단위 로직은 현재 도달하지 않음 (주석 처리)
        """
        if p < 2000: return 1
        elif p < 5000: return 5
        elif p < 20000: return 10
        elif p < 50000: return 50
        elif p < 200000: return 100
        elif p < 500000: return 500
        else: return 1000
        """

    def get_stock_info(self, symbol: str) -> Optional[dict]:
        """[국내주식] 상품기본조회 (TR: CTPF1604R)"""
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/search-info"
        params = {"PDNO": symbol, "PRDT_TYPE_CD": "300"} # 300: 주식/ETF/ETN
        res = self._call_api("GET", url, "CTPF1604R", params=params)
        data = res.json()
        if data.get('rt_cd') == '0' and data.get('output'):
            return data['output']
        return None

    def adjust_price_by_tick(self, symbol: str, price: float, order_type: Literal["BUY", "SELL"]) -> int:
        """호가 단위에 맞게 가격 보정 (매수 시 올림, 매도 시 내림)"""
        if price <= 0: return 0
        tick = self._get_tick_size(symbol, price)
        if order_type == "BUY":
            # 호가 단위로 올림 (예: 2001원 -> 2005원)
            return int(math.ceil(price / tick) * tick)
        else:
            # 호가 단위로 내림 (예: 2004원 -> 2000원)
            return int(math.floor(price / tick) * tick)

    def place_order(self, symbol: str, price: float, qty: float, order_type: Literal["BUY", "SELL"], price_type: str = "00", strategy: str = "MANUAL") -> bool:
        """
        국내 주식 주문 실행
        국내 주식은 LOC(34), MOC(33) 기능을 지원하지 않으므로 무조건 지정가(00)로 처리합니다. MOC 모사주문은 장마감전 시장가(01)로 제출하여 최대한 근접하게 구현합니다.
        """
        # [V4.0 대응] MOC(33)는 시장가(01)로, 나머지는 지정가(00)로 변환
        if price_type == "33":
            price_type = "01" # MOC 모사: 장 종료 전 시장가 제출
            price = 0 # 시장가는 가격 0으로 제출
        elif price_type in ["34", "32", "31"]:
            price_type = "00" # LOC/LOO 모사: 지정가 제출

        # 호가 단위 보정 적용
        adjusted_price = self.adjust_price_by_tick(symbol, price, order_type)

        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        tr_id = ("VTTC0012U" if order_type == "BUY" else "VTTC0011U") if self.is_simulation else ("TTTC0012U" if order_type == "BUY" else "TTTC0011U")
        payload = {"CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt_cd, "PDNO": symbol, "ORD_DVSN": price_type, "ORD_QTY": str(int(qty)), "ORD_UNPR": str(adjusted_price)}
        logger.info(f"[KR 주문시도] {symbol} {order_type} {qty}주 @ {adjusted_price}원 (타입: {price_type})")
        
        res = self._call_api("POST", url, tr_id, data=json.dumps(payload))
        data = res.json()
        if data.get('rt_cd') == '0':
            odno = data['output']['ODNO']
            logger.info(f"[KR 주문성공] {symbol} 주문번호: {odno}")
            log_order_db(symbol, strategy, order_type, adjusted_price, qty, price_type, "SUCCESS", odno, data['msg1'])
            
            # [추가] 웹소켓 지연에 대비한 즉시 주문 제출 알림 (REST 기반)
            side_icon = "🔵" if order_type == "BUY" else "🔴"
            symbol_display = format_symbol_display(symbol, "KR")
            send_telegram_message(
                f"📝{side_icon} <b>[KR 주문상태 확인]</b>\n"
                f"일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"종목: {symbol_display}\n"
                f"구분: {'매수' if order_type == 'BUY' else '매도'}\n"
                f"수량: {int(qty)}주\n"
                f"가격: ₩{int(adjusted_price):,}\n"
                f"주문번호: <code>{odno}</code>"
            )
            return True
            
        logger.error(f"[KR 주문실패] {symbol}: {data.get('msg1')} ({data.get('rt_cd')})")
        return False

    def fetch_open_orders(self, symbol: str) -> List[dict]:
        """
        국내 주식 미체결 주문 조회 (정정/취소 가능 주문 조회)
        TR_ID: TTTC0084R (실전 전용, 모의투자 미지원)
        """
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl"
        tr_id = "TTTC0084R"
        
        params = {
            "CANO": self.cano,
            "ACNT_PRDT_CD": self.acnt_prdt_cd,
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
            "INQR_DVSN_1": "1",  # 0: 주문순, 1: 종목순
            "INQR_DVSN_2": "0"   # 0: 전체, 1: 매도, 2: 매수
        }
        
        try:
            res = self._call_api("GET", url, tr_id, params=params)
            data = res.json()
            
            if data.get('rt_cd') != '0':
                logger.error(f"[KIS-KR] 미체결 조회 실패: {data.get('msg1')}")
                return []
            
            # pdno(종목번호 6자리)가 일치하는 미체결 내역 필터링
            orders = [item for item in data.get('output', []) if str(item.get('pdno', '')).strip() == symbol.strip()]
            return orders
        except Exception as e:
            logger.error(f"[KIS-KR] 미체결 조회 중 오류 발생: {e}")
            return []

    def fetch_execution_history(self, symbol: str, start_date: str, end_date: str) -> List[dict]:
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld" # API 문서에 따라 URL 변경
        tr_id = "VTTC0081R" if self.is_simulation else "TTTC0081R" # TR ID는 이미 올바르게 설정되어 있었음
        all_executions = []
        ctx_fk = ""
        ctx_nk = ""
        
        while True:
            try:
                params = {
                    "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt_cd,
                    "INQR_STRT_DT": start_date, "INQR_END_DT": end_date,
                    "SLL_BUY_DVSN_CD": "00", # 문서 기준: SLL_BUY_DVSN_CD (00:전체)
                    "INQR_DVSN": "00",       # 문서 기준: 00 (역순)
                    "PDNO": symbol,
                    "CCLD_DVSN": "00",       # 문서 기준: 00 (전체)
                    "ORD_GNO_BRNO": "", 
                    "ODNO": "",
                    "INQR_DVSN_1": "", 
                    "INQR_DVSN_3": "00", 
                    "EXCG_ID_DVSN_CD": "ALL", # 문서 기준 필수 파라미터 추가
                    "CTX_AREA_FK100": ctx_fk,
                    "CTX_AREA_NK100": ctx_nk
                }
                extra_headers = {"tr_cont": "N"} if ctx_fk or ctx_nk else None
                res = self._call_api("GET", url, tr_id, params=params, extra_headers=extra_headers)
                
                if not res.text or not res.text.strip():
                    logger.error(f"[KIS-KR] 체결 내역 조회 실패: 빈 응답(Empty Response) 수신")
                    break

                data = res.json()
                logger.debug(f"[KIS-KR] fetch_execution_history raw response for {symbol} ({start_date}~{end_date}): {json.dumps(data, ensure_ascii=False)}")

                if data.get('rt_cd') != '0': break
                
                for item in data.get('output1', []):
                    # 국내 주식 (TTTC0081R) 응답 필드 매핑 (API 문서와 일치)
                    all_executions.append({
                        'ord_dt': item.get('ord_dt'),
                        'sll_buy_dvsn_cd': item.get('sll_buy_dvsn_cd'),
                        'ft_ccld_unpr3': item.get('avg_prvs'), # 평균 체결가
                        'ft_ccld_qty': item.get('tot_ccld_qty'), # 총 체결 수량
                        'ft_ccld_amt3': item.get('tot_ccld_amt'), # 총 체결 금액
                        'odno': item.get('odno'),
                        'ord_tmd': item.get('ord_tmd') # [ADD] 체결 시각 필드 추가
                    })
                
                if res.headers.get('tr_cont') in ['F', 'M']:
                    ctx_fk = data.get('ctx_area_fk100', '')
                    ctx_nk = data.get('ctx_area_nk100', '')
                else: break
            except Exception as e:
                logger.error(f"[KIS-KR] 체결 내역 조회 중 네트워크/JSON 오류 발생: {e}")
                break
        return all_executions

    def get_period_profit(self, start_date: str, end_date: str) -> float:
        """국내주식 기간별 실현 손익 조회 (TTTC8504R)"""
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-period-trade-profit"
        tr_id = "VTTC8504R" if self.is_simulation else "TTTC8504R"
        total_profit = 0.0
        ctx_fk = ""
        ctx_nk = ""
        
        while True:
            params = {
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "INQR_STRT_DT": start_date,
                "INQR_END_DT": end_date,
                "CTX_AREA_FK100": ctx_fk,
                "CTX_AREA_NK100": ctx_nk
            }
            res = self._call_api("GET", url, tr_id, params=params)
            data = res.json()
            if data.get('rt_cd') != '0': break
            
            # 국내 API는 output2에 기간 총계가 포함됨
            out2 = data.get('output2', {})
            total_profit = float(out2.get('tot_pnl_amt', 0.0))
            
            if res.headers.get('tr_cont') in ['F', 'M']:
                ctx_fk = data.get('ctx_area_fk100', '')
                ctx_nk = data.get('ctx_area_nk100', '')
            else: break
        return total_profit