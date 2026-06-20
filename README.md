# 토스증권 무매 리밸 (tossinvestCAVR)

토스증권 Open API를 활용하여 한국 및 미국 주식 시장에서 **무한매수법(Cost Averaging)**과 **밸류 리밸런싱(Value Rebalancing)** 전략을 백그라운드에서 안전하고 편리하게 자동 매매 및 모니터링하는 시스템입니다.

---

## 🚀 주요 특징

*   **토스증권 Open API 연동**:
    *   OAuth2 액세스 토큰 자동 발급 및 데이터베이스 캐싱 관리.
    *   종합계좌번호 기반 `accountSeq` 헤더 자동 조회 및 실시간 매핑.
*   **하이브리드 동기화 스케줄**:
    *   웹소켓 미지원 제약을 극복하기 위해 **매 15분 단위**로 REST API 정기 동기화(`kr_hourly_sync`, `us_hourly_sync`)를 가동하여 체결 및 주문 상태를 로컬 DB에 완벽히 백업합니다.
    *   종료된 주문 리스트 조회가 제한되는 토스 API의 한계를 우회하여, DB 내 성공 주문번호를 순회하며 개별 단건 상세 조회를 통해 이력을 동기화합니다.
*   **정밀한 주문 매핑**:
    *   미국 시장은 `LIMIT` + `CLS` (LOC 마감 지정가) 주문을 직접 전송합니다.
    *   한국 시장은 LOC 주문이 미지원되므로, 장 종료 직전(15:10)에 **LOC 모사 주문**(`job_kr_loc_simulation`)을 통해 지정가 분할 매수로 대응합니다.
    *   MOC 주문은 일반 시장가 주문(`MARKET` + `DAY`)으로 변환하여 실시간 체결합니다.
*   **직관적인 대시보드**:
    *   Streamlit 기반 웹 GUI를 제공하여 실시간 잔고 조회, 신규 전략 설정(CA V2.2/V4.0, VR 실력공식), 전략 활성화/종료 및 백테스트 시뮬레이션을 원클릭으로 구동합니다.

---

## 🛠️ 설치 및 설정 방법

### 1. 가상환경 및 패키지 설치 (WSL / Linux 환경 권장)
```bash
# WSL 터미널 접속 후 프로젝트 폴더로 이동
cd /mnt/d/Python_D/tossinvest

# 가상환경 활성화
source .venv/bin/activate

# 의존성 패키지 설치
pip install -r requirements.txt
```

### 2. TOSS API 환경설정 (`env/` 폴더 내 설정)
`tossinvest/env` 폴더 내의 환경설정 파일에 토스증권에서 발급받은 Open API Key와 계좌 정보를 셋업합니다. (한국/미국 동일 키 공유 사용)
*   **공통 환경 변수**: `env/.env`
*   **한국 시장 설정**: `env/.env.kr` (수수료, 세금, 목표수익률 등)
*   **미국 시장 설정**: `env/.env.us` (수수료, 세금, 목표수익률 등)

#### 설정 필드 예시 (`env/.env.us`)
```env
TOSS_BASE_URL="https://openapi.tossinvest.com"
TOSS_CLIENT_ID="발급받은_CLIENT_ID"
TOSS_CLIENT_SECRET="발급받은_CLIENT_SECRET"
TOSS_ACCOUNT_NO="종합계좌번호11자리"
MARKET_TYPE=US
fee_rate=0.001
tax_sell=0.0000206
T_default=40
a_default=40
T_profit=0.15
```

*   **종목 리스트**: `env/ETF_list_kr.txt` 및 `env/ETF_list_us.txt` 파일에 자동매매 대상 종목을 `TICKER "종목이름"` 형태로 작성합니다.

---

## 🏃 실행 방법

### 1. 자동매매 백그라운드 스케줄러 가동
장 시작 전 휴장일 판단, 주기적 환율 갱신, 15분 단위 체결 동기화 및 자동 주문 제출을 수행하는 백그라운드 데몬 프로세스를 구동합니다.
```bash
# 가상환경 활성화 상태에서 실행
python core/scheduler.py
```

### 2. 웹 대시보드 (Streamlit) 가동
모니터링 및 수동 제어를 위한 GUI 웹 페이지를 가동합니다. (포트 8504 사용)
```bash
streamlit run dashboard.py --server.port 8504
```
브라우저에서 `http://localhost:8504` 로 접속하여 로그인 및 운용을 관리합니다.
