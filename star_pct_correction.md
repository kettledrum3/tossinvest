# 무한매수법 Star% 공식 수정 및 타 프로젝트 이식 가이드

이 문서는 무한매수법 전략의 핵심인 `star%` 계산 공식의 결함을 보완하고 수정된 공식을 **한국투자증권(KIS) CAVR 프로젝트** 및 **토스증권(tossinvest) 프로젝트**에 안전하게 이식하기 위한 가이드입니다.

---

## 1. 개요 및 문제 정의

### 기존 공식의 문제점
기존 공식은 다음과 같이 목표수익률에서 회차 감쇄분을 단순 뺄셈하는 방식이었습니다.
$$Star\% = \text{목표수익률}(\%) - \left(\frac{T}{2} \times \frac{T_{default}}{a_{default}}\right) \%$$

* **결함:** 목표수익률에 따라 $T$가 분할 수의 절반(40분할 기준 20회차)에 도달하기 훨씬 전에 `star%`가 $0\%$ 이하로 내려가거나(목표수익률이 낮을 때), 혹은 20회차에 도달해도 여전히 양수로 남아 있는 문제(목표수익률이 높을 때)가 발생했습니다.
* **의도:** `star%`는 지정한 회차의 50%에 도달할 때 정확히 `0%`가 되고, 이를 기점으로 후반전에 안정적으로 진입하는 안전장치 역할을 해야 합니다.

---

## 2. 수정된 공식 (Corrected Formula)

목표수익률과 상관없이 $T = 20$ (40분할의 절반)이 되는 지점에서 정확히 $Star\% = 0\%$가 되도록 **비율 기반 곱셈 공식**으로 변경하였습니다.

$$Star\% = \text{목표수익률}(\%) \times \left(1 - \frac{T}{20} \times \frac{T_{default}}{a_{default}}\right) \%$$

* 이 공식에 따르면, $T_{default} = a_{default}$일 때 $T = 20$을 대입하면 괄호 안의 식은 $1 - 1 = 0$이 되어 정확히 $Star\% = 0\%$가 도출됩니다.

---

## 3. 타 프로젝트 이식 가이드 (Porting Guide)

타 프로젝트(한국투자증권 KIS, 토스증권 등)에 이 공식을 이식할 때는 다음 **두 가지 핵심 포인트**를 반영해야 합니다.

### [Point 1] 백엔드 엔진 공식 수정 및 부동소수점 오차 보정
파이썬의 부동소수점 곱셈 연산 오차로 인해 `0.10 * 0.75`가 `0.07500000000000001`과 같이 미세하게 어긋나서 올림(`math.ceil`) 시 불필요하게 `0.01%`가 튀어 오르는 현상이 발생합니다. 이를 보정하기 위해 올림 처리 전에 `round(..., 9)`를 반드시 거쳐야 합니다.

#### 수정 대상 함수 예시 (`_calculate_star_percent`)
각 프로젝트의 전략 엔진(예: `cavr.py` 혹은 무한매수법 관련 모듈) 내 계산 함수를 아래와 같이 변경합니다.

```python
import math

def _calculate_star_percent(self, turn: float) -> float:
    """별값(%) 계산: 목표수익률 * (1 - T/20 * (T_default/a_default)) %"""
    t = max(0, turn)
    
    # 1. 공식 수정 적용
    raw_star = self.config.target_profit_pct * (1.0 - (t / 20.0) * (self.config.T_default / self.config.a_default))
    
    # 2. [필수] 부동소수점 오차 보정 (올림 처리 전 미세 오차 제거)
    raw_star = round(raw_star, 9)
    
    # 3. 소수점 넷째 자리 올림 (무한매수법 공통 규칙)
    return math.ceil(raw_star * 10000) / 10000.0
```

---

### [Point 2] 프론트엔드/대시보드 UI 연산 동기화
사용자 화면(대시보드, 웹 UI, 텔레그램 프리뷰 등)에 매수/매도 조건표를 계산해 보여주는 파일(예: `dashboard.py` 등)이 있다면 백엔드 엔진과 계산 로직을 동일하게 맞춰주어야 불일치가 발생하지 않습니다.

#### 수정 대상 코드 예시 (`dashboard.py` 등)
```python
# 기존: term = (current_t / 2.0) * (40 / db_a_default) -> star_pct = db_target_profit - (term / 100.0)
# 변경 후:
star_pct = db_target_profit * (1.0 - (current_t / 20.0) * (40.0 / db_a_default))
star_pct = round(star_pct, 9)
star_pct = math.ceil(star_pct * 10000) / 10000.0
```

---

## 4. 이식 후 검증용 단위 테스트 코드

이식한 코드가 정상적으로 수학적 기획에 맞게 돌아가는지 확인하기 위한 파이썬 테스트 케이스 예시입니다.

```python
import math

def run_star_pct_test(target_profit_pct, T_default, a_default, t):
    # 이식한 함수 로직
    raw_star = target_profit_pct * (1.0 - (t / 20.0) * (T_default / a_default))
    raw_star = round(raw_star, 9)
    return math.ceil(raw_star * 10000) / 10000.0

# 10% 목표수익률 기준 검증
target = 0.10
T_def = 40
a_def = 40

# T=0 일 때 (10.00%)
assert math.isclose(run_star_pct_test(target, T_def, a_def, 0.0), 0.10)
# T=5 일 때 (7.50%)
assert math.isclose(run_star_pct_test(target, T_def, a_def, 5.0), 0.075)
# T=10 일 때 (5.00%)
assert math.isclose(run_star_pct_test(target, T_def, a_def, 10.0), 0.05)
# T=20 일 때 (0.00%) - 정확히 0% 지점
assert math.isclose(run_star_pct_test(target, T_def, a_def, 20.0), 0.0)

print("KIS/Toss 이식용 검증 테스트 통과!")
```
