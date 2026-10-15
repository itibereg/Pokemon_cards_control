from __future__ import annotations
# VERSAO_ATUALIZADA: CUSTOMER_PHONE_FIXED_V89_2026_09_28

import json
import sqlite3
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4
from datetime import datetime
from app.card_parser import ParsedCard
from app.models import SaleItem, calculate_totals


SALE_STATUS_OPTIONS = ["Aberta", "Liberado", "Finalizada", "Cancelada"]


@dataclass(frozen=True, slots=True)
class SavedSaleSummary:
    id: str
    title: str
    customer_name: str
    customer_phone: str
    status: str
    item_count: int
    unit_count: int
    gross_total: float
    net_total: float
    notes: str
    created_at: str
    updated_at: str
    completed_at: str
    completed_location: str
    payment_status: str
    paid_at: str


@dataclass(frozen=True, slots=True)
class SavedSale:
    id: str
    title: str
    customer_name: str
    customer_phone: str
    status: str
    notes: str
    items: list[SaleItem]
    created_at: str
    updated_at: str
    completed_at: str
    completed_location: str
    payment_status: str
    paid_at: str


class SalesHistoryStore:
    """Histórico local profissional para vendas/orçamentos em SQLite.

    Cada registro é uma venda independente. A venda pode ficar aberta, ser
    finalizada ou cancelada, e pode ser reaberta para edição depois.
    """

    def __init__(self, db_path: str | Path = "data/sales_history.sqlite3") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sales (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    customer_name TEXT NOT NULL DEFAULT '',
                    customer_phone TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'Aberta',
                    notes TEXT NOT NULL DEFAULT '',
                    item_count INTEGER NOT NULL DEFAULT 0,
                    unit_count INTEGER NOT NULL DEFAULT 0,
                    gross_total REAL NOT NULL DEFAULT 0,
                    net_total REAL NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TEXT NOT NULL DEFAULT '',
                    completed_location TEXT NOT NULL DEFAULT '',
                    payment_status TEXT NOT NULL DEFAULT 'Pendente',
                    paid_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._ensure_columns(conn)
            # Corrige finalizações antigas sem os dois comprovantes operacionais.
            conn.execute("""UPDATE sales SET status = 'Liberado'
                WHERE status = 'Finalizada'
                AND (completed_at = '' OR payment_status != 'Pgto recebido')""")
            self._sync_separated_status(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_updated_at ON sales(updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_status ON sales(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_customer ON sales(customer_name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_title ON sales(title)")

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection) -> None:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(sales)").fetchall()}
        columns = {
            "customer_name": "TEXT NOT NULL DEFAULT ''",
            "customer_phone": "TEXT NOT NULL DEFAULT ''",
            "status": "TEXT NOT NULL DEFAULT 'Aberta'",
            "notes": "TEXT NOT NULL DEFAULT ''",
            "item_count": "INTEGER NOT NULL DEFAULT 0",
            "unit_count": "INTEGER NOT NULL DEFAULT 0",
            "gross_total": "REAL NOT NULL DEFAULT 0",
            "net_total": "REAL NOT NULL DEFAULT 0",
            "payload_json": "TEXT NOT NULL DEFAULT '[]'",
            "created_at": "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
            "updated_at": "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
            "completed_at": "TEXT NOT NULL DEFAULT ''",
            "completed_location": "TEXT NOT NULL DEFAULT ''",
            "payment_status": "TEXT NOT NULL DEFAULT 'Pendente'",
            "paid_at": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE sales ADD COLUMN {column} {definition}")


    def generate_sale_code(self) -> str:
        """Gera um código sequencial diário para a próxima venda.

        Formato: VEN-AAAAMMDD-NNN. Ex.: VEN-20260927-001.
        O contador considera as vendas já salvas naquele dia.
        """
        from datetime import datetime

        prefix = f"VEN-{datetime.now().strftime('%Y%m%d')}-"
        pattern = re.compile(rf"^{re.escape(prefix)}(\d+)$")
        highest = 0

        with self.connect() as conn:
            rows = conn.execute(
                "SELECT title FROM sales WHERE title LIKE ?",
                (f"{prefix}%",),
            ).fetchall()

        for row in rows:
            match = pattern.match(str(row["title"] or ""))
            if match:
                highest = max(highest, int(match.group(1)))

        return f"{prefix}{highest + 1:03d}"

    @staticmethod
    def _items_are_separated(items) -> bool:
        return bool(items) and all(
            str(getattr(item, "separation_status", "") or "").strip().casefold() == "separado"
            for item in items
        )

    @staticmethod
    def _payload_is_separated(payload_json: str) -> bool:
        try:
            items = json.loads(payload_json or "[]")
        except (ValueError, TypeError):
            return False
        return isinstance(items, list) and bool(items) and all(
            isinstance(item, dict)
            and str(item.get("separation_status") or "").strip().casefold() == "separado"
            for item in items
        )

    def _sync_separated_status(self, conn, sale_id: str | None = None) -> None:
        query = "SELECT id, payload_json FROM sales WHERE status IN ('Aberta', 'Liberado')"
        params = ()
        if sale_id is not None:
            query += " AND id = ?"
            params = (sale_id,)
        for row in conn.execute(query, params).fetchall():
            status = "Liberado" if self._payload_is_separated(row["payload_json"]) else "Aberta"
            conn.execute("UPDATE sales SET status = ? WHERE id = ? AND status != ?",
                         (status, row["id"], status))

    def save_sale(
        self,
        *,
        items: list[SaleItem],
        title: str,
        sale_id: str | None = None,
        customer_name: str = "",
        customer_phone: str = "",
        status: str = "Aberta",
        notes: str = "",
    ) -> str:
        clean_title = clean_text(title) or "Venda sem título"
        clean_customer = clean_text(customer_name)
        clean_phone = clean_text(customer_phone)
        clean_status = status if status in SALE_STATUS_OPTIONS else "Aberta"
        if clean_status in {"Aberta", "Liberado"}:
            clean_status = "Liberado" if self._items_are_separated(items) else "Aberta"
        clean_notes = str(notes or "").strip()
        sale_id = sale_id or uuid4().hex
        totals = calculate_totals(items)
        payload_json = json.dumps([self._item_to_dict(item) for item in items], ensure_ascii=False)

        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT completed_at, payment_status FROM sales WHERE id = ?", (sale_id,),
            ).fetchone()
            ready = bool(existing and existing["completed_at"] and existing["payment_status"] == "Pgto recebido")
            if clean_status == "Finalizada" and not ready:
                raise ValueError("A venda só pode ser Finalizada após concluir a entrega e receber o pagamento.")
            if ready and clean_status != "Cancelada":
                clean_status = "Finalizada"
            conn.execute(
                """
                INSERT INTO sales (
                    id, title, customer_name, customer_phone, status, notes, item_count, unit_count,
                    gross_total, net_total, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    customer_name = excluded.customer_name,
                    customer_phone = excluded.customer_phone,
                    status = excluded.status,
                    notes = excluded.notes,
                    item_count = excluded.item_count,
                    unit_count = excluded.unit_count,
                    gross_total = excluded.gross_total,
                    net_total = excluded.net_total,
                    payload_json = excluded.payload_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    sale_id,
                    clean_title,
                    clean_customer,
                    clean_phone,
                    clean_status,
                    clean_notes,
                    totals.item_count,
                    totals.unit_count,
                    totals.gross_total,
                    totals.net_total,
                    payload_json,
                ),
            )
        return sale_id

    def set_payment_received(self, sale_id: str, received: bool = True) -> str:
        """Atualiza o status de pagamento da venda e registra data/hora quando recebido."""
        if received:
            paid_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            payment_status = "Pgto recebido"
        else:
            paid_at = ""
            payment_status = "Pendente"

        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE sales
                SET payment_status = ?,
                    paid_at = ?,
                    status = CASE
                        WHEN status = 'Cancelada' THEN status
                        WHEN ? = 'Pgto recebido' AND completed_at != '' THEN 'Finalizada'
                        WHEN status = 'Finalizada' THEN 'Liberado'
                        ELSE status END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (payment_status, paid_at, payment_status, sale_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Venda não encontrada.")
            self._sync_separated_status(conn, sale_id)

        return paid_at

    def complete_sale(self, sale_id: str, location: str) -> str:
        """Registra a entrega; finaliza apenas se o pagamento já foi recebido."""
        clean_location = str(location or "").strip()
        valid_locations = {"Domínio", "Multiverso", "Countergames"}
        if clean_location not in valid_locations:
            raise ValueError("Selecione um local válido para concluir a entrega.")

        completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE sales
                SET status = CASE WHEN payment_status = 'Pgto recebido' THEN 'Finalizada' ELSE 'Liberado' END,
                    completed_at = ?,
                    completed_location = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'Liberado' AND completed_at = ''
                """,
                (completed_at, clean_location, sale_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("A venda precisa estar Separada e com entrega pendente para concluir a entrega.")
        return completed_at

    def cancel_delivery(self, sale_id: str) -> None:
        """Desfaz a confirmação da entrega, preservando o pagamento."""
        with self.connect() as conn:
            cursor = conn.execute(
                """UPDATE sales SET
                    status = CASE WHEN status = 'Cancelada' THEN status ELSE 'Liberado' END,
                    completed_at = '', completed_location = '', updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND completed_at != ''""", (sale_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError("Esta venda não tem entrega concluída para cancelar.")
            self._sync_separated_status(conn, sale_id)

    def reopen_delivery(self, sale_id: str) -> None:
        """Retorna Finalizada para Separada mantendo o pagamento recebido."""
        with self.connect() as conn:
            cursor = conn.execute(
                """UPDATE sales SET status = 'Liberado', completed_at = '',
                    completed_location = '', updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND status = 'Finalizada'""", (sale_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError("Apenas uma venda Finalizada pode retornar para Separada.")
            self._sync_separated_status(conn, sale_id)

    def list_sales(self, search: str = "", status: str = "Todos", limit: int = 300) -> list[SavedSaleSummary]:
        clauses: list[str] = []
        params: list[Any] = []

        clean_search = clean_text(search)
        if clean_search:
            like = f"%{clean_search.lower()}%"
            clauses.append(
                "(LOWER(title) LIKE ? OR LOWER(customer_name) LIKE ? OR LOWER(customer_phone) LIKE ? "
                "OR LOWER(notes) LIKE ? OR LOWER(completed_location) LIKE ? OR LOWER(payment_status) LIKE ?)"
            )
            params.extend([like, like, like, like, like, like])

        clean_status = clean_text(status)
        if clean_status and clean_status != "Todos":
            clauses.append("status = ?")
            params.append(clean_status)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))

        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, title, customer_name, customer_phone, status, notes, item_count, unit_count,
                       gross_total, net_total, created_at, updated_at,
                       completed_at, completed_location, payment_status, paid_at
                FROM sales
                {where_sql}
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()

        return [self._row_to_summary(row) for row in rows]

    def count_sales_by_status(self, search: str = "") -> dict[str, int]:
        """Conta vendas por status, respeitando o texto de busca atual."""
        clauses: list[str] = []
        params: list[Any] = []

        clean_search = clean_text(search)
        if clean_search:
            like = f"%{clean_search.lower()}%"
            clauses.append(
                "(LOWER(title) LIKE ? OR LOWER(customer_name) LIKE ? OR LOWER(customer_phone) LIKE ? "
                "OR LOWER(notes) LIKE ?)"
            )
            params.extend([like, like, like, like])

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        counts = {status: 0 for status in SALE_STATUS_OPTIONS}
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT status, COUNT(*) AS total
                FROM sales
                {where_sql}
                GROUP BY status
                """,
                params,
            ).fetchall()

        for row in rows:
            status = str(row["status"] or "").strip()
            if status in counts:
                counts[status] = int(row["total"] or 0)

        counts["Todos"] = sum(counts.values())
        return counts

    def get_sale(self, sale_id: str) -> SavedSale | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM sales WHERE id = ? LIMIT 1", (sale_id,)).fetchone()
        if not row:
            return None

        try:
            raw_items = json.loads(row["payload_json"] or "[]")
        except json.JSONDecodeError:
            raw_items = []

        items = [self._dict_to_item(item) for item in raw_items if isinstance(item, dict)]
        return SavedSale(
            id=str(row["id"]),
            title=str(row["title"] or "Venda sem título"),
            customer_name=str(row["customer_name"] or ""),
            customer_phone=str(row["customer_phone"] or ""),
            status=str(row["status"] or "Aberta"),
            notes=str(row["notes"] or ""),
            items=items,
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
            completed_at=str(row["completed_at"] or ""),
            completed_location=str(row["completed_location"] or ""),
            payment_status=str(row["payment_status"] or "Pendente"),
            paid_at=str(row["paid_at"] or ""),
        )

    def delete_sale(self, sale_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM sales WHERE id = ?", (sale_id,))

    def duplicate_sale(self, sale_id: str) -> str | None:
        sale = self.get_sale(sale_id)
        if sale is None:
            return None
        return self.save_sale(
            title=self.generate_sale_code(),
            customer_name=sale.customer_name,
            customer_phone=sale.customer_phone,
            status="Aberta",
            notes=sale.notes,
            items=sale.items,
        )

    @staticmethod
    def _row_to_summary(row: sqlite3.Row) -> SavedSaleSummary:
        return SavedSaleSummary(
            id=str(row["id"]),
            title=str(row["title"] or "Venda sem título"),
            customer_name=str(row["customer_name"] or ""),
            customer_phone=str(row["customer_phone"] or ""),
            status=str(row["status"] or "Aberta"),
            item_count=int(row["item_count"] or 0),
            unit_count=int(row["unit_count"] or 0),
            gross_total=float(row["gross_total"] or 0),
            net_total=float(row["net_total"] or 0),
            notes=str(row["notes"] or ""),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
            completed_at=str(row["completed_at"] or ""),
            completed_location=str(row["completed_location"] or ""),
            payment_status=str(row["payment_status"] or "Pendente"),
            paid_at=str(row["paid_at"] or ""),
        )

    @staticmethod
    def _item_to_dict(item: SaleItem) -> dict[str, Any]:
        return {
            "id": item.id,
            "quantity": int(item.quantity),
            "unit_price": float(item.unit_price),
            "discount_percent": float(getattr(item, "discount_percent", 0) or 0),
            "extras": str(getattr(item, "extras", "") or ""),
            "language": str(getattr(item, "language", "PT") or "PT"),
            "separation_status": str(getattr(item, "separation_status", "") or ""),
            "stock_item_id": getattr(item, "stock_item_id", None),
            "card": {
                "name": item.card.name,
                "card_number": item.card.card_number,
                "collection_number": item.card.collection_number,
                "raw_text": item.card.raw_text,
            },
        }

    @staticmethod
    def _dict_to_item(data: dict[str, Any]) -> SaleItem:
        card_data = data.get("card") or {}
        card = ParsedCard(
            name=str(card_data.get("name", "")),
            card_number=str(card_data.get("card_number", "")),
            collection_number=str(card_data.get("collection_number", "")),
            raw_text=str(card_data.get("raw_text", "")),
        )
        return SaleItem(
            card=card,
            quantity=max(1, int(data.get("quantity") or 1)),
            unit_price=float(data.get("unit_price") or 0),
            discount_percent=float(data.get("discount_percent") or 0),
            extras=str(data.get("extras") or ""),
            language=str(data.get("language") or "PT"),
            separation_status=str(data.get("separation_status") or ""),
            stock_item_id=(int(data["stock_item_id"]) if data.get("stock_item_id") not in (None, "") else None),
            id=str(data.get("id") or uuid4().hex),
        )


def clean_text(value: str | None) -> str:
    return " ".join(str(value or "").strip().split())
