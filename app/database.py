from __future__ import annotations
# VERSAO_ATUALIZADA: TTK_DEFAULT_FEATURES_V6_2026_06_09

import csv
import sqlite3
from pathlib import Path
from typing import Any

from app.models import CardInfo, InventoryItem, join_values


class InventoryDatabase:
    """Persistência local do estoque em SQLite."""

    def __init__(self, db_path: str | Path = "data/pokemon_cards.db") -> None:
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
                CREATE TABLE IF NOT EXISTS inventory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    card_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    supertype TEXT,
                    types TEXT,
                    set_name TEXT,
                    set_series TEXT,
                    number TEXT,
                    rarity TEXT,
                    image_small TEXT,
                    image_large TEXT,
                    tcgplayer_url TEXT,
                    market_price_usd REAL,
                    quantity INTEGER NOT NULL DEFAULT 1,
                    sale_price_brl REAL NOT NULL DEFAULT 0,
                    condition TEXT NOT NULL DEFAULT 'Near Mint',
                    language TEXT NOT NULL DEFAULT 'Português',
                    status TEXT NOT NULL DEFAULT 'Disponível',
                    notes TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._ensure_columns(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_inventory_card_id ON inventory(card_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_inventory_status ON inventory(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_inventory_name ON inventory(name)")

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(inventory)").fetchall()}
        columns = {
            "supertype": "TEXT",
            "types": "TEXT",
            "set_series": "TEXT",
            "image_large": "TEXT",
            "tcgplayer_url": "TEXT",
            "market_price_usd": "REAL",
            "created_at": "TEXT",
            "updated_at": "TEXT",
        }

        for column, definition in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE inventory ADD COLUMN {column} {definition}")

    def add_card_to_inventory(
        self,
        card: CardInfo,
        quantity: int = 1,
        sale_price_brl: float = 0,
        condition: str = "Near Mint",
        language: str = "Português",
        status: str = "Disponível",
        notes: str = "",
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO inventory (
                    card_id, name, supertype, types, set_name, set_series, number, rarity,
                    image_small, image_large, tcgplayer_url, market_price_usd,
                    quantity, sale_price_brl, condition, language, status, notes,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    card.id,
                    card.name,
                    card.supertype,
                    join_values(card.types),
                    card.set_name,
                    card.set_series,
                    card.number,
                    card.rarity,
                    card.image_small,
                    card.image_large,
                    card.tcgplayer_url,
                    card.market_price_usd,
                    int(quantity),
                    float(sale_price_brl),
                    condition,
                    language,
                    status,
                    notes,
                ),
            )
            return int(cursor.lastrowid)

    def list_inventory(self, search: str = "", status: str = "Todos") -> list[InventoryItem]:
        clauses: list[str] = []
        params: list[Any] = []

        if search.strip():
            clauses.append(
                """
                (name LIKE ? OR set_name LIKE ? OR set_series LIKE ? OR number LIKE ? OR rarity LIKE ? OR card_id LIKE ?)
                """
            )
            term = f"%{search.strip()}%"
            params.extend([term, term, term, term, term, term])

        if status.strip() and status.strip() != "Todos":
            clauses.append("status = ?")
            params.append(status.strip())

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        query = f"""
            SELECT *
            FROM inventory
            {where_sql}
            ORDER BY updated_at DESC, id DESC
        """

        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()

        return [self._row_to_inventory_item(row) for row in rows]

    def get_inventory_item(self, item_id: int) -> InventoryItem | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM inventory WHERE id = ?", (item_id,)).fetchone()

        if not row:
            return None

        return self._row_to_inventory_item(row)

    def update_inventory_item(
        self,
        item_id: int,
        quantity: int,
        sale_price_brl: float,
        condition: str,
        language: str,
        status: str,
        notes: str,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE inventory
                SET quantity = ?, sale_price_brl = ?, condition = ?, language = ?,
                    status = ?, notes = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    int(quantity),
                    float(sale_price_brl),
                    condition,
                    language,
                    status,
                    notes,
                    int(item_id),
                ),
            )

    def update_status(self, item_id: int, status: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE inventory
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, int(item_id)),
            )

    def delete_inventory_item(self, item_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM inventory WHERE id = ?", (int(item_id),))

    def has_card(self, card_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM inventory WHERE card_id = ? LIMIT 1",
                (card_id,),
            ).fetchone()
        return row is not None

    def get_card_ids_in_inventory(self, card_ids: list[str]) -> set[str]:
        clean_ids = [card_id for card_id in card_ids if card_id]
        if not clean_ids:
            return set()

        placeholders = ",".join("?" for _ in clean_ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT DISTINCT card_id FROM inventory WHERE card_id IN ({placeholders})",
                clean_ids,
            ).fetchall()

        return {str(row["card_id"]) for row in rows}

    def get_summary(self) -> dict[str, float | int]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS items,
                    COALESCE(SUM(quantity), 0) AS units,
                    COALESCE(SUM(quantity * sale_price_brl), 0) AS sale_value,
                    COALESCE(SUM(CASE WHEN status = 'Disponível' THEN quantity ELSE 0 END), 0) AS available_units,
                    COALESCE(SUM(CASE WHEN status = 'Vendido' THEN quantity ELSE 0 END), 0) AS sold_units,
                    COALESCE(SUM(CASE WHEN status = 'Reservado' THEN quantity ELSE 0 END), 0) AS reserved_units
                FROM inventory
                """
            ).fetchone()

        return {
            "items": int(row["items"] or 0),
            "units": int(row["units"] or 0),
            "sale_value": float(row["sale_value"] or 0),
            "available_units": int(row["available_units"] or 0),
            "sold_units": int(row["sold_units"] or 0),
            "reserved_units": int(row["reserved_units"] or 0),
        }

    def export_inventory_csv(self, output_path: str | Path) -> Path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        items = self.list_inventory()

        with path.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.writer(file, delimiter=";")
            writer.writerow(
                [
                    "ID",
                    "Card ID",
                    "Nome",
                    "Coleção",
                    "Série",
                    "Número",
                    "Raridade",
                    "Tipo",
                    "Quantidade",
                    "Preço Venda BRL",
                    "Condição",
                    "Idioma",
                    "Status",
                    "Observações",
                    "Atualizado em",
                ]
            )

            for item in items:
                writer.writerow(
                    [
                        item.id,
                        item.card_id,
                        item.name,
                        item.set_name,
                        item.set_series,
                        item.number,
                        item.rarity,
                        item.types,
                        item.quantity,
                        f"{item.sale_price_brl:.2f}",
                        item.condition,
                        item.language,
                        item.status,
                        item.notes,
                        item.updated_at,
                    ]
                )

        return path

    def _row_to_inventory_item(self, row: sqlite3.Row) -> InventoryItem:
        return InventoryItem(
            id=int(row["id"]),
            card_id=row["card_id"] or "",
            name=row["name"] or "-",
            supertype=row["supertype"] or "-",
            types=row["types"] or "-",
            set_name=row["set_name"] or "-",
            set_series=row["set_series"] or "-",
            number=row["number"] or "-",
            rarity=row["rarity"] or "-",
            image_small=row["image_small"] or "",
            image_large=row["image_large"] or "",
            tcgplayer_url=row["tcgplayer_url"],
            market_price_usd=float(row["market_price_usd"]) if row["market_price_usd"] is not None else None,
            quantity=int(row["quantity"] or 0),
            sale_price_brl=float(row["sale_price_brl"] or 0),
            condition=row["condition"] or "-",
            language=row["language"] or "-",
            status=row["status"] or "-",
            notes=row["notes"] or "",
            created_at=row["created_at"] or "-",
            updated_at=row["updated_at"] or "-",
        )
