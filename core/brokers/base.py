from typing import Tuple, List, Literal

class Broker:
    def get_price(self, symbol: str) -> float:
        raise NotImplementedError
        
    def get_previous_close(self, symbol: str) -> float:
        raise NotImplementedError

    def get_last_5_day_avg_close(self, symbol: str) -> float:
        """직전 5거래일의 종가 평균을 반환합니다."""
        raise NotImplementedError

    def get_current_high(self, symbol: str) -> float:
        raise NotImplementedError

    def get_current_low(self, symbol: str) -> float:
        raise NotImplementedError
    
    def get_account_equity(self, symbol: str, strategy_name: str = "") -> Tuple[float, float, float]:
        """returns (shares, avg_price, eval_amt)"""
        if strategy_name:
            from core.database import get_holdings_from_db
            market = getattr(self, "market", "US")
            shares, avg_price, eval_amt = get_holdings_from_db(symbol, market, strategy_name)
            curr_price = self.get_price(symbol)
            if curr_price > 0:
                eval_amt = shares * curr_price
            return shares, avg_price, eval_amt
        return self._get_account_equity_impl(symbol)

    def _get_account_equity_impl(self, symbol: str) -> Tuple[float, float, float]:
        raise NotImplementedError

    def get_cumulative_buy_amount(self, symbol: str, strategy_name: str = "") -> float:
        """Returns the current total cost basis of the holding."""
        res = self.get_account_equity(symbol, strategy_name=strategy_name)
        if res is None:
            return 0.0
        shares, avg_price, _ = res
        return shares * avg_price
    
    def get_cash_pool(self) -> float:
        raise NotImplementedError

    def adjust_price_by_tick(self, symbol: str, price: float, order_type: Literal["BUY", "SELL"]) -> float:
        """호가 단위에 맞게 가격 보정 (기본값은 소수점 2자리 반올림)"""
        return round(price, 2)
        
    def place_order(self, symbol: str, price: float, qty: float, order_type: Literal["BUY", "SELL"], price_type: str = "00", strategy: str = "MANUAL") -> bool:
        raise NotImplementedError

    def fetch_open_orders(self, symbol: str) -> List[dict]:
        raise NotImplementedError

    def fetch_execution_history(self, symbol: str, start_date: str, end_date: str) -> List[dict]:
        raise NotImplementedError

    def get_period_profit(self, start_date: str, end_date: str) -> float:
        """지정된 기간 동안의 총 실현 손익을 반환합니다."""
        raise NotImplementedError