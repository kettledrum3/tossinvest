# Tossinvest CAVR Project Context
[2026년 6월 19일 금요일 (단오절)]
이 문서는 KIS(한국투자증권) Open API 기반의 CAVR(무한매수/밸류리밸런싱) 자동매매 엔진을 **토스증권(Toss) Open API**로 마이그레이션한 프로젝트 `tossinvest`(`토스증권 무매 리밸`)의 아키텍처 및 운용 컨텍스트를 정리합니다.

---

## 1. 프로젝트 개요
* **프로그램명**: 토스증권 무매 리밸 (tossinvestCAVR)
* **대상 증권사**: 토스증권(주) (tossinvest.com)
* **운용 대상 시장**: 한국 주식 시장(KRX), 미국 주식 시장(NYSE, NASDAQ 등)
* **연결 방식**: REST 기반 Open API (OAuth2 토큰 및 계좌 일련번호 인증)
* **주요 전략**: 
  - **CA**: Cost Averaging method (라오어 무한매수법 V2.2 / V4.0)
  - **VR**: Value Rebalancing method (라오어 밸류리밸런싱 실력공식)

---

## 2. 핵심 설계 및 마이그레이션 내용

### 2.1. [TossBroker](file:///d:/Python_D/tossinvest/core/brokers/toss.py) 구현
- KIS 브로커 인터페이스(`core/brokers/base.py`)를 완벽하게 승계하여 비즈니스 로직 수정 최소화.
- **인증 연동**: `client_id`와 `client_secret`을 통해 OAuth2 토큰을 발급받아 `api_tokens` DB에 저장 및 자동 갱신(만료 60초 전).
- **계좌 식별**: 종합계좌번호(`TOSS_ACCOUNT_NO`)를 기반으로 API 서버에 등록된 일련번호(`accountSeq`)를 자동으로 가져와 모든 호출에 `X-Tossinvest-Account` 헤더로 매핑.
- **KIS 규격 호환**: 미체결 조회(`fetch_open_orders`) 및 체결 이력 조회(`fetch_execution_history`)의 출력 스키마를 KIS가 반환하던 JSON 구조와 동일하게 마샬링하는 어댑터(Adapter) 패턴 적용.

### 2.2. 실시간 웹소켓 미지원 대응
- 토스증권 Open API는 현재 실시간 웹소켓(Websocket)을 지원하지 않습니다.
- **주기적 동기화**: 장중 실시간 모니터 스레드를 비활성화하고, 스케줄러(`scheduler.py`)를 통해 **매 15분마다** REST API로 미체결 및 체결 데이터를 가져와 DB에 백업 및 상태 동기화(`kr_hourly_sync`, `us_hourly_sync`).
- **체결 우회 동기화**: 토스 Open API는 종료된 주문 목록 조회를 차단(400 에러)하므로, 로컬 DB `order_history`에 `SUCCESS` 상태로 남아 있는 미체결 주문번호를 순회하며 개별 주문 API(`GET /api/v1/orders/{orderId}`) 상세 조회를 수행해 체결 이력을 동기화하도록 우회 설계했습니다.

### 2.3. 마감 매매 주문(LOC/MOC) 매핑
- **미국 시장 (US)**: 토스 Open API가 기본 제공하는 시간조건부 주문 유형 `LIMIT` + `CLS` (LOC 마감 지정가)를 사용해 LOC 주문 제출.
- **한국 시장 (KR)**: 토스가 LOC/MOC를 직접 지원하지 않으므로, 장 종료 직전(오후 3시 10분)에 스케줄러를 가동해 **LOC 모사 주문**(`job_kr_loc_simulation`)을 지정가 분할 매수로 전송.
- **MOC 주문**: 토스 API 미지원으로 인해 일반 시장가 주문(`MARKET` + `DAY`)으로 대체 즉시 체결 처리.

### 2.4. 환율 및 휴장일 판정
- KIS용 공공 API 또는 달력 크롤러 대신, 토스 공식 Open API의 캘린더 조회(`/api/v1/market-calendar/KR` 및 `/api/v1/market-calendar/US`)를 이용해 국내외 휴장일 판정을 처리합니다.
- 달러 환율 역시 `/api/v1/exchange-rate` API를 호출해 `USDKRW` DB 설정을 갱신합니다.

---

## 3. 주요 버그 해결 사항

### 3.1. 미국 주식 Ticker 접두사 `'A'` 훼손 버그 수정 (`core/database.py`)
- **기존 KIS 로직**: 한국 주식의 Ticker 포맷상 접두사 `'A'`가 붙는 규칙 때문에 `symbol.lstrip('A')` 정규화를 일괄 적용함.
- **문제점**: 미국 주식 중 `AAPL`, `AMZN` 등 `A`로 시작하는 종목들의 첫 글자가 훼손되어 `APL`, `MZN`으로 바뀌어 조회 에러가 나는 현상이 발생함.
- **수정**: `clean_symbol.startswith('A') and len(clean_symbol) == 7 and clean_symbol[1:].isdigit()` 인 한국 주식 규칙에 매칭될 때만 접두사 `'A'`를 떼어내도록 정규 표현식을 고도화함.

### 3.2. 신규 배포 시 sqlite3 OperationalError 해결 (`core/database.py`)
- **문제점**: `strategy_state` 테이블의 Primary Key 변경 마이그레이션 로직이 테이블이 아예 없는 최초 데이터베이스 기동 시에도 작동하여 `ALTER TABLE strategy_state RENAME TO strategy_state_old`를 호출해 크래시가 발생함.
- **수정**: `sqlite_master` 쿼리를 통해 테이블의 존재 여부를 먼저 확인한 뒤, 기존 테이블이 있을 때만 마이그레이션을 태우고 없을 경우 신규 스키마로 직접 생성되도록 안전 장치 적용.

### 3.3. 빈 종목코드(Symbol) API 호출 시 400 Client Error 버그 수정 (`core/brokers/toss.py`)
- **문제점**: 대시보드 구동 직후 혹은 백그라운드 동기화 과정에서 종목코드가 정해지지 않은 초기 시점(빈 문자열 `""` 혹은 `None` 상태)에 `get_price()`, `fetch_open_orders()` 등의 API 호출이 발생하여, Toss API 서버로부터 `400 Bad Request ("요청 필드가 올바르지 않습니다.")` 에러 응답 및 로그 에러가 폭증하는 현상이 발생함.
- **수정**: `toss.py` 내의 `symbol`을 파라미터로 갖는 모든 조회 및 주문 메서드들의 입구에 `if not symbol or not symbol.strip():` 방어 코드를 구현하여 빈 값일 경우 실제 HTTP 요청을 날리지 않고 즉시 기본값(0.0, `[]`, `None`, `False` 등)을 리턴하도록 처리하였으며, 전달된 `symbol` 인수는 일괄적으로 `.strip().upper()` 정형화 처리를 하여 비정상적인 파라미터 유입을 차단함.

### 3.4. 계좌 일련번호(accountSeq) 초기화 시 401 Unauthorized 무한 루프 버그 수정 (`core/brokers/toss.py`)
- **문제점**: 브로커 초기화 시점에 수행되는 계좌 일련번호(`accountSeq`) 조회 API(`GET /api/v1/accounts`)는 `_call_api` 공통 메서드를 거치지 않고 개별적으로 호출됨. 이 과정에서 DB에 캐싱된 토큰이 이미 만료되어 만료되지 않았다고 판단된 상태로 사용될 경우, `401 Unauthorized` 에러를 응답받아도 이를 탐지 및 무효화하지 못하고 무조건 3회 재시도 실패 후 기동 오류를 유발함.
- **수정**: `_init_account_seq()` 내 `requests.get` 응답 코드에 `401 Unauthorized` 감지 로직을 추가하여, 401 에러 감지 시 즉시 로컬 DB의 토큰 캐시를 삭제(`delete_api_token_db`)하고 다음 루프 시 새로운 토큰을 발급받도록 수정하여 회복성(Resilience)을 보장함.

---

## 4. 환경 및 설정 제약 사항
- **환경 변수**: 한국/미국 주식이 동일한 키와 종합 계좌를 사용하므로 `.env`에 공통으로 선언하고, 각 시장별 추가 설정(수수료, 세율 등)은 `env/.env.kr` 및 `env/.env.us`에 각각 분리하여 유지합니다.
- **종목 파일**: `env/ETF_list_kr.txt`와 `env/ETF_list_us.txt` 파일은 사용자가 정의한 최종 파일이며, `Ticker "종목이름"` 포맷으로 통일되어 있으므로 절대로 덮어쓰거나 훼손하지 않습니다.

---

## 5. Docker 빌드 및 아키텍처 배포 설정 사항

### 5.1. Docker Hub API 및 인증 주입
- **Personal Access Token (PAT) 권한**: Docker Hub를 통한 이미지 업로드(Push)를 수행하기 위해서는 권한(Access permissions)이 **"Read & Write"** 또는 **Admin**인 토큰이 필요합니다. "Read-only" 토큰 사용 시 `insufficient scopes` 에러가 발생합니다.
- **보안 로그인**: WSL CLI 환경에서는 토큰 노출 최소화를 위해 `--password-stdin`을 사용하는 보안 로그인을 수행합니다.
  ```bash
  echo "DockerHub_Access_Token" | docker login -u simji3 --password-stdin
  ```

### 5.2. WSL2 환경 Docker Daemon 연동
- WSL 내에서 `docker.service not found` 혹은 소켓 연결 실패 에러 발생 시, Windows 호스트의 **Docker Desktop > Settings > Resources > WSL Integration**에서 현재 사용 중인 배포판(`GenMini`)에 대해 연동 활성화(ON) 조치가 필요합니다.

### 5.3. 멀티 아키텍처(ARM64) 배포 대응  [2026년 6월 20일 토요일]
- 배포 대상 서버(Synology NAS, Apple Silicon, AWS Graviton 등)가 `linux/arm64/v8` 아키텍처일 경우, 로컬(x86_64) 빌드 본을 그대로 풀(Pull)하면 아키텍처 불일치 에러가 발생합니다. 다음 세 가지 방법으로 해결합니다.
  1. **Buildx 멀티 빌드 (권장)**: `docker buildx`로 `linux/amd64`와 `linux/arm64/v8` 지원 이미지를 통합 빌드하여 Docker Hub에 푸시합니다.
     ```bash
     docker buildx create --use --name mybuilder
     docker buildx bootstrap
     docker buildx build --platform linux/amd64,linux/arm64/v8 -t simji3/toss-dashboard:latest --push .
     docker buildx build --platform linux/amd64,linux/arm64/v8 -t simji3/toss-scheduler:latest --push .
     ```
  2. **현지 서버 빌드**: `docker-compose.yml` 파일 내 각 서비스에 `build: .` 컨텍스트를 추가하고, 대상 서버 내부에서 직접 `docker-compose up -d --build`를 통해 현지 사양(ARM64)으로 자동 빌드 및 구동합니다.
  3. **에뮬레이션 구동**: `docker-compose.yml` 서비스 설정 아래 `platform: linux/amd64` 옵션을 정의하여 QEMU 에뮬레이터 모드로 강제 실행합니다.

---

## 6. Git / GitHub 저장소 설정

### 6.1. 원격 저장소 및 브랜치 구성
- **원격 저장소 주소**: `https://github.com/kettledrum3/tossinvest.git`
- **기본 브랜치**: `main`

### 6.2. 초기 저장 및 백업 순서
1. **로컬 저장소 초기화**:
   ```bash
   git init
   git branch -M main
   ```
2. **원격 저장소 추가**:
   ```bash
   git remote add origin https://github.com/kettledrum3/tossinvest.git
   ```
3. **코드 업로드**:
   ```bash
   git add .
   git commit -m "feat: KIS에서 토스증권 Open API로 마이그레이션 및 초기 설정 완료"
   git push -u origin main
   ```

### 6.3. 보안 관련 주의 사항
- `.gitignore` 설정을 통해 API Key, 계좌 비밀번호가 기재된 환경 변수 파일(`.env`, `env/.env.kr`, `env/.env.us`), DB 파일(`data/*.db`, `*.sqlite3`) 및 WSL 로컬 가상환경(`.venv/`, `env/`) 등 민감한 자산이 GitHub에 커밋되지 않도록 철저히 격리 관리합니다.

---

## 7. 주요 업데이트 및 요구사항 반영 [2026년 6월 22일 월요일]

### 7.1. Toss 계좌 예수금 및 잔고 격리성 확보
- 단일 Toss 종합계좌를 공유하여 여러 전략을 운용함에 따른 잔고 왜곡을 방지하기 위해, 계좌 평가 정보를 조회하는 `get_account_equity()` 및 `get_cumulative_buy_amount()` 호출 시 `strategy_name`을 인자로 전달받도록 고도화하였습니다.
- 주입된 `strategy_name`이 존재할 경우 실제 브로커 API 대신 로컬 DB의 `trade_history` 데이터를 시간순으로 누적 재구성하여 개별 전략 전용의 **가상 잔고 및 평단가**를 독자적으로 산출합니다.

### 7.2. 자연수(Integer) 수량 매매 규정
- 토스증권 공식 모바일 앱을 통한 소수점 매매(Fractional Trading) 및 다른 자동매매 프로그램들과의 수량 간섭을 차단하기 위해, 프로그램 내부에서 계산 및 주문되는 주식 수량을 자연수(integer)로 강제 정형화(cast)하였습니다. 수량이 1주 미만(예: 0.5주)인 경우 0주로 계산되어 자동 매매 대상에서 제외됩니다.

### 7.3. 지능형 예수금 모니터링 및 자동 일시정지
- 스케줄러 내에 4시간 주기로 작동하는 `job_monitor_deposits()` 모니터링 작업을 탑재하였습니다.
- 활성 전략들의 1회 필요 매수금 대비 실제 계좌 예수금이 부족한 경우 시스템 스케줄러 상태를 `"paused"`로 일시정지하고 텔레그램으로 경고를 발송합니다. 이후 예수금이 충전되면 자동으로 `"running"` 상태로 복구됩니다.
- **매도전용 구동 보존**: 예수금이 부족하더라도 해당 시장의 전략에 매도 가능한 주식 잔고(DB holdings)가 $0$보다 큰 경우, 매도 주문을 정상적으로 수행하기 위해 일시정지하지 않고 계속 프로그램을 구동하도록 예외처리하였습니다.

### 7.4. 환율 및 잔고부족 경고 최적화
- **환율 조회 최적화 및 소수점 정밀화**: 스케줄러 시작 시 무조건 수행되던 환율 업데이트 작업을 야간 시간대(KST 20:00 ~ 익일 06:00)로 제한하여 무의미한 낮 시간대 API 트래픽을 차단하였으며, 실제 API 수신 값(소수점 1~2자리 실수형 문자열)이 온전히 반영되도록 시스템 보고서 및 로깅 상의 환율을 소수점 두 자리로 완벽히 포맷팅(`f"{float(usd_krw):,.2f}"`) 처리하여 가독성을 높였습니다.
- **경고 격하**: 단순 예수금/잔고 부족으로 인한 주문 에러 시 `🚨 에러` 알림 대신 `⚠️ 경고`로 필터링하여 불필요한 시스템 비상 경보(텔레그램 오류 메시지)를 방지하였습니다.

### 7.5. 대시보드 강제 덮어쓰기 방지
- Streamlit 대시보드(`dashboard.py`) 상에서 사용자가 계좌 새로고침 버튼을 누를 때 계좌 전체의 실제 예수금으로 개별 전략의 pool 예산 설정값을 강제로 덮어쓰던 코드를 제거하여 전략 고유 예산 관리가 안전하게 보호됩니다.
- 대시보드의 계좌 정보 새로고침 시에도 `strategy_name`을 인자로 전달하여 각 전략별 가상 잔고와 평가금액이 오차 없이 시각화되도록 연동하였습니다.

---

## 8. 한국장 기동 시 401 Unauthorized 에러 자동 복구 보강 [2026년 6월 23일 화요일]

### 8.1. 계좌 일련번호(accountSeq) 초기화 시 401 무한 루프 버그 해결
- **문제점**: 브로커 초기화 도중 계좌 일련번호(`accountSeq`)를 직접 `requests.get`으로 조회할 때, DB에 보관된 API 토큰 캐시가 만료되었거나 비정상임에도 만료 판정을 우회하여 그대로 쓰이면 `401 Unauthorized` 에러를 응답받게 됩니다. 하지만 공통 API 호출 메서드(`_call_api`)와 달리 `_init_account_seq`에서는 401 수신 시 토큰 무효화(삭제) 및 재시도가 누락되어 기동 실패 및 동기화 루프 중단이 계속 발생했습니다.
- **수정**: `_init_account_seq()` 내에서 API 응답 결과가 `401`일 때 즉시 로컬 DB 토큰 캐시를 무효화(삭제) 처리하는 `delete_api_token_db`를 추가함으로써, 다음 재시도 루프에서 TOSS API로부터 신규 토큰을 강제 발급받도록 수정하였습니다.
- **검증**: 가짜 토큰을 강제 주입하여 401 에러를 고의 발생시키는 시뮬레이션 테스트 스크립트(`scratch/check_kr_accounts.py`)를 가동하여, 1회차 401 감지 즉시 토큰이 정상적으로 무효화되고 2회차에 새로운 정상 토큰을 재발행받아 계좌 일련번호(`accountSeq: 1`) 조회를 완벽하게 성공하는 복구 성능을 확인하였습니다.

---

## 9. 장중 급락 매수 로직 개선 및 급락 기준 변경 [2026년 6월 23일 화요일 / 6월 24일 수요일]

### 9.1. 장중 급락 매수 지연 실행 및 시장가 전환
- **문제점**: 급락 감시 중 변동성 장세에서 예기치 않게 동일 종목을 단시간 내 중복 매수하는 문제가 발견되었습니다.
- **수정**: 급락 감지 시 즉시 매수하지 않고, 펜딩 플래그(`intraday_drop_pending`) 설정 및 기준가(`last_execution_price = current_price`)를 DB에 즉시 업데이트한 후 `threading.Timer`를 통해 10초 뒤 시장가(`price_type="01"`)로 0.5T 분량을 매수하는 지연 매수 로직을 구현했습니다.
- **자가 치유형 펜딩 플래그**: 프로세스 비정상 종료 등으로 인해 플래그가 펜딩 상태로 고정되는 것을 방지하기 위해 60초 초과 시 자동으로 해제되도록 안전 장치를 적용했습니다.
- **급락 기준 변경 (3% -> 5%)**: 한국 시장 및 미국 시장 모두 급락 대응 기준 비율을 기존 3%에서 **5%**(`intraday_drop_threshold=0.05`)로 상향 조정하고 동적 구성이 가능하도록 `CAConfig`에 필드를 추가했습니다.

### 9.2. 시장별 시장가 주문 처리 보완:
  - **한국 시장 (`KisKrBroker`)**: 시장가 주문(`01`) 시 주문 가격을 `0`으로 변환하여 제출하도록 보완했습니다.
  - **미국 시장 (`KisUsBroker`)**: 시장가 주문(`01`) 시 주문 가격을 `0.0`으로 변환하여 `OVRS_ORD_UNPR`을 `"0.00"`으로 전달하도록 보완했습니다.
  - **토스증권 브로커 (`TossBroker`)**: 토스증권 API는 시장가 주문(`orderType="MARKET"`) 시 `price` 파라미터를 페이로드에서 자동으로 누락시키는 기능이 브로커 자체에 구현되어 있으므로 별도 수정 없이 동작함을 확인했습니다.


---

## 10. KIS CAVR 개선사항 마이그레이션 및 Toss API 트래픽/데이터 안정성 보완 [2026년 6월 27일 토요일]

### 10.1. 정기 동기화 요약 알림 및 반환 규격 통일
- **동기화 건수 반환**: [database.py](file:///d:/Python_D/tossinvest/core/database.py) 내 `sync_trade_history_db` 함수가 신규 체결 건수(`new_count`)와 업데이트 건수(`update_count`)를 튜플 `(new_count, update_count)`로 반환하도록 구조를 개선했습니다.
- **텔레그램 요약 알림**: [scheduler.py](file:///d:/Python_D/tossinvest/core/scheduler.py) 내 매시 45분 장중 정기 동기화(`job_hourly_market_sync`) 성공 시, 신규 복구/업데이트 건수가 1건 이상일 때 요약 건수 및 세부 종목 정보를 담아 텔레그램 메시지로 자동 발송하도록 보완했습니다.

### 10.2. 일일 보고서 실제 계좌 예수금 노출
- **예수금 조회 연동**: [email_service.py](file:///d:/Python_D/tossinvest/core/email_service.py) 내 `generate_daily_report_html` 일일 운용 보고서 생성 시, `TossBroker`를 통해 장 마감 시점의 실제 출금 가능한 계좌 예수금(`get_cash_pool()`)을 실시간 조회하여 이메일 본문 상단에 시각적 블록(`🏦 실제 계좌 예수금`)으로 표시하도록 개선했습니다.

### 10.3. 전략 할당 예수금(pool) 격리 및 강제 덮어쓰기 방지
- **VR 엔진 pool 보호**: [cavr.py](file:///d:/Python_D/tossinvest/core/cavr.py) 내 `ValueRebalancingEngine`의 `run_cycle` 프로세스 중 실전 투자 시(`self.config.use_db=True`) 개별 전략의 가상 할당 자금(`self.state.pool`)을 실제 계좌 예수금으로 강제 덮어쓰던 로직을 방지하여 격리된 예산 한도가 보존되도록 했습니다 (백테스트 시에만 덮어씀).
- **CA V4.0 유동 매수금 공식 보정**: `CostAveragingEngine` 내 V4.0 유동 매수금 계산 시 분자 인자를 고정된 `self.state.pool` 대신, 보유 주식 투입액을 차감한 실제 가용 잔액 `s_pool = self.state.pool - (shares * avg_price)`으로 대체하여 잔고 소진 비율에 따라 매일 동적으로 정상 갱신되도록 수식을 보완했습니다.

### 10.4. 미국 정규장 시간 기준 하루 2회 환율 갱신 및 기준점 보존
- **크론잡 이원화**: [scheduler.py](file:///d:/Python_D/tossinvest/core/scheduler.py) 내 환율 갱신 작업을 뉴욕 시간대(`ny_tz`) 기준 **07:30 ET(장전 2시간)** 및 **16:05 ET(장후 마감 직후)** 두 개의 CronJob으로 분리하여 서머타임 변동에 자동 대응하도록 개편했습니다.
- **변동폭 수동 계산 및 기준 환율 저장**: 불안정한 외부 환율 API 연동 대신 토스 공식 환율 API(`GET /api/v1/exchange-rate`)를 사용하되, 갱신 시 DB config의 `USDKRW_BASE_RATE`와 비교하여 변동폭(`diff`) 및 등락률(`pct`)을 직접 수동 계산하여 `USDKRW_DIFF`, `USDKRW_PCT`로 저장합니다. 16:05 ET(장 마감 후) 업데이트 완료 시에만 오늘 환율을 새로운 `USDKRW_BASE_RATE`로 저장하여 전일 정규장 마감 시점의 환율이 항상 정확한 전일대비 기준점으로 보존되도록 구현했습니다.

### 10.5. 대시보드 실시간 가용 자금 표기 및 API 캐싱
- **가용 예수금 실시간 계산**: [dashboard.py](file:///d:/Python_D/tossinvest/dashboard.py) 내 `💰 전략 할당 예수금`에 기존 고정 설정금 대신 실시간 가용 예수금인 `s_pool = pool - (shares * avg_price)`을 표기하도록 수정했습니다.
- **예수금 캐싱 및 트래픽 완화**: 대시보드의 5초 주기 프래그먼트 자동 갱신(`display_holdings_metrics_live`) 시 매번 `broker.get_cash_pool()` API를 호출하는 대신, 수동 계좌조회 버튼 클릭 시 `st.session_state`에 캐싱된 계좌 예수금을 매핑하여 403 API 트래픽 제한 위험을 해소했습니다. 환율 메트릭 위젯에는 수동 계산된 전일대비 변동폭 및 등락률(delta)을 연동 표기했습니다.

### 10.6. 웹소켓 및 Docker 환경 안정성 보완
- **AttributeError 방지**: [ws_client.py](file:///d:/Python_D/tossinvest/core/ws_client.py) 내 `_ping_loop` 시 websockets 라이브러리 버전별 `ClientConnection` 개체의 open 속성 유무에 대응해 `hasattr` 체크를 통한 안전한 연결 감지 방식을 적용했습니다.
- **CA 전략 예수금 격리**: 웹소켓 실시간 체결 처리 시 `ca_state.pool`을 강제로 가산/감산하던 코드를 제거하여 설정 예수금 한도가 훼손되지 않도록 영구 보존했습니다.
- **Dockerfile 빌드 안정화**: [dockerfile](file:///d:/Python_D/tossinvest/dockerfile) 내에서 PyPI 패키지 다운로드 중 네트워크 타임아웃 및 해시 불일치 에러를 방지하기 위해 `pip`를 최신으로 선제 업그레이드하고 `--default-timeout=1000`, `--retries 10` 옵션을 추가했습니다.

### 10.7. ToosInvest 브랜드 테마 및 UI 커스터마이징
- **짙은 푸른색 테마 적용**: [.streamlit/config.toml](file:///d:/Python_D/tossinvest/.streamlit/config.toml) 테마 설정 파일을 신규 생성하여 대시보드 Key Color(primaryColor)를 짙은 푸른색 `#004B87`로 설정하고 위젯 배경 등을 연한 푸른빛 계열로 단장하여 KIS CAVR 프로젝트와 시각적으로 명확히 분리했습니다. [dashboard.py](file:///d:/Python_D/tossinvest/dashboard.py) 상단에도 커스텀 CSS 마크다운을 주입하여 스타일을 한층 보강했습니다.
- **로고 및 제목 브랜딩**: 사용자가 보관 중이던 `toss_logo.png` 로고 이미지 파일을 대시보드 메인 화면 타이틀, 사이드바 최상단, 로그인 화면에 삽입하였으며, 대시보드 메인 제목 및 사이드바 타이틀에 "ToosInvest" 식별 문구를 추가하여 브랜딩을 강화했습니다.
