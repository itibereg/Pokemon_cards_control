from __future__ import annotations
# VERSAO_ATUALIZADA: STOCK_LINK_V51_2026_09_27

from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from app.card_parser import ParsedCard


ITEM_STATUS_PENDING = ""
ITEM_STATUS_SEPARATED = "Separado"
ITEM_STATUS_NOT_FOUND = "Não encontrado"
ITEM_STATUS_OPTIONS = [ITEM_STATUS_PENDING, ITEM_STATUS_SEPARATED, ITEM_STATUS_NOT_FOUND]


@dataclass(slots=True)
class SaleItem:
    card: ParsedCard
    quantity: int
    unit_price: float
    discount_percent: float = 0.0
    extras: str = ""
    language: str = "PT"
    separation_status: str = ""
    stock_item_id: int | None = None
    id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=datetime.now)

    @property
    def gross_total(self) -> float:
        return self.quantity * self.unit_price

    @property
    def discount_value(self) -> float:
        return self.gross_total * (self.discount_percent / 100)

    @property
    def net_total(self) -> float:
        return self.gross_total - self.discount_value


@dataclass(slots=True)
class SaleTotals:
    item_count: int
    unit_count: int
    gross_total: float
    discount_total: float
    net_total: float


def calculate_totals(items: list[SaleItem]) -> SaleTotals:
    gross_total = sum(item.gross_total for item in items)
    discount_total = sum(item.discount_value for item in items)
    net_total = sum(item.net_total for item in items)
    unit_count = sum(item.quantity for item in items)

    return SaleTotals(
        item_count=len(items),
        unit_count=unit_count,
        gross_total=gross_total,
        discount_total=discount_total,
        net_total=net_total,
    )


def parse_money_brl(value: str) -> float:
    cleaned = str(value).strip().replace("R$", "").replace(" ", "")

    if not cleaned:
        return 0.0

    # "1.234,56" -> "1234.56"
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")

    return float(cleaned)


def format_brl(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def item_is_trade(item: SaleItem) -> bool:
    return item.unit_price < 0


def display_item_extras(item: SaleItem) -> str:
    extras = str(getattr(item, "extras", "") or "").strip()
    if not item_is_trade(item):
        return extras
    return "Troca" + (f" · {extras}" if extras else "")


def trade_summary(items: list[SaleItem]) -> tuple[float, float, float]:
    billable = [item for item in items
                if str(getattr(item, "separation_status", "") or "").strip().casefold()
                not in {"não encontrado", "nao encontrado"}]
    cards = sum(item.net_total for item in billable if not item_is_trade(item))
    trades = -sum(item.net_total for item in billable if item_is_trade(item))
    return cards, trades, cards - trades
