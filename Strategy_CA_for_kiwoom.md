# 무한매수법(CA) 키움증권(Kiwoom) 프로젝트 이식 및 전략 구현 가이드
> **문서 버전:** 1.1 (2026-09-16)  
> **대상 프로젝트:** 키움증권(Kiwoom) Open API 기반 자동매매 시스템  
> **출처:** 토스증권(tossinvest) 및 한국투자증권(KIS) CAVR 프로젝트 실전 운용 검증  

---

## 1. 개요 및 목적

본 문서는 무한매수법(Cost Averaging, CA V2.2 및 V4.0) 전략을 **키움증권(Kiwoom) 자동매매 프로젝트**에 안전하게 구현하거나 이식하기 위한 기술 레퍼런스입니다.

실전 운영 환경(토스증권 및 KIS)에서 발견된 **치명적인 버그(리버스 무한 루프, 체결 동기화 시 회차 왜곡, 부동소수점 호가 절사 오류, 자전거래 방지 오프셋 상쇄, UI 불일치)**에 대한 원인 분석과 완벽한 검증 코드를 집약하여, 키움 프로젝트에서 동일한 결함이 발생하지 않도록 방지하는 데 목적이 있습니다.

---

## 2. [핵심 포인트 1] V4.0 회차(T) 산출 기준: 기준분할매수금 분리 및 체결 동기화 통일

### 2.1. 문제 배경: 유동 매수금 역산 시 T 폭등 버그
- 무한매수법 V4.0은 잔여 가용 예수금($s\_pool$)과 남은 분할 슬롯($a - T$)을 바탕으로 1회 매수금($\text{unit\_buy\_amount}$)을 매일 동적으로 재계산합니다:
  $$\text{unit\_buy\_amount} = \frac{s\_pool}{a - T}$$
- **결함 발생 원인**:
  - 누적 매수액을 이 축소된 유동 1회 매수금으로 나누어 $T$를 역산($T = \frac{\text{누적 매수액}}{\text{unit\_buy\_amount}}$)할 경우, 잔금이 줄어들수록 1회 매수금이 작아져 **$T$가 $38 \sim 80$ 이상으로 비정상 폭등**합니다.
  - 이로 인해 원금 소진 상태가 아님에도 $T > a - 1$ 조건에 걸려 **리버스 모드(`REVERSE`)**로 잘못 진입하는 참사가 발생합니다.

### 2.2. 해결 공식: 기준분할매수금(base_unit_buy) 도입
$T$(회차)를 계산하는 분모는 잔금에 따라 매일 바뀌는 유동 매수금이 아니라, **사이클 전체 예산 기반의 고정 기준분할매수금**이어야 합니다.

$$T = \frac{\text{실제 누적 매수금 (총 매입 원가)}}{\text{base\_unit\_buy\_amount}}$$
$$\text{단, } \text{base\_unit\_buy\_amount} = \frac{\text{사이클 총 예산 (cycle\_budget)}}{\text{기본 분할 횟수 (a\_default)}}$$

### 2.3. 키움 엔진 이식 코드 예시
```python
class CostAveragingEngine:
    def _get_base_unit_buy_amount(self) -> float:
        """T(회차) 계산의 기준이 되는 기준분할매수금 (cycle_budget / a_default)"""
        a_def = self.config.a_default if self.config.a_default > 0 else 40
        budget = self.state.cycle_budget if self.state.cycle_budget > 0 else self.config.initial_budget
        if budget > 0 and a_def > 0:
            return budget / a_def
        if self.config.unit_buy_amount > 0:
            return self.config.unit_buy_amount
        return 250.0  # 기본 fallback

    def update_turn(self, cumulative_buy_amount: float):
        """회차(T) 계산: 반드시 기준분할매수금을 분모로 사용"""
        base_unit_buy = self._get_base_unit_buy_amount()
        if base_unit_buy > 0:
            # [V4.0 원칙] 인위적인 반올림/올림 없이 실수형 그대로 보존
            self.state.current_turn = cumulative_buy_amount / base_unit_buy
```

### 2.4. [치명적 주의] 체결 통보(REST/WebSocket/DB 동기화) 수신 시 T 역산 분모 통일
엔진 내부뿐만 아니라, **실시간 체결 통보 핸들러나 DB 동기화 모듈(예: `core/database.py`)에서도 동일한 기준분할매수금을 사용**해야 합니다. 엔진만 고치고 체결 DB 동기화 모듈을 누락하면, 주문 체결 시마다 DB에 $T$가 다시 38.1처럼 왜곡되어 덮어써지는 연쇄 장애가 발생합니다.

```python
# [체결 동기화/DB 핸들러 구현 표준: core/database.py]
def sync_trade_history_db(symbol, qty, price, side, state_dict):
    old_qty = float(state_dict.get('total_shares', 0))
    old_avg = float(state_dict.get('avg_price', 0))
    
    if side == "BUY":
        new_total_qty = old_qty + qty
        new_avg = ((old_qty * old_avg) + (qty * price)) / new_total_qty if new_total_qty > 0 else price
    else: # SELL
        new_total_qty = max(0, old_qty - qty)
        new_avg = old_avg if new_total_qty > 0 else 0.0
        
    state_dict['total_shares'] = new_total_qty
    state_dict['avg_price'] = new_avg
    
    # ⚠️ [절대 금지 안티패턴]: unit_buy = float(state_dict.get('unit_buy_amount', 0))
    # V4.0에서 유동 매수금이 작아진 상태일 때 이 값으로 나누면 T가 38~80 이상으로 폭등함!
    #
    # ✅ [올바른 구현]: 반드시 cycle_budget / a_default 기반의 base_unit_buy 산출
    a_def = float(state_dict.get('a_default', 40) or 40)
    c_budget = float(state_dict.get('cycle_budget', 0.0) or 0.0)
    base_unit_buy = (c_budget / a_def) if (c_budget > 0 and a_def > 0) else float(state_dict.get('unit_buy_amount', 0))
    
    if base_unit_buy > 0:
        invested = new_total_qty * new_avg
        # 체결 기록 및 상태 표시용 T는 소수점 첫째 자리 올림
        state_dict['current_turn'] = math.ceil((invested / base_unit_buy) * 10) / 10.0
```

---

## 3. [핵심 포인트 2] 리버스 모드(Reverse Mode) 무한 재귀 루프 방어 및 안전 상태 머신

### 3.1. 문제 배경: 재귀 호출(return run_cycle())로 인한 무한 핑퐁 루프
- V4.0 리버스 모드 루틴(`_run_reverse_mode_routine`)에서 손실률이 회복(-15% 이상)되어 일반 모드로 복귀할 때, `self.state.mode = "NORMAL"` 설정 후 **`return self.run_cycle()`로 재귀 호출하는 구조는 치명적인 결함**을 유발합니다.
- 만약 DB 동기화 오류 등으로 $T > a - 1$인 상태가 유지되어 있다면:
  1. 리버스 루틴에서 손실률 회복 판정 $\rightarrow$ NORMAL 복귀 $\rightarrow$ `run_cycle()` 재귀 호출
  2. 재호출된 `run_cycle()` 첫머리에서 여전히 $T > a - 1$ 조건 만족 $\rightarrow$ 즉시 REVERSE 재진입 및 텔레그램 발송
  3. 다시 리버스 루틴 진입 $\rightarrow$ 손실률 회복 상태이므로 NORMAL 복귀 $\rightarrow$ `run_cycle()` 재귀 호출...
  4. **0.1초마다 리버스 진입 ↔ 복귀가 무한히 핑퐁 반복(Infinite Recursion)**되어 텔레그램 메시지가 폭주하고 프로세스가 정지합니다.

### 3.2. 5대 안전 상태 머신 설계 원칙
1. **재귀 호출 완전 금지 (No Recursion)**: 리버스 모드 종료 시 `return self.run_cycle(...)`을 절대 호출하지 않고, `mode = "NORMAL"`로 저장 후 안전하게 당일 사이클을 종료하거나 다음 루틴으로 이어갑니다.
2. **당일 리버스 탈출 플래그 (`_reverse_exited_today`) 도입**: 하루 중 리버스 모드를 한 번 탈출했다면 당일 세션 내에서는 어떤 경우에도 리버스 모드로 다시 진입하지 못하도록 차단합니다.
3. **리버스 진입 전 손실률 방어**: $T > a - 1$을 만족하더라도, 주가 손실률이 이미 회복 기준(-15% 등)보다 양호하다면 애초에 리버스 모드로 들어가지 않고 NORMAL 모드를 유지합니다.
4. **브로커 누적매수액 기반 T 최신화 선행**: 브로커로부터 잔고 및 실제 누적 매수액을 가져와 $T$를 계산하는 코드를 리버스 모드 진입 판정보다 **앞단**에 배치하여, 왜곡된 DB 캐시 T값으로 리버스에 갇히는 현상을 방지합니다.
5. **프리마켓/지정가 매도 세션(`SELL_LIMIT_ONLY`) 스킵**: 장 시작 전 지정가 매도 전용 세션에서는 리버스 모드 루틴(MOC/LOC 매수 등)이 불필요하게 가동되지 않도록 건너뜁니다.

### 3.3. 키움 프로젝트 이식 코드 예시
```python
class CostAveragingEngine:
    def __init__(self, config, broker):
        self.config = config
        self.broker = broker
        self._reverse_exited_today = False  # 당일 리버스 복귀 후 재진입 방지 플래그

    def run_cycle(self, date, order_filter=None, preview=False):
        # 1. 브로커 시세 및 잔고 조회
        current_price = self.broker.get_price(self.config.symbol)
        shares, avg_price, _ = self.broker.get_account_equity(self.config.symbol)
        self.state.total_shares = shares
        self.state.avg_price = avg_price

        # 2. [필수] T값 최신화를 리버스 모드 판정보다 반드시 먼저 수행!
        cumulative_buy = self.broker.get_cumulative_buy_amount(self.config.symbol)
        base_unit_buy = self._get_base_unit_buy_amount()
        if base_unit_buy > 0:
            self.state.current_turn = cumulative_buy / base_unit_buy

        # 3. V4.0 리버스 모드 진입 조건 검증 (이중 방어)
        if self.config.version == "V4.0":
            if self.state.current_turn > (self.config.a_default - 1):
                recovery_threshold = -0.15 if "TQQQ" in self.config.symbol else -0.20
                loss_pct = (current_price - self.state.avg_price) / self.state.avg_price if self.state.avg_price > 0 else 0
                
                # [방어] 손실률이 기준 이하이고 당일 탈출 이력이 없을 때만 진입
                if not getattr(self, '_reverse_exited_today', False) and loss_pct <= recovery_threshold:
                    if self.state.mode != "REVERSE" and not preview:
                        send_telegram("🚨 [CA V4.0 리버스 모드 가동]")
                    self.state.mode = "REVERSE"
                else:
                    if self.state.mode == "REVERSE":
                        self.state.mode = "NORMAL"

        # 4. 리버스 모드 실행 분기
        if self.state.mode == "REVERSE":
            return self._run_reverse_mode_routine(current_price, date, preview)

        # 5. 일반 CA 매도/매수 로직 계속 진행...

    def _run_reverse_mode_routine(self, current_price: float, date, preview: bool = False):
        # 프리마켓 지정가 매도 세션은 스킵
        if self.current_order_filter == "SELL_LIMIT_ONLY":
            return None

        recovery_threshold = -0.15 if "TQQQ" in self.config.symbol else -0.20
        loss_pct = (current_price - self.state.avg_price) / self.state.avg_price if self.state.avg_price > 0 else 0

        # 회복 조건 만족 시: 재귀 호출 없이 안전하게 NORMAL 복귀 후 종료
        if loss_pct > recovery_threshold:
            self.state.mode = "NORMAL"
            self._reverse_exited_today = True
            if not preview:
                send_telegram("✨ [CA V4.0 일반 모드 복귀]")
                self._save_and_finish(current_price=current_price)
            return self.planned_orders if preview else None

        # 리버스 모드 매도/쿼터매수 실행...
```

---

## 4. [핵심 포인트 3] Star% 비율 기반 곱셈 공식 및 부동소수점 오차 보정

### 4.1. 문제 배경 및 수정된 공식
기존의 단순 뺄셈 공식($\text{Target} - \frac{T}{2}$)은 목표수익률에 따라 $T=20$에 도달하기 전에 음수가 되거나 20회차에서도 양수로 남아있는 문제가 있었습니다. 이를 해결하기 위해 **비율 기반 곱셈 공식**을 적용합니다.

$$Star\% = \text{목표수익률}(\%) \times \left(1 - \frac{T}{20} \times \frac{T_{default}}{a_{default}}\right) \%$$

### 4.2. 올림 처리 전 부동소수점 오차 보정 필수
파이썬 부동소수점 연산 오차로 인해 `math.ceil` 시 불필요하게 `0.01%`가 튀는 현상을 막기 위해 `round(raw_star, 9)`를 반드시 거쳐야 합니다.

```python
import math

def calculate_star_percent(target_profit_pct: float, current_turn: float, T_default: float = 40.0, a_default: float = 40.0) -> float:
    """별값(%) 계산: 소수점 넷째 자리 올림"""
    t = max(0.0, current_turn)
    raw_star = target_profit_pct * (1.0 - (t / 20.0) * (T_default / a_default))
    
    # 1. [필수] 부동소수점 미세 오차 제거
    raw_star = round(raw_star, 9)
    
    # 2. 소수점 넷째 자리 올림 (무한매수법 공통 원칙)
    return math.ceil(raw_star * 10000) / 10000.0
```

---

## 5. [핵심 포인트 4] 호가 보정 시 부동소수점 절사(Floor) 오차 제거

### 5.1. 문제 배경: $70.07이 $70.06으로 깎이는 현상
- 무한매수법 원칙상 **매도는 버림(floor)**, **매수는 올림(ceil)**을 적용합니다.
- 미국 주식 호가 보정 시 흔히 `math.floor(price * 100) / 100.0`을 사용합니다.
- 하지만 파이썬에서 `price = 70.07`일 때:
  ```python
  70.07 * 100  # 결과: 7006.999999999999
  math.floor(70.07 * 100)  # 결과: 7006 (7007이 아님!)
  math.floor(70.07 * 100) / 100.0  # 결과: 70.06 (부당하게 1센트 절사!)
  ```
- 이미 `$70.07`로 계산된 매도 가격조차 브로커 호가 보정 메서드를 거치면서 `$70.06`으로 왜곡되는 심각한 오류가 발생합니다. (한국 주식 `price / tick` 연산 시에도 동일 문제 발생 가능)

### 5.2. 키움 브로커 호가 보정 수정 코드
`math.floor` 또는 `math.ceil`을 취하기 전에 **`round(..., 6)`를 통해 부동소수점 잔여 오차를 반드시 흡수**해야 합니다.

```python
import math
from typing import Literal

def adjust_price_by_tick(symbol: str, price: float, order_type: Literal["BUY", "SELL"], market: str = "US") -> float:
    """키움 호가 보정: 부동소수점 오차 완벽 방어"""
    if price <= 0:
        return 0.0

    if market == "US":
        if price >= 1.0:
            # [필수] round(..., 6)으로 7006.999999999999를 7007.0으로 정규화
            scaled = round(price * 100.0, 6)
            result = math.ceil(scaled) if order_type == "BUY" else math.floor(scaled)
            return result / 100.0
        else:
            scaled = round(price * 10000.0, 6)
            result = math.ceil(scaled) if order_type == "BUY" else math.floor(scaled)
            return result / 10000.0
    else:
        # 한국 주식 호가 단위 (KOSPI/KOSDAQ)
        p = int(price)
        if p < 2000: tick = 1
        elif p < 5000: tick = 5
        elif p < 20000: tick = 10
        elif p < 50000: tick = 50
        elif p < 200000: tick = 100
        elif p < 500000: tick = 500
        else: tick = 1000

        scaled = round(price / tick, 6)
        if order_type == "BUY":
            return float(math.ceil(scaled) * tick)
        else:
            return float(math.floor(scaled) * tick)
```

---

## 6. [핵심 포인트 5] 자전거래 방지 오프셋과 올림/내림 상쇄 방지

### 6.1. 문제 배경: 매수 호가와 매도 호가의 동일화 및 역전
- Star% 가격 계산 시 매수 주문과 매도 주문이 동일한 가격에 체결되어 자전거래가 발생하는 것을 방지하기 위해 매수 주문에 오프셋($-0.01\$$ 또는 $-10$원)을 적용합니다.
- 그러나:
  - **매도 LOC 가격**: 이론가 $70.0769 \rightarrow$ 절사(floor) $\rightarrow$ **$70.07**
  - **매수 LOC 가격**: 이론가 $70.0769 - 0.01 = 70.0669 \rightarrow$ 올림(ceil) $\rightarrow$ 다시 **$70.07**
- 오프셋을 차감했음에도 불구하고 매수 올림(ceil) 처리에 의해 **매도 가격과 매수 가격이 같은 $70.07이 되어 자전거래 방지가 무력화**되거나, 앞서 언급한 매도 부동소수점 버그와 결합 시 **매수가격($70.07) > 매도가격($70.06)으로 역전**되는 기현상이 일어납니다.

### 6.2. 해결 설계: 매수 상한가 제약 (Max Buy Allowed)
매수 LOC 가격은 매도 LOC 가격(확정 호가)보다 최소 1틱(호가단위) 낮도록 명시적인 상한 제약을 걸어야 합니다.

```python
# 1. Star% 매도 기준 호가 (절사)
loc_sell_price = adjust_price_by_tick(symbol, base_price * (1.0 + star), "SELL", market)

# 2. 자전거래 방지 오프셋 정의 (US: -0.01, KR: -10원)
loc_buy_offset = -0.01 if market == "US" else -10

# 3. 매수 상한선 정의: 매도 확정 호가 대비 최소 1틱 낮음 보장
max_buy_allowed = adjust_price_by_tick(symbol, loc_sell_price + loc_buy_offset, "BUY", market)

# 4. 큰수 LOC 매수 가격 산출
price_star_raw = (base_price * (1.0 + star)) + loc_buy_offset
star_buy_price = adjust_price_by_tick(symbol, price_star_raw, "BUY", market)

# [핵심] 매수 가격이 매도 가격과 같아지거나 역전되지 않도록 상한 적용
limit_star_buy = min(star_buy_price, max_buy_allowed)

# 결과 검증:
# base_price = 71.7781, star = -0.0237 일 때
# loc_sell_price = $70.07
# limit_star_buy = $70.06  (자전거래 원천 차단 및 1센트 갭 완벽 보장!)
```

---

## 7. [핵심 포인트 6] UI 조건표 표기와 백엔드 주문 엔진의 100% 동기화

### 7.1. 문제 배경
사용자 화면(Streamlit 대시보드, 텔레그램 프리뷰, 모바일 웹 등)에서 매수/매도 조건표를 그릴 때, 브로커 호가 보정(`adjust_price_by_tick`) 없이 단순 `format(val, ".2f")`(반올림)을 적용하면:
- 이론가 $70.0769$가 화면 조건표에는 **$70.08**로 표시됨.
- 반면 실제 제출 주문은 엔진 호가 보정에 의해 **$70.07**로 제출되어 사용자가 "조건표와 주문이 다르다"고 오인하게 됩니다.

### 7.2. 키움 UI/대시보드 구현 가이드
UI 레이어에서도 독자적인 포맷팅을 하지 말고, 백엔드 엔진과 동일한 호가 보정 함수를 공유하여 표기해야 합니다.

```python
# [대시보드/UI 매도 조건표]
sell_loc_display = adjust_price_by_tick(symbol, avg_price * (1 + star_pct), "SELL", market)
# -> 화면에 정확히 $70.07 표시

# [대시보드/UI 매수 조건표]
loc_offset = -0.01 if market == "US" else -10
loc_sell_ref = adjust_price_by_tick(symbol, avg_price * (1 + star_pct), "SELL", market)
max_buy_allowed = adjust_price_by_tick(symbol, loc_sell_ref + loc_offset, "BUY", market)
buy_star_display = min(adjust_price_by_tick(symbol, (avg_price * (1 + star_pct)) + loc_offset, "BUY", market), max_buy_allowed)
# -> 화면에 정확히 $70.06 표시
```

---

## 8. 요약 체크리스트 (Kiwoom 개발자 확인용)

| 점검 항목 | 원인 및 위험 | 키움 프로젝트 적용 조치 |
| :--- | :--- | :--- |
| **V4.0 T회차 계산** | 유동 1회 매수금 역산 시 $T$ 폭등 $\rightarrow$ 리버스 무한 루프 | $\text{base\_unit\_buy} = \frac{\text{budget}}{a}$ 를 분모로 고정 사용 |
| **체결 동기화 시 T 분모** | DB/웹소켓 체결 처리 시 `unit_buy_amount` 사용으로 T 폭등 | 체결 동기화 모듈에서도 반드시 `base_unit_buy`를 분모로 일원화 |
| **리버스 무한 루프 방어** | 리버스 탈출 시 `return self.run_cycle()` 재귀 호출로 핑퐁 루프 | 재귀 호출 완전 금지, `_reverse_exited_today` 플래그 및 손실률 사전 검증 |
| **T값 최신화 선행** | 브로커 T 갱신 전 왜곡된 DB 캐시 T값으로 리버스 조기 진입 | 브로커 잔고/누적액 기반 $T$ 최신화를 리버스 판정보다 앞단에 배치 |
| **Star% 계산** | 단순 뺄셈 시 조기 음수화 / 올림 시 0.01% 튐 | 비율 곱셈 공식 + `round(..., 9)` 후 `math.ceil` 적용 |
| **호가 단위 보정** | `70.07 * 100 = 7006.9999` 부동소수점 오차로 1센트 절사 | `round(price * 100, 6)` 스케일링 후 `math.floor` 적용 |
| **자전거래 방지** | 매수 올림(ceil)으로 매도 호가와 동일화/역전 현상 | $\min(\text{매수호가}, \text{매도호가} + \text{offset})$ 상한 제약 필수 |
| **UI 조건표 정합성** | 단순 `:.2f` 반올림으로 조건표와 실제 주문가 불일치 | UI 조건표에도 동일한 `adjust_price_by_tick` 적용 |
