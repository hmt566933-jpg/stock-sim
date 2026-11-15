"""模擬倉 / 撮合層：處理成交價、手續費、證交稅、漲跌停、資金不足。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class Costs:
    fee_rate: float = 0.001425
    fee_discount: float = 1.0
    min_fee: float = 20
    sell_tax_stock: float = 0.003
    sell_tax_etf: float = 0.001
    price_limit: float | None = 0.10
    etfs: tuple = ()
    etf_prefix: str | None = None  # 台股 ETF 代號以 "00" 開頭

    def fee(self, value: float) -> float:
        if self.fee_rate <= 0 or value <= 0:
            return 0.0
        return max(self.min_fee, value * self.fee_rate * self.fee_discount)

    def tax(self, symbol: str, value: float) -> float:
        is_etf = symbol in self.etfs or bool(self.etf_prefix and symbol.startswith(self.etf_prefix))
        rate = self.sell_tax_etf if is_etf else self.sell_tax_stock
        return value * rate


@dataclass
class Account:
    cash: float
    costs: Costs
    shares: dict = field(default_factory=dict)      # 代號 -> 股數
    cost_basis: dict = field(default_factory=dict)  # 代號 -> 總成本（含買進手續費）
    trades: list = field(default_factory=list)

    def blocked(self, side: str, price: float, prev_close: float | None) -> bool:
        """台股漲停買不到、跌停賣不掉（以開盤價是否貼近漲跌停近似）。"""
        lim = self.costs.price_limit
        if not lim or prev_close is None or not prev_close > 0:
            return False
        if side == "buy" and price >= prev_close * (1 + lim - 0.005):
            return True
        if side == "sell" and price <= prev_close * (1 - lim + 0.005):
            return True
        return False

    def sell(self, date, symbol: str, qty: int, price: float, prev_close=None) -> None:
        held = self.shares.get(symbol, 0)
        qty = min(qty, held)
        if qty <= 0 or self.blocked("sell", price, prev_close):
            return
        value = qty * price
        fee, tax = self.costs.fee(value), self.costs.tax(symbol, value)
        proceeds = value - fee - tax
        avg = self.cost_basis[symbol] / held
        pnl = proceeds - avg * qty
        self.cash += proceeds
        self.shares[symbol] = held - qty
        self.cost_basis[symbol] -= avg * qty
        if self.shares[symbol] == 0:
            del self.shares[symbol], self.cost_basis[symbol]
        self.trades.append(dict(date=str(date.date()), symbol=symbol, side="賣", qty=qty,
                                price=round(price, 4), fee=round(fee + tax, 2), pnl=round(pnl, 2)))

    def buy(self, date, symbol: str, qty: int, price: float, prev_close=None) -> None:
        if qty <= 0 or self.blocked("buy", price, prev_close):
            return
        # 資金不足就買少一點
        afford = math.floor(self.cash / (price * (1 + self.costs.fee_rate * self.costs.fee_discount)))
        qty = min(qty, afford)
        while qty > 0 and qty * price + self.costs.fee(qty * price) > self.cash:
            qty -= 1
        if qty <= 0:
            return
        value = qty * price
        fee = self.costs.fee(value)
        self.cash -= value + fee
        self.shares[symbol] = self.shares.get(symbol, 0) + qty
        self.cost_basis[symbol] = self.cost_basis.get(symbol, 0.0) + value + fee
        self.trades.append(dict(date=str(date.date()), symbol=symbol, side="買", qty=qty,
                                price=round(price, 4), fee=round(fee, 2), pnl=None))

    def market_value(self, prices: dict) -> float:
        return self.cash + sum(q * prices[s] for s, q in self.shares.items())
