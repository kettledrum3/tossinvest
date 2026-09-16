import unittest
from unittest.mock import MagicMock, patch
import math
from datetime import datetime

from core.cavr import CostAveragingEngine, CAConfig, CAState

class TestReverseModeFix(unittest.TestCase):
    def setUp(self):
        self.mock_broker = MagicMock()
        self.mock_broker.adjust_price_by_tick.side_effect = lambda sym, p, s: round(p, 2)
        self.mock_broker.get_account_equity.return_value = (29.0, 71.64, 2077.56)
        self.mock_broker.get_cumulative_buy_amount.return_value = 2077.56
        self.mock_broker.get_price.return_value = 70.0
        self.mock_broker.fetch_open_orders.return_value = []

        self.config = CAConfig(
            symbol="TQQQ",
            market="US",
            version="V4.0",
            a_default=20,
            initial_budget=3000.0,
            unit_buy_amount=150.0,
            target_profit_pct=0.07,
            strategy_name="T_TQQQ_1차",
            use_db=False
        )

    def test_sell_limit_only_skips_reverse_mode(self):
        """SELL_LIMIT_ONLY 세션에서는 리버스 모드 루틴이 스킵되어야 함"""
        engine = CostAveragingEngine(self.config, broker=self.mock_broker)
        engine.current_order_filter = "SELL_LIMIT_ONLY"
        engine.state.mode = "REVERSE"
        engine.state.current_turn = 25.0
        
        result = engine._run_reverse_mode_routine(70.0, datetime.now(), preview=True)
        self.assertIsNone(result, "SELL_LIMIT_ONLY 세션에서는 None을 반환해야 함")

    def test_reverse_mode_exit_no_infinite_recursion(self):
        """손실률 회복 시 리버스 모드 종료 후 재귀 호출 없이 NORMAL로 안전 복귀해야 함"""
        engine = CostAveragingEngine(self.config, broker=self.mock_broker)
        engine.state.mode = "REVERSE"
        engine.state.avg_price = 71.64
        engine.state.total_shares = 29.0
        engine.state.current_turn = 38.1 # 왜곡된 T값 가정
        
        # 현재가 70.0 -> 손실률: (70 - 71.64) / 71.64 = -2.29% > -15% (회복 기준)
        result = engine._run_reverse_mode_routine(70.0, datetime.now(), preview=True)
        
        self.assertEqual(engine.state.mode, "NORMAL", "손실률 회복 시 NORMAL로 변경되어야 함")
        self.assertTrue(engine._reverse_exited_today, "_reverse_exited_today 플래그가 True여야 함")

    def test_v4_reverse_mode_entry_prevented_when_recovered(self):
        """T > 19 이더라도 손실률이 회복 기준 이상이면 리버스 모드 재진입이 차단되어야 함"""
        engine = CostAveragingEngine(self.config, broker=self.mock_broker)
        engine.state.mode = "NORMAL"
        engine.state.avg_price = 71.64
        engine.state.total_shares = 29.0
        engine.state.current_turn = 20.5 # T > 19
        engine.state.cycle_budget = 3000.0
        engine.state.pool = 2300.0
        
        # 브로커가 T=20.5를 반환하도록 설정
        self.mock_broker.get_cumulative_buy_amount.return_value = 20.5 * 150.0
        
        # run_cycle 실행 (손실률 -2.29% > -15%)
        with patch('core.cavr.send_telegram_message'):
            engine.run_cycle(datetime.now(), preview=True, order_filter="SELL_LIMIT_ONLY")
            
        # 리버스 모드로 변경되지 않고 NORMAL 유지되어야 함
        self.assertEqual(engine.state.mode, "NORMAL", "손실률이 양호한 경우 T > 19 이어도 리버스 모드로 진입하지 않아야 함")

    def test_base_unit_buy_used_for_turn_calculation(self):
        """T 계산 시 unit_buy_amount가 아닌 cycle_budget / a_default 가 분모로 사용되어야 함"""
        engine = CostAveragingEngine(self.config, broker=self.mock_broker)
        engine.state.cycle_budget = 3000.0
        engine.config.a_default = 20
        engine.config.unit_buy_amount = 36.56 # 축소된 유동 매수금
        
        base_unit = engine._get_base_unit_buy_amount()
        self.assertEqual(base_unit, 150.0, "기준 분할 매수금은 3000 / 20 = 150이어야 함")
        
        # 누적 매수금 2077.56일 때 올바른 T는 2077.56 / 150 = 13.85
        invested = 2077.56
        correct_t = invested / base_unit
        self.assertAlmostEqual(correct_t, 13.85, places=1)

if __name__ == '__main__':
    unittest.main()
