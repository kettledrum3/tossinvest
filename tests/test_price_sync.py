from datetime import datetime
from core.cavr import CostAveragingEngine, CAConfig
from core.brokers.toss import TossBroker

def test_price_sync():
    cfg = CAConfig(symbol='TQQQ', use_db=True, market='US', strategy_name='T_TQQQ_1차')
    engine = CostAveragingEngine(cfg, broker=TossBroker(market='US'))

    orders = engine.run_cycle(preview=True, date=datetime.now())
    print("=== Orders Generated ===")
    for o in orders:
        print(f"{o['side']} {o['qty']}주 @ ${o['price']:.2f} ({o['type']}) - {o['desc']}")

    # 1. Star% 매도 주문 가격: 70.07
    sell_loc_order = [o for o in orders if o['side'] == 'SELL' and o['type'] == '34'][0]
    assert sell_loc_order['price'] == 70.07, f"Expected 70.07, got {sell_loc_order['price']}"

    # 2. 큰수 LOC 매수 주문 가격: 70.06
    buy_loc_order = [o for o in orders if o['side'] == 'BUY' and o['type'] == '34'][0]
    assert buy_loc_order['price'] == 70.06, f"Expected 70.06, got {buy_loc_order['price']}"

    # 3. 10% 지정가 매도 주문 가격: 76.80
    sell_limit_order = [o for o in orders if o['side'] == 'SELL' and o['type'] == '00'][0]
    assert sell_limit_order['price'] == 76.80, f"Expected 76.80, got {sell_limit_order['price']}"

    print("SUCCESS: All assertions passed!")

if __name__ == "__main__":
    test_price_sync()
