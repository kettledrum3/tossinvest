import os
from dotenv import dotenv_values
import logging

logger = logging.getLogger(__name__)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def get_ticker_name(symbol: str, market: str) -> str:
    """종목 코드에 해당하는 이름을 반환 (ETF_list_*.txt 참조)"""
    symbol = symbol.upper()
    try:
        env_suffix = market.lower()
        file_name = f"ETF_list_{env_suffix}.txt"
        file_path = os.path.join(PROJECT_ROOT, "env", file_name)
        if os.path.exists(file_path):
            with open(file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.split('#')[0].strip()
                    if line:
                        parts = line.split(None, 1)
                        if parts[0].upper() == symbol:
                            return parts[1].strip().strip('"') if len(parts) > 1 else symbol
    except Exception:
        pass
    return symbol

def format_symbol_display(symbol: str, market: str) -> str:
    """종목 코드와 이름을 '코드 (종목명)' 형식으로 반환"""
    name = get_ticker_name(symbol, market)
    if name != symbol:
        return f"{symbol} ({name})"
    return symbol