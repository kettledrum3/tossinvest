import asyncio
import websockets
import json
import os
import time
import logging
import pytz
import math
import sys
import base64
import threading
from Crypto.Cipher import AES
import random # Added for exponential backoff jitter
from datetime import datetime, timedelta, time as dtime

# 프로젝트 루트 경로 추가
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.cavr import get_websocket_approval_key, CAState, VRState, format_symbol_display, CAConfig, CostAveragingEngine
from core.database import load_state_db, save_state_db, log_trade_db, delete_order_by_odno_db, get_all_states_db, get_config
from core.notifier import send_telegram_message

# Exponential backoff constants
BASE_RECONNECT_DELAY = 5  # seconds
MAX_RECONNECT_DELAY = 300 # seconds (5 minutes)
# KIS 주문 유형 코드 매핑
ORDER_TYPE_MAP = {
    "00": "지정가 주문",
    "01": "시장가 주문",
    "02": "조건부 지정가",
    "03": "최유리 지정가",
    "04": "최우선 지정가",
    "05": "장전 시간외",
    "06": "장후 시간외",
    "07": "시간외 단일가",
    "31": "LOO (시가 최유리)",
    "32": "MOO (시가 최유리 시장가)",
    "33": "MOC (종가 시장가)",
    "34": "LOC (종가 지정가)",
    "1": "시장가 주문",
    "2": "지정가 주문",
    "A": "MOO (장개시 시장가)",
    "B": "LOO (장개시 지정가)",
    "C": "MOC (장마감 시장가)",
    "D": "LOC (장마감 지정가)",
}

# 로거 설정
logger = logging.getLogger(__name__)

class KisWebSocketClient:
    def __init__(self, market: str = "US"):
        self.market = market.upper()
        
        # 시장별 환경변수 로드
        env_suffix = "kr" if self.market == "KR" else "us"
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env_path = os.path.join(project_root, "env", f".env.{env_suffix}")
        from dotenv import load_dotenv
        load_dotenv(env_path, override=True)

        base_url = os.getenv("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443")
        self.hts_id = os.getenv("KIS_HTS_ID")

        self._broker = None # [수정] 실제 연결 시점에 생성하도록 지연 로딩 처리
        # URL 설정 (실전/모의 구분)
        if "openapivts" in base_url:
            self.ws_url = "ws://ops.koreainvestment.com:31000" # 모의투자
            self.tr_id = "H0STCNI9" if self.market == "KR" else "H0GSCNI9"
            self.price_tr_id = "H0STCNT0" if self.market == "KR" else "HDFSCNT0"
        else:
            self.ws_url = "ws://ops.koreainvestment.com:21000" # 실전투자
            self.tr_id = "H0STCNI0" if self.market == "KR" else "H0GSCNI0"
            self.price_tr_id = "H0STCNT0" if self.market == "KR" else "HDFSCNT0"
            
        self.approval_key = None
        self.aes_key = None
        self.aes_iv = None
        self.running = False
        self.retry_attempts = 0
        self._websocket = None # [ADD] PINGPONG 응답 전송을 위해 저장
        self.ping_task = None      # [신규] 클라이언트 주도 핑 루프 태스크
        self._prev_close_cache = {} # [ADD] 전일종가 캐시 (REST 호출 방지)
        self._last_db_update = {}   # [ADD] DB 업데이트 쓰로틀링용 (stype, symbol, alias) -> timestamp
        self._last_cache_date = None
        self.tz = pytz.timezone('Asia/Seoul' if self.market == "KR" else 'America/New_York')
        self._cache_lock = threading.Lock() # 스레드풀 환경에서의 캐시 접근 제어용 락

    async def connect(self):
        """웹소켓 연결 및 데이터 수신 메인 루프"""
        # 시장별 타임존 설정
        if self.market == "KR":
            market_tz = pytz.timezone('Asia/Seoul')
            # 한국 정규장 시간: 09:00 ~ 15:30 (전후 1시간 연장)
            open_time = dtime(8, 0)
            close_time = dtime(15, 50) # [수정] 장 마감 보고(15:40) 및 후처리 완료 시점 고려
        else: # US Market
            market_tz = pytz.timezone('America/New_York')
            # 미국 정규장 시간: 09:30 ~ 16:00 (전후 1시간 연장) ==> 프리장~본장~애프트장 모두 활용함. 전후 1시간은 불필요함. 
            open_time = dtime(7, 0) # [수정] 07:00 ET (Summer Time: 20:00 KST)
            close_time = dtime(18, 0) # 18:00 ET (Summer Time: 07:00 KST 다음날)

        self.running = True

        connection_start_time = 0
        while self.running:
            try:
                # [추가] 스케줄러와의 레이스 컨디션 방지를 위해 루프 시작 시 미세 대기
                if self.retry_attempts == 0:
                    await asyncio.sleep(2)

                now_in_market_tz = datetime.now(market_tz)
                is_weekend = now_in_market_tz.weekday() >= 5

                # [수정] 각 인스턴스의 시장(self.market)에 해당하는 휴장 정보만 확인
                is_holiday = False
                if self.market == "KR":
                    is_holiday = get_config("kr_market_opnd_yn") == "N"
                elif self.market == "US":
                    is_holiday = get_config("us_market_opnd_yn") == "N"

                if is_weekend:
                    # [수정] 한국 시간(now_kst)이 아닌 각 시장의 현지 시간(now_in_market_tz)을 기준으로 다음 확인 시점을 계산합니다.
                    next_check = (now_in_market_tz + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
                    
                    sleep_seconds = (next_check - now_in_market_tz).total_seconds()
                    logger.info(f"📅 [WS] 오늘은 {self.market} 시장 주말입니다. 현지 시간 내일 오전 8시({next_check.strftime('%m/%d %H:%M')})까지 대기합니다.")
                    await asyncio.sleep(sleep_seconds)
                    continue
                
                if is_holiday:
                    # [수정] 휴장일인 경우 다음날 오전 8시까지 대기 (스팸 방지)
                    next_check = (now_in_market_tz + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
                    sleep_seconds = (next_check - now_in_market_tz).total_seconds()
                    
                    logger.info(f"📅 [WS] 오늘은 {self.market} 시장 휴장일(DB 설정)입니다. 현지 시간 내일 오전 8시({next_check.strftime('%m/%d %H:%M')})까지 대기합니다.")
                    await asyncio.sleep(sleep_seconds)
                    continue

                is_active_time = open_time <= now_in_market_tz.time() <= close_time
                if not is_active_time:
                    # 시장 운영 시간 외일 경우, 다음 개장 시간까지 남은 시간을 계산하여 대기
                    now_time = now_in_market_tz.time()
                    current_date = now_in_market_tz.date()
                    
                    next_open_dt = None
                    if now_time < open_time:
                        # 오늘 시장 개장 전
                        next_open_dt = market_tz.localize(datetime.combine(current_date, open_time))
                    else:
                        # 오늘 시장 마감 후, 또는 내일 시장 개장 전
                        next_open_dt = market_tz.localize(datetime.combine(current_date + timedelta(days=1), open_time))
                        # 주말 건너뛰기
                        while next_open_dt.weekday() >= 5: # 토요일(5) 또는 일요일(6)
                            next_open_dt += timedelta(days=1)
                    
                    sleep_seconds = (next_open_dt - now_in_market_tz).total_seconds()
                    
                    # 적절한 대기 시간 및 로그 메시지 결정
                    if sleep_seconds > 4 * 3600: # 4시간 이상 남았다면 4시간 대기
                        actual_sleep = 4 * 3600
                        log_msg = f"[WS] {self.market} 시장 운영 시간 외입니다. (현재: {now_in_market_tz.strftime('%H:%M')}) 4시간 후 재확인합니다."
                    elif sleep_seconds > 3600: # 1시간 이상 남았다면 1시간 대기
                        actual_sleep = 3600
                        log_msg = f"[WS] {self.market} 시장 운영 시간 외입니다. (현재: {now_in_market_tz.strftime('%H:%M')}) 1시간 후 재확인합니다."
                    elif sleep_seconds > 600: # 10분 이상 남았다면 10분 대기
                        actual_sleep = 600
                        log_msg = f"[WS] {self.market} 시장 운영 시간 외입니다. (현재: {now_in_market_tz.strftime('%H:%M')}) 10분 후 재확인합니다."
                    else: # 10분 미만 남았다면 남은 시간 대기 (최소 1분)
                        actual_sleep = max(60, sleep_seconds)
                        log_msg = f"[WS] {self.market} 시장 운영 시간 외입니다. (현재: {now_in_market_tz.strftime('%H:%M')}) {int(actual_sleep/60)}분 후 재확인합니다."
                    
                    logger.info(log_msg)
                    await asyncio.sleep(actual_sleep)
                    continue

                # [ADD] 시장 운영 시간에 진입한 경우에만 브로커 인스턴스 생성 (불필요한 야간 알림 방지)
                if self._broker is None:
                    from core.brokers.kis_kr import KisKrBroker
                    from core.brokers.kis_us import KisUsBroker
                    self._broker = KisKrBroker() if self.market == "KR" else KisUsBroker()
                    logger.info(f"🚀 [WS] {self.market} 실시간 감시를 위한 브로커를 시작합니다.")

                if not self.hts_id:
                    logger.warning("[WS] KIS_HTS_ID 누락.")
                    return
                logger.debug(f"[WS] {self.market} Approval Key 발급 시도...")

                # 연결 시도 직전에 키 발급
                self.approval_key = get_websocket_approval_key(market=self.market)
                if not self.approval_key:
                    logger.error(f"[WS] {self.market} Approval Key 발급 실패. 1분 후 재시도.")
                    await asyncio.sleep(60)
                    continue

                logger.info(f"[WS] {self.market} 웹소켓 연결 시도 중... (URL: {self.ws_url})")
                logger.debug(f"[WS] {self.market} WebSocket connection parameters: URL={self.ws_url}, ping_interval=None")
                # [수정] KIS 서버는 자체 PINGPONG 메시지를 사용하므로, 라이브러리 레벨의 binary ping은 비활성화합니다.
                async with websockets.connect(self.ws_url, ping_interval=None, close_timeout=10) as websocket:
                    self._websocket = websocket
                    connection_start_time = time.time()
                    logger.info(f"✅ [WS] {self.market} 웹소켓 서버에 연결되었습니다. (Approval Key: {self.approval_key[:5]}...)")
                    
                    # [신규] 클라이언트 주도 PING 루프 태스크 시작
                    self.ping_task = asyncio.create_task(self._ping_loop(websocket))
                    
                    # 체결 통보 구독 요청
                    await self.subscribe(websocket)
                    
                    # [ADD] 실시간 시세 구독 요청 (CA 전략 종목 대상)
                    await self.subscribe_prices(websocket)

                    # 메시지 수신 루프
                    while self.running:
                        try:
                            # [수정] 정규장 운영 시간 종료 시 연결 자동 해제 (KR/US 간 HTS_ID 충돌 방지)
                            now_time = datetime.now(market_tz).time()
                            if not (open_time <= now_time <= close_time):
                                logger.info(f"⏰ [WS] {self.market} 정규장 운영 시간 종료. 연결을 해제합니다. (현재: {now_time.strftime('%H:%M')})")
                                break

                            # [수정] recv()에 타임아웃을 설정하여 주기적으로 장 운영 시간을 체크하고 PINGPONG을 처리하도록 함
                            msg = await asyncio.wait_for(websocket.recv(), timeout=30)
                            logger.debug(f"[WS] {self.market} Received raw message: {msg[:100]}...") # Log first 100 chars of raw message
                            await self.on_message(msg)
                        except asyncio.TimeoutError:
                            logger.debug(f"[WS] {self.market} asyncio.TimeoutError: No message received for 30 seconds. Checking market active time.")
                            # 데이터 수신이 없어도 루프를 돌며 시간 체크를 지속함. PINGPONG은 on_message에서 처리됨.
                            continue
                        except websockets.ConnectionClosed as e:
                            now_in_market_tz = datetime.now(market_tz)
                            if not (open_time <= now_in_market_tz.time() <= close_time):
                                logger.info(f"⏰ [WS] {self.market} 정규장 운영 시간 종료 후 연결 끊김. 재연결하지 않고 대기합니다.")
                                self.running = False # 루프 종료
                                break

                            # [개선] 장 마감 직후 발생하는 1006 에러는 재연결 없이 종료
                            if e.code == 1006 and (now_in_market_tz.time() > close_time or now_in_market_tz.time() < open_time):
                                logger.info(f"🏁 [WS] {self.market} 장 종료 시간대의 세션 종료(1006) 감지. 재연결을 중단합니다.")
                                self.running = False
                                break
                            
                            # [수정] ConnectionClosed도 공통 에러 처리 로직을 타도록 Exception으로 전달
                            raise e
                            break
                        
                        # [추가] 과도한 재연결 방지를 위한 미세 대기
                        # This sleep is inside the inner loop, so it will execute after each message.
                        # It might be better to remove it or make it conditional if message frequency is high.
                        # For debugging, let's keep it for now.
                        await asyncio.sleep(0.1)

            except websockets.InvalidStatusCode as e:
                self._stop_ping_loop()
                if e.status_code == 403:
                    logger.error(f"[WS] 접근 거부(403). API 호출 한도 초과 가능성. 5분 후 재시도합니다.")
                    await asyncio.sleep(300)
                else:
                    await asyncio.sleep(10)
                logger.error(f"[WS] {self.market} websockets.InvalidStatusCode: {e.status_code}, Reason: {e.reason}")
            except Exception as e:
                self._stop_ping_loop()
                # [수정] 연결 유지 시간 확인 (5분 이상 유지되었다면 횟수 초기화, 아니면 누적)
                duration = time.time() - connection_start_time
                if duration > 300:
                    self.retry_attempts = 1
                else:
                    self.retry_attempts += 1

                # Calculate exponential backoff delay with jitter
                delay = min(MAX_RECONNECT_DELAY, BASE_RECONNECT_DELAY * (2 ** (self.retry_attempts - 1)))
                jitter = random.uniform(0.8, 1.2) # Add 20% jitter
                wait_time = delay * jitter

                # [요청 반영] 재연결 시도 중 또 끊어지면 최소 1분 대기 (retry_attempts가 2 이상일 때)
                if self.retry_attempts >= 2:
                    wait_time = max(60, wait_time)
                    logger.warning(f"⚠️ [WS] {self.market} 빈번한 연결 끊김 감지. 안정화를 위해 {wait_time:.0f}초간 대기 후 재시도합니다.")

                # Log and send alert for significant retry attempts
                if self.retry_attempts <= 3 or self.retry_attempts % 5 == 0:
                    send_telegram_message(f"⚠️ <b>[WS 재연결 경고]</b> {self.market} 웹소켓 연결이 {self.retry_attempts}회 연속 실패했습니다.\n사유: {type(e).__name__}\n다음 시도까지 {wait_time:.0f}초 대기합니다.")
                
                logger.error(f"[WS] {self.market} 연결 오류 발생 ({self.retry_attempts}회): {e}. {wait_time:.0f}초 후 재시도.")
                await asyncio.sleep(wait_time)

        self._stop_ping_loop()


    async def _ping_loop(self, websocket):
        """클라이언트 주도의 주기적인 PINGPONG 메시지 전송 루프 (2분 간격)"""
        logger.info(f"[WS] {self.market} 클라이언트 주도 PING 루프를 시작합니다.")
        try:
            while self.running:
                await asyncio.sleep(120)  # 2분 간격
                if websocket.open:
                    ping_msg = {
                        "header": {
                            "tr_id": "PINGPONG",
                            "datetime": datetime.now().strftime("%Y%m%d%H%M%S")
                        }
                    }
                    logger.debug(f"[WS] {self.market} Sending client-initiated PING...")
                    await websocket.send(json.dumps(ping_msg))
        except asyncio.CancelledError:
            logger.info(f"[WS] {self.market} PING 루프가 취소되었습니다.")
        except Exception as e:
            logger.error(f"[WS] {self.market} PING 루프 오류 발생: {e}")

    def _stop_ping_loop(self):
        """실행 중인 PING 루프 태스크가 있다면 취소합니다."""
        if hasattr(self, 'ping_task') and self.ping_task and not self.ping_task.done():
            self.ping_task.cancel()
            logger.info(f"[WS] {self.market} 기존 PING 루프 취소 요청 완료.")

    async def subscribe(self, websocket):
        """체결 통보 구독 요청"""
        data = {
            "header": {
                "approval_key": self.approval_key,
                "custtype": "P",
                "tr_type": "1", # 1: 등록
                "content-type": "utf-8"
            },
            "body": {
                "input": {
                    "tr_id": self.tr_id,
                    "tr_key": self.hts_id
                }
            }
        }
        await websocket.send(json.dumps(data))
        logger.info(f"[WS] 체결 통보 구독 요청 전송 (HTS_ID: {self.hts_id})")

    async def subscribe_prices(self, websocket):
        """장중 감시 대상 종목의 실시간 시세 구독"""
        # [수정] 모든 활성 전략(CA, VR)의 종목을 중복 없이 추출하여 구독
        all_states = get_all_states_db(market=self.market)
        active_symbols = set(s['symbol'] for s in all_states if s.get('is_active', True))
        for symbol in active_symbols:
            tr_key = symbol
            if self.market == "US":
                # 미국 시장은 D + 시장구분(NAS/NYS/AMS) + 종목코드 형식
                if symbol in ['TQQQ', 'SQQQ', 'QLD', 'TMF']: # 나스닥
                    tr_key = f"DNAS{symbol}"
                elif symbol in ['SOXL', 'SOXS', 'LABU', 'FNGU']: # 아멕스
                    tr_key = f"DAMS{symbol}"
                else: # 뉴욕 및 기타
                    tr_key = f"DNYS{symbol}"

            data = {
                "header": {
                    "approval_key": self.approval_key,
                    "custtype": "P",
                    "tr_type": "1",
                    "content-type": "utf-8"
                },
                "body": {
                    "input": {
                        "tr_id": self.price_tr_id,
                        "tr_key": tr_key
                    }
                }
            }
            await websocket.send(json.dumps(data))
            logger.info(f"[WS] {self.market} {symbol} 실시간 시세 구독 시작")
            await asyncio.sleep(0.1) # [ADD] 구독 요청 간 딜레이 추가 (트래픽 분산)

    async def on_message(self, message):
        """메시지 수신 및 처리"""
        # 첫 데이터가 암호화되지 않은 텍스트라고 가정 (주로 0/1로 시작)
        if message[0] in ['0', '1']:
            logger.debug(f"[WS] {self.market} Real-time Data Frame received.")
            parts = message.split('|')
            if len(parts) > 2 and parts[1] == self.price_tr_id:
                # 실시간 시세 데이터 파싱 및 급락 감시 (메인 루프 블로킹 방지를 위해 스레드풀 활용)
                asyncio.create_task(asyncio.to_thread(self.handle_realtime_price, message))
                return

            # 실데이터 파싱
            asyncio.create_task(asyncio.to_thread(self.parse_execution_data, message))
        else:
            # JSON 응답 (구독 성공 메시지 등)
            try:
                data = json.loads(message)
                if data.get('header', {}).get('tr_id') == 'PINGPONG':
                    logger.debug(f"[WS] {self.market} PINGPONG Echo sending...")
                    await self._websocket.send(message)
                    return
                
                logger.info(f"[WS] {self.market} Control Message received: {message}")
                msg_code = data.get('body', {}).get('msg1')
                # [수정] 중복 접속(ALREADY IN USE) 처리 로직 강화
                if msg_code and "ALREADY IN USE" in msg_code:
                    logger.warning(f"⚠️ [WS] {self.market} 중복 접속 감지! 기존 세션 종료 대기를 위해 2분간 대기합니다.")
                    await asyncio.sleep(120)
                    # 예외를 발생시켜 외부 루프에서 재연결하도록 유도
                    raise Exception(f"WebSocket session already in use for {self.market}")

                if msg_code:
                    logger.info(f"[WS] {self.market} 시스템 메시지: {msg_code}")

                # 구독 성공 시 AES Key/IV 추출
                if data.get('header', {}).get('tr_id') == self.tr_id:
                    output = data.get('body', {}).get('output', {})
                    logger.info(f"[WS] {self.market} AES Key/IV 추출 성공. Key: {output.get('key')[:5]}..., IV: {output.get('iv')[:5]}...")
                    self.aes_key = output.get('key')
                    self.aes_iv = output.get('iv')
            except:
                pass

    def aes_256_cbc_decode(self, cipher_text):
        """AES256 CBC 모드 복호화"""
        try:
            if not self.aes_key or not self.aes_iv:
                return None
            cipher = AES.new(self.aes_key.encode('utf-8'), AES.MODE_CBC, self.aes_iv.encode('utf-8'))
            logger.debug(f"[WS] Decrypting: {cipher_text[:50]}... with key={self.aes_key[:5]}..., iv={self.aes_iv[:5]}...")
            # Base64 디코딩 후 복호화
            decoded_data = cipher.decrypt(base64.b64decode(cipher_text))
            # 패딩 제거 (PKCS7 형식 대응)
            padding_len = decoded_data[-1]
            return decoded_data[:-padding_len].decode('utf-8')
        except Exception as e:
            logger.error(f"[WS] 복호화 실패: {e}")
            logger.debug(f"[WS] Cipher text: {cipher_text}")
            return None

    def handle_realtime_price(self, message):
        """실시간 시세를 이용한 장중 급락 감시 로직"""
        try:
            parts = message.split('|')
            data_fields = parts[3].split('^')
            
            if self.market == "KR":
                # [KR] H0STCNT0 레이아웃: 0:단축종목코드, 2:현재가
                symbol = data_fields[0]
                current_price = float(data_fields[2]) if data_fields[2] else 0.0
            else:
                # [US] HDFSCNT0 레이아웃: 1:종목코드, 11:현재가(LAST), 14:등락율(RATE)
                symbol = data_fields[1]
                current_price = float(data_fields[11]) if data_fields[11] else 0.0
            
            if current_price <= 0: return

            # [ADD] 날짜 변경 시 캐시 초기화
            today = datetime.now(self.tz).date()
            if self._last_cache_date != today:
                self._prev_close_cache.clear()
                self._last_cache_date = today

            # [ADD] 전일종가 캐싱 (틱마다 REST API 호출 방지)
            cache_key = f"{self.market}_{symbol}"
            with self._cache_lock:
                if cache_key not in self._prev_close_cache:
                    try:
                        pc = self._broker.get_previous_close(symbol)
                        self._prev_close_cache[cache_key] = pc if pc and pc > 0 else 0.01 # 0 방지
                        logger.info(f"[WS] Cached prev_close for {cache_key}: {self._prev_close_cache[cache_key]}")
                    except Exception as e:
                        logger.error(f"[WS] 전일종가 조회 실패 ({cache_key}): {e}")
                        self._prev_close_cache[cache_key] = 0.01 # 실패 시에도 최소값 할당하여 재호출 방지

            # [수정] 해당 종목을 사용하는 모든 전략(CA, VR)의 현재가 정보를 DB에 업데이트
            target_states = get_all_states_db(symbol=symbol, market=self.market)
            
            for state_data in target_states:
                if not state_data.get('is_active', True): continue
                
                stype = state_data.get('strategy_type')
                alias = state_data.get('strategy_name', '')
                update_key = (stype, symbol, alias)
                now = time.time()
                
                # [ADD] DB 업데이트 쓰로틀링 (10초에 한 번만 저장하여 IO 부하 감소)
                should_save = (now - self._last_db_update.get(update_key, 0)) > 10

                # [FIX] DB 전용 필드인 updated_at이 데이터 클래스 생성자에 전달되지 않도록 제거
                state_data.pop('updated_at', None)

                if stype == "CA":
                    config = CAConfig(symbol=symbol, use_db=True, market=self.market, strategy_name=alias)
                    engine = CostAveragingEngine(config, broker=self._broker)
                    
                    # 장중 감시 실행 (내부에서 매수 발생 시 DB 저장함)
                    engine.run_intraday_check(current_price=current_price, prev_close=self._prev_close_cache.get(cache_key))
                    
                    # [수정] Stale한 state_data 대신 engine.state를 사용하여 저장 (last_execution_price 보존)
                    target_state = engine.state
                elif stype == "VR":
                    target_state = VRState(**state_data)

                if should_save:
                    target_state.current_price = current_price
                    save_state_db(target_state, market=self.market, strategy_name=alias, only_if_exists=True)
                    self._last_db_update[update_key] = now

        except Exception as e:
            logger.error(f"[WS] 실시간 시세 처리 오류: {e}")

    def parse_execution_data(self, message):
        """
        해외주식 체결 통보 데이터 파싱
        형식: 구분|데이터1|데이터2|...
        """
        try:
            parts = message.split('|')
            if len(parts) < 4: return
            
            tr_id = parts[1]
            is_encrypted = message[0] == '1'

            if tr_id == self.tr_id:
                raw_data = parts[3]
                
                # [ADD] 암호화된 경우 복호화 수행
                if is_encrypted:
                    raw_data = self.aes_256_cbc_decode(raw_data)
                    if not raw_data: return
                logger.debug(f"[WS] {self.market} {self.tr_id} Decrypted Data: {raw_data}") # 디버깅을 위한 raw_data 로깅
                logger.debug(f"[WS] {self.market} {self.tr_id} Raw Data: {raw_data}") # 디버깅을 위한 raw_data 로깅

                fields = raw_data.split('^')

                # 시장별 레이아웃 차이 대응
                if self.market == "KR":
                    # [KR] H0STCNI0 레이아웃 (문서 기준)
                    odno = str(fields[2])
                    order_type = "SELL" if fields[4] == "01" else "BUY" # 01:매도, 02:매수 (Index 4)
                    # [NML] 심볼 정규화 (A 제거)
                    symbol = str(fields[8]).strip().upper().lstrip('A') 
                    exec_qty = int(float(fields[9])) # CNTG_QTY
                    exec_price = float(fields[10]) # CNTG_UNPR
                    exec_time = fields[11] # STCK_CNTG_HOUR
                    order_div_cd = fields[6].strip() # ODER_KIND (Index 6) - KIS 문서 기준
                    order_div_nm = "" # 명칭은 맵핑 테이블 사용
                    cntg_div_raw = fields[13] # CNTG_YN (1:주문/접수, 2:체결) (Index 13)
                    cntg_div = "02" if cntg_div_raw == "2" else "01"
                else:
                    # [US] H0GSCNI0 레이아웃 (문서 기준)
                    odno = str(fields[2])
                    order_type = "SELL" if fields[4] in ["01", "03"] else "BUY" # Index 4 (SELN_BYOV_CLS)
                    # [NML] 심볼 정규화
                    symbol = str(fields[7]).strip().upper()
                    exec_qty = int(float(fields[8])) if fields[8] else 0 # CNTG_QTY
                    # [중요] 미국 주식은 소수점 4자리 처리 (문서 가이드 반영)
                    exec_price = float(fields[9]) / 10000.0 if fields[9] else 0.0 # CNTG_UNPR
                    order_div_cd = str(fields[6]).strip() # ODER_KIND2 (Index 6)
                    order_div_nm = ""
                    exec_time = fields[10] # STCK_CNTG_HOUR (Index 10)
                    cntg_div_raw = fields[12] # CNTG_YN (1:주문/접수, 2:체결) (Index 12)
                    cntg_div = "02" if cntg_div_raw == "2" else "01"

                logger.debug(f"[WS] {self.market} Parsed Fields: odno={odno}, order_type={order_type}, symbol={symbol}, exec_qty={exec_qty}, exec_price={exec_price}, cntg_div={cntg_div}")

                # [신규] 텔레그램 메시지 포맷팅 강화 로직
                is_buy = (order_type == "BUY")
                side_icon = "🔵" if is_buy else "🔴"
                status_icon = "✅" if cntg_div == "02" else "📝"
                status_text = "체결 완료" if cntg_div == "02" else "주문상태"
                log_prefix = "체결" if cntg_div == "02" else "주문"
                msg_title = f"{status_icon}{side_icon} [{self.market} {status_text}: {order_type}]"

                # 체결 데이터가 없는 경우 (주문 접수 등) 스킵 (US)
                if self.market == "US" and cntg_div == "02" and (exec_qty <= 0 or exec_price <= 0): # [수정] US 시장 체결 데이터 부족 시 경고 로깅
                    logger.warning(f"[WS] {self.market} 체결 데이터 부족으로 실시간 알림 스킵 (ODNO: {odno}, Qty: {exec_qty}, Price: {exec_price}). 원본: {raw_data}")
                    return

                # [ADD] 주문 유형(지정가 등) 정보 보완: 웹소켓에 없거나 부정확할 경우 DB에서 조회
                if not order_div_cd or order_div_cd.isdigit() and len(order_div_cd) > 2:
                    from core.database import get_connection
                    conn = get_connection()
                    cursor = conn.cursor()
                    cursor.execute("SELECT type FROM order_history WHERE odno=?", (odno,))
                    db_res = cursor.fetchone()
                    if db_res:
                        order_div_cd = db_res[0]
                    conn.close()

                if symbol:
                    cur_sym = "₩" if self.market == "KR" else "$"
                    # 체결 시각 포맷팅 (HHMMSS -> YYYY-MM-DD HH:MM:SS)
                    today_str = datetime.now().strftime("%Y-%m-%d")
                    if exec_time and len(exec_time) == 6:
                        formatted_exec_time = f"{today_str} {exec_time[:2]}:{exec_time[2:4]}:{exec_time[4:6]}"
                    else:
                        # API에서 시간이 오지 않거나 형식이 다를 경우 수신 시각 표시
                        formatted_exec_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    
                    # 가격 포맷팅 (KR은 소수점 제거)
                    price_fmt = f"{int(exec_price):,}" if self.market == "KR" else f"{exec_price:,.2f}"
                    
                    symbol_display = format_symbol_display(symbol, self.market)
                    side_name = "매수" if is_buy else "매도"
                    
                    # [수정] 주문 유형 이름 가독성 향상 (코드와 명칭 함께 표시)
                    # US 시장의 경우 코드가 '1'과 같이 1자리로 올 수 있으므로 zfill(2) 적용
                    lookup_cd = order_div_cd.zfill(2) if order_div_cd.isdigit() else order_div_cd
                    name_from_map = ORDER_TYPE_MAP.get(lookup_cd)
                    order_type_display = f"{order_div_cd} {name_from_map}" if name_from_map else (f"{order_div_cd} ({order_div_nm})" if order_div_nm else order_div_cd)
                    
                    # 로그 기록
                    log_msg = f"[WS {log_prefix}] [{self.market}] {symbol_display} {side_name} {log_prefix} ({order_type_display})! {exec_qty}주 @ {cur_sym}{price_fmt} ({exec_time})"
                    logger.info(log_msg)
                    
                    # 텔레그램 전송 (주문 유형 포함)
                    tg_msg = (
                        f"⚡ <b>{msg_title}</b>\n"
                        f"일시: {formatted_exec_time if formatted_exec_time else exec_time}\n"
                        f"종목: <b>{symbol_display}</b>\n"
                        f"구분: {side_name}\n"
                        f"수량: {exec_qty}주\n"
                        f"가격: {cur_sym}{price_fmt}"
                    )
                    if order_type_display:
                        tg_msg += f"\n유형: {order_type_display}"
                    send_telegram_message(tg_msg) # type: ignore
                    
                    # 3. 실제 체결(02)인 경우에만 로컬 상태 업데이트 및 거래 기록
                    if cntg_div == "02":
                        self.process_trade_update(symbol, order_type, exec_price, exec_qty, formatted_exec_time, odno=odno)
                    # 주문 접수(01) 단계에서는 DB에서 삭제하지 않고 체결(02) 시에만 처리함
                    
        except Exception as e:
            logger.error(f"[WS] 데이터 파싱 오류: {e} | 원본: {message[:100]}...")

    def process_trade_update(self, symbol, order_type, price, qty, time_str, odno=None):
        """체결 정보를 바탕으로 DB 업데이트 (VR 우선 확인)"""
        # 0. 체결이 확인되었으므로 미체결 내역에서 즉시 삭제 (중복 표시 방지 강화)
        if odno:
            delete_order_by_odno_db(odno)

        # 주문번호로 어떤 전략의 주문이었는지 확인
        from core.database import get_connection, get_all_states_db
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT strategy, strategy_name FROM order_history WHERE odno=?", (odno,))
        order_info = cursor.fetchone()

        # Determine the target strategy for state update
        # If order_info is None, it means the order was not placed by a known strategy (e.g., manual, or strategy deleted)
        # In this case, we should not update any specific strategy's state.
        if order_info:
            target_strategy_type = order_info[0] # e.g., "CA", "VR"
            target_strategy_alias = order_info[1] # e.g., "TQQQ_1차"
            strategy_found_for_log = target_strategy_type # For logging to trade_history
        elif symbol:
            # [보완] 주문번호 매칭 실패 시, 현재 활성 전략 중 해당 종목을 사용하는 전략을 찾아 연결
            cursor.execute("SELECT strategy, strategy_name FROM strategy_state WHERE (symbol=? OR symbol LIKE ?) AND market=? AND is_active=1 LIMIT 1", (symbol, f"{symbol} %", self.market))
            fallback_info = cursor.fetchone()
            if fallback_info:
                target_strategy_type, target_strategy_alias = fallback_info
                strategy_found_for_log = target_strategy_type
            else:
                target_strategy_type, target_strategy_alias, strategy_found_for_log = None, None, "MANUAL"
        else:
            target_strategy_type, target_strategy_alias, strategy_found_for_log = None, None, "MANUAL"
        
        conn.close()

        # [수정] 잔고 조회 실패 시에 대한 안전한 처리 (strategy_name 추가)
        equity_res = self._broker.get_account_equity(symbol, strategy_name=target_strategy_alias)
        real_avg_price = 0.0
        if equity_res and equity_res[1] is not None:
            real_avg_price = equity_res[1]
        
        # Profit calculation variables
        avg_price_for_profit = real_avg_price # 실제 계좌 평단가 우선 사용
        realized_profit = 0.0
        realized_profit_rate = 0.0

        # Only proceed to update strategy state if a specific strategy was identified for the order
        if target_strategy_type and target_strategy_alias:
            # Load the specific strategy state
            state_data = load_state_db(symbol, target_strategy_type, market=self.market, strategy_name=target_strategy_alias)
            
            if state_data:
                # 만약 계좌 조회 평단가가 0이라면 DB에 저장된 예전 평단가라도 사용 (백업)
                if avg_price_for_profit <= 0:
                    avg_price_for_profit = float(state_data.get('avg_price', 0.0))
                
                # [FIX] 여전히 0이라면 trade_history 테이블에서 마지막 평단가 역추적 (전량 매도 시점 대응)
                if avg_price_for_profit <= 0:
                    cursor.execute('''
                        SELECT avg_price FROM trade_history 
                        WHERE symbol=? AND market=? AND strategy_name=? AND avg_price > 0 
                        ORDER BY date DESC, id DESC LIMIT 1
                    ''', (symbol, self.market, target_strategy_alias))
                    row_th = cursor.fetchone()
                    if row_th:
                        avg_price_for_profit = row_th[0]

                try:
                    if target_strategy_type == "VR":
                        vr_state = VRState(**state_data)
                        current_pool = float(vr_state.pool)
                        trade_amt = price * qty
                        
                        if order_type == "BUY":
                            vr_state.pool = current_pool - trade_amt
                        elif order_type == "SELL":
                            vr_state.pool = current_pool + trade_amt
                        
                        save_state_db(vr_state, market=self.market, strategy_name=vr_state.strategy_name)
                        cur_sym = "₩" if self.market == "KR" else "$"
                        symbol_display = format_symbol_display(symbol, self.market)
                        msg = f"💰 [{self.market} VR 잔고 보고] {symbol_display} ({vr_state.strategy_name})\nPool: {cur_sym}{current_pool:,.2f} -> <b>{cur_sym}{vr_state.pool:,.2f}</b>"
                        logger.info(msg.replace('\n', ' '))
                        send_telegram_message(msg)
                        
                    elif target_strategy_type == "CA":
                        ca_state = CAState(**state_data)
                        current_shares = float(ca_state.total_shares)
                        current_avg = float(ca_state.avg_price)
                        current_pool = float(ca_state.pool) if hasattr(ca_state, 'pool') else 0.0
                        trade_amt = price * qty
                        
                        if order_type == "BUY":
                            new_shares = current_shares + qty
                            if new_shares > 0:
                                new_avg = ((current_shares * current_avg) + (qty * price)) / new_shares
                            else:
                                new_avg = price
                            
                            ca_state.total_shares = new_shares
                            ca_state.avg_price = new_avg
                            ca_state.last_execution_price = price # 장중 매수 기준점 업데이트
                            ca_state.pool = current_pool - trade_amt # [추가] 예수금 차감
                            
                            unit_buy = float(ca_state.unit_buy_amount)
                            if unit_buy > 0:
                                invested = new_shares * new_avg
                                ca_state.current_turn = math.ceil((invested / unit_buy) * 10) / 10.0
                            
                        elif order_type == "SELL":
                            new_shares = max(0, current_shares - qty)
                            ca_state.total_shares = new_shares
                            ca_state.pool = current_pool + trade_amt # [추가] 예수금 합산
                            
                            if new_shares == 0:
                                # [수정] 장중 전량 매도 시 즉시 초기화하지 않고 다음 날 차수 전환을 위해 플래그 설정
                                ca_state.pending_cycle_transition = True
                                logger.info(f"🚩 [WS] {symbol} 전량 매도 확인. 다음 날 장 시작 시 차수 전환이 진행됩니다.")
                                # 평단가는 이익 계산을 위해 유지하고 shares만 0으로 업데이트
                        
                        save_state_db(ca_state, market=self.market, strategy_name=ca_state.strategy_name)
                        symbol_display = format_symbol_display(symbol, self.market)
                        logger.info(f"[WS] CA 잔고 및 상태 업데이트 완료: {symbol_display} ({ca_state.strategy_name}) (T: {ca_state.current_turn})")
                except Exception as e:
                    logger.error(f"[WS] {target_strategy_type} 상태 업데이트 실패: {e}")

        # 수수료 및 세금 로드 (환경변수 또는 사용자 요청 기본값)
        fee_rate = float(os.getenv("fee_rate", "0.000038" if self.market == "KR" else "0.0009"))
        tax_rate = float(os.getenv("tax_sell", "0.0020" if self.market == "KR" else "0.0000206"))

        # Calculate Realized Profit for SELL
        fee_val = 0.0
        tax_val = 0.0
        principal = price * qty
        if order_type == "SELL":
            fee_val = round(principal * fee_rate, 2)
            # 한국 세금 또는 미국 SEC Fee 계산
            tax_val = round(principal * tax_rate, 2)
            
            total_amt = principal - fee_val - tax_val
            
            if avg_price_for_profit > 0:
                # 평단가 기반 원금 + 매수 시 발생했던 추정 수수료 포함
                cost_basis = avg_price_for_profit * qty * (1 + fee_rate)
                realized_profit = total_amt - cost_basis
                if cost_basis > 0:
                    realized_profit_rate = (realized_profit / cost_basis) * 100
        else:
            fee_val = round(principal * fee_rate, 2)
            total_amt = principal + fee_val

        # 3. DB 거래 내역 저장
        try:
            # Requirement 4: 현재 별칭의 누적 매수/매도액 합산
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT SUM(total_amount) FROM trade_history WHERE strategy_name=? AND symbol=? AND market=? AND side='BUY'", (target_strategy_alias, symbol, self.market))
            c_buy = (cursor.fetchone()[0] or 0.0) + (total_amt if order_type == "BUY" else 0.0)
            cursor.execute("SELECT SUM(total_amount) FROM trade_history WHERE strategy_name=? AND symbol=? AND market=? AND side='SELL'", (target_strategy_alias, symbol, self.market))
            c_sell = (cursor.fetchone()[0] or 0.0) + (total_amt if order_type == "SELL" else 0.0)
            conn.close()

            # [수정] date 인자에도 시각을 포함하여 저장 (timestamp 필드 제거 반영)
            # 만약 time_str에 날짜 정보가 포함되어 있다면 그것을 사용하고, 아니면 현재 시각 사용
            trade_date = time_str if (time_str and '-' in str(time_str)) else datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            log_trade_db(
                date=trade_date,
                symbol=symbol,
                strategy=strategy_found_for_log,
                side=order_type,
                price=price,
                qty=qty,
                fee=fee_val,
                total_amount=total_amt,
                turn=0.0, # 회차 정보는 정확히 알 수 없음
                note=f"Synced (ODNO: {odno})",
                odno=odno, 
                market=self.market,
                strategy_name=target_strategy_alias,
                avg_price=avg_price_for_profit,
                realized_profit=realized_profit,
                realized_profit_rate=realized_profit_rate,
                cum_buy_amt=c_buy,
                cum_sell_amt=c_sell            )
        except Exception as e:
            logger.error(f"[WS] 거래 내역 저장 실패: {e}")
