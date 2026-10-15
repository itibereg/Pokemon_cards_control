from __future__ import annotations
# VERSAO_ATUALIZADA: HISTORY_DELETE_V24_2026_09_26

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.card_parser import ParsedCard


@dataclass(frozen=True, slots=True)
class CardSuggestion:
    """Sugestão para autocomplete, vinda do histórico local ou da API."""

    name: str
    card_number: str
    collection_number: str
    display_text: str
    input_text: str
    usage_count: int = 0
    source: str = "Local"
    detail: str = ""


class CardCatalogStore:
    """Catálogo local simples baseado nas cartas já digitadas pelo usuário.

    O autocomplete agora combina este histórico local com sugestões da Pokémon TCG API.
    """

    def __init__(self, db_path: str | Path = "data/card_catalog.sqlite3") -> None:
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
                CREATE TABLE IF NOT EXISTS card_catalog (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL DEFAULT '',
                    card_number TEXT NOT NULL,
                    collection_number TEXT NOT NULL,
                    display_text TEXT NOT NULL,
                    usage_count INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(name, card_number, collection_number)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_card_catalog_name ON card_catalog(name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_card_catalog_code ON card_catalog(card_number, collection_number)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_card_catalog_usage ON card_catalog(usage_count)")

    def save_card(self, card: ParsedCard) -> None:
        name = card.name.strip()
        card_number = card.card_number.strip()
        collection_number = card.collection_number.strip()
        display_text = card.display_name.strip()

        # V9: salva também itens livres, sem número/coleção.
        # Isso permite autocomplete futuro para entradas como "vulpix" ou descrições manuais.
        if not display_text:
            return

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO card_catalog (
                    name, card_number, collection_number, display_text,
                    usage_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(name, card_number, collection_number) DO UPDATE SET
                    display_text = excluded.display_text,
                    usage_count = card_catalog.usage_count + 1,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (name, card_number, collection_number, display_text),
            )

    def delete_card(
        self,
        name: str = "",
        card_number: str = "",
        collection_number: str = "",
        display_text: str = "",
    ) -> int:
        """Remove uma carta do histórico local usado no autocomplete.

        Retorna a quantidade de linhas removidas. O método tenta primeiro pela
        chave normalizada (nome, número, coleção). Se não encontrar, tenta pelo
        texto exibido/input, o que cobre entradas livres antigas.
        """
        name = str(name or "").strip()
        card_number = str(card_number or "").strip()
        collection_number = str(collection_number or "").strip()
        display_text = str(display_text or "").strip()

        removed = 0
        with self.connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM card_catalog
                WHERE name = ?
                  AND card_number = ?
                  AND collection_number = ?
                """,
                (name, card_number, collection_number),
            )
            removed = int(cursor.rowcount or 0)

            if removed <= 0 and display_text:
                cursor = conn.execute(
                    """
                    DELETE FROM card_catalog
                    WHERE display_text = ?
                    """,
                    (display_text,),
                )
                removed = int(cursor.rowcount or 0)

        return removed

    def search(self, query: str, limit: int = 10) -> list[CardSuggestion]:
        cleaned = " ".join(str(query or "").strip().split())
        if len(cleaned) < 2:
            return []

        normalized = cleaned.lower()
        like = f"%{normalized}%"
        starts = f"{normalized}%"

        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    name,
                    card_number,
                    collection_number,
                    display_text,
                    usage_count,
                    LOWER(display_text) AS display_lower,
                    LOWER(name) AS name_lower,
                    LOWER(card_number || '/' || collection_number) AS code_lower
                FROM card_catalog
                WHERE
                    LOWER(display_text) LIKE ?
                    OR LOWER(name) LIKE ?
                    OR LOWER(card_number || '/' || collection_number) LIKE ?
                ORDER BY
                    CASE
                        WHEN code_lower = ? THEN 0
                        WHEN code_lower LIKE ? THEN 1
                        WHEN name_lower LIKE ? THEN 2
                        WHEN display_lower LIKE ? THEN 3
                        ELSE 4
                    END,
                    usage_count DESC,
                    updated_at DESC,
                    display_text ASC
                LIMIT ?
                """,
                (like, like, like, normalized, starts, starts, starts, int(limit)),
            ).fetchall()

        return [
            CardSuggestion(
                name=row["name"] or "",
                card_number=row["card_number"] or "",
                collection_number=row["collection_number"] or "",
                display_text=f"{row['display_text']} · Histórico local",
                input_text=row["display_text"] or "",
                usage_count=int(row["usage_count"] or 0),
                source="Local",
                detail="Histórico local",
            )
            for row in rows
        ]
