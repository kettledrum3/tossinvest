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

