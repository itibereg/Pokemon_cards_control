from __future__ import annotations
# VERSAO_ATUALIZADA: TTK_DEFAULT_FEATURES_V6_2026_06_09

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests


class PokemonTCGApiError(RuntimeError):
    """Erro controlado para falhas na comunicação com a Pokémon TCG API."""


@dataclass(slots=True)
class SearchResult:
    cards: list[dict[str, Any]]
    total_count: int
    page: int
    page_size: int
    from_cache: bool = False
    cache_age_seconds: int | None = None
    stale_cache: bool = False


class PokemonSearchCache:
    """Cache local SQLite para respostas de pesquisa da Pokémon TCG API.

    O cache evita consultas repetidas à API para a mesma combinação de:
    termo, página, tamanho da página, ordenação e filtros.
    """

    def __init__(
        self,
        db_path: str | Path = "data/pokemon_api_cache.sqlite3",
        ttl_hours: int = 24,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl = timedelta(hours=max(1, int(ttl_hours)))
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS search_cache (
                    cache_key TEXT PRIMARY KEY,
                    term TEXT NOT NULL,
                    page INTEGER NOT NULL,
                    page_size INTEGER NOT NULL,
                    order_by TEXT NOT NULL,
                    rarity TEXT,
                    card_type TEXT,
                    supertype TEXT,
                    total_count INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_search_cache_term ON search_cache(term)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_search_cache_updated_at ON search_cache(updated_at)")

    def get(
        self,
        *,
        term: str,
        page: int,
        page_size: int,
        order_by: str,
        rarity: str,
        card_type: str,
        supertype: str,
        allow_expired: bool = False,
    ) -> SearchResult | None:
        cache_key = self.make_key(
            term=term,
            page=page,
            page_size=page_size,
            order_by=order_by,
            rarity=rarity,
            card_type=card_type,
            supertype=supertype,
        )

        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM search_cache WHERE cache_key = ? LIMIT 1",
                (cache_key,),
            ).fetchone()

        if not row:
            return None

        updated_at = self._parse_timestamp(row["updated_at"])
        age_seconds = int((datetime.now() - updated_at).total_seconds())
        is_expired = datetime.now() - updated_at > self.ttl

        if is_expired and not allow_expired:
            return None

        try:
            cards = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            return None

        if not isinstance(cards, list):
            return None

        return SearchResult(
            cards=cards,
            total_count=int(row["total_count"] or 0),
            page=int(row["page"] or page),
            page_size=int(row["page_size"] or page_size),
            from_cache=True,
            cache_age_seconds=max(0, age_seconds),
            stale_cache=is_expired,
        )

    def set(
        self,
        *,
        term: str,
        page: int,
        page_size: int,
        order_by: str,
        rarity: str,
        card_type: str,
        supertype: str,
        cards: list[dict[str, Any]],
        total_count: int,
    ) -> None:
        cache_key = self.make_key(
            term=term,
            page=page,
            page_size=page_size,
            order_by=order_by,
            rarity=rarity,
            card_type=card_type,
            supertype=supertype,
        )

        payload_json = json.dumps(cards, ensure_ascii=False, separators=(",", ":"))

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO search_cache (
                    cache_key, term, page, page_size, order_by, rarity, card_type,
                    supertype, total_count, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(cache_key) DO UPDATE SET
                    total_count = excluded.total_count,
                    payload_json = excluded.payload_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    cache_key,
                    term,
                    int(page),
                    int(page_size),
                    order_by,
                    rarity,
                    card_type,
                    supertype,
                    int(total_count),
                    payload_json,
                ),
            )

    def clear_expired(self) -> int:
        cutoff = datetime.now() - self.ttl
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM search_cache WHERE updated_at < ?",
                (cutoff.strftime("%Y-%m-%d %H:%M:%S"),),
            )
            return int(cursor.rowcount or 0)

    @staticmethod
    def make_key(
        *,
        term: str,
        page: int,
        page_size: int,
        order_by: str,
        rarity: str,
        card_type: str,
        supertype: str,
    ) -> str:
        normalized = {
            "term": term.strip().lower(),
            "page": int(page),
            "page_size": int(page_size),
            "order_by": order_by.strip(),
            "rarity": rarity.strip(),
            "card_type": card_type.strip(),
            "supertype": supertype.strip(),
        }
        raw = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(str(value), fmt)
            except ValueError:
                continue
        return datetime.now()


class PokemonTCGClient:
    """Cliente para a Pokémon TCG API v2 com cache local SQLite."""

    BASE_URL = "https://api.pokemontcg.io/v2"

    def __init__(
        self,
        timeout: int = 20,
        cache: PokemonSearchCache | None = None,
        cache_enabled: bool = True,
    ) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.cache_enabled = cache_enabled
        self.cache = cache or PokemonSearchCache()

        api_key = os.getenv("POKEMON_TCG_API_KEY", "").strip()
        if api_key:
            self.session.headers.update({"X-Api-Key": api_key})

    def search_cards(
        self,
        term: str,
        page: int = 1,
        page_size: int = 36,
        order_by: str = "name,set.releaseDate",
        rarity: str = "",
        card_type: str = "",
        supertype: str = "",
        force_refresh: bool = False,
    ) -> SearchResult:
        cleaned_term = term.strip()
        if not cleaned_term:
            raise ValueError("Informe um termo de busca.")

        if self.cache_enabled and not force_refresh:
            cached = self.cache.get(
                term=cleaned_term,
                page=page,
                page_size=page_size,
                order_by=order_by,
                rarity=rarity,
                card_type=card_type,
                supertype=supertype,
            )
            if cached is not None:
                return cached

        query = build_card_query(
            term=cleaned_term,
            rarity=rarity,
            card_type=card_type,
            supertype=supertype,
        )

        params = {
            "q": query,
            "page": page,
            "pageSize": page_size,
            "orderBy": order_by,
        }

        url = f"{self.BASE_URL}/cards?{urlencode(params)}"

        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except requests.Timeout as exc:
            stale = self._get_stale_cache(
                term=cleaned_term,
                page=page,
                page_size=page_size,
                order_by=order_by,
                rarity=rarity,
                card_type=card_type,
                supertype=supertype,
            )
            if stale:
                return stale
            raise PokemonTCGApiError("Tempo limite excedido ao consultar a API.") from exc
        except requests.RequestException as exc:
            stale = self._get_stale_cache(
                term=cleaned_term,
                page=page,
                page_size=page_size,
                order_by=order_by,
                rarity=rarity,
                card_type=card_type,
                supertype=supertype,
            )
            if stale:
                return stale
            raise PokemonTCGApiError(f"Falha ao consultar a API: {exc}") from exc
        except ValueError as exc:
            stale = self._get_stale_cache(
                term=cleaned_term,
                page=page,
                page_size=page_size,
                order_by=order_by,
                rarity=rarity,
                card_type=card_type,
                supertype=supertype,
            )
            if stale:
                return stale
            raise PokemonTCGApiError("Resposta inválida recebida da API.") from exc

        cards = payload.get("data", []) or []
        total_count = int(payload.get("totalCount", 0) or 0)
        result_page = int(payload.get("page", page) or page)
        result_page_size = int(payload.get("pageSize", page_size) or page_size)

        if self.cache_enabled:
            self.cache.set(
                term=cleaned_term,
                page=page,
                page_size=page_size,
                order_by=order_by,
                rarity=rarity,
                card_type=card_type,
                supertype=supertype,
                cards=cards,
                total_count=total_count,
            )

        return SearchResult(
            cards=cards,
            total_count=total_count,
            page=result_page,
            page_size=result_page_size,
            from_cache=False,
            cache_age_seconds=None,
            stale_cache=False,
        )

    def _get_stale_cache(
        self,
        *,
        term: str,
        page: int,
        page_size: int,
        order_by: str,
        rarity: str,
        card_type: str,
        supertype: str,
    ) -> SearchResult | None:
        if not self.cache_enabled:
            return None

        return self.cache.get(
            term=term,
            page=page,
            page_size=page_size,
            order_by=order_by,
            rarity=rarity,
            card_type=card_type,
            supertype=supertype,
            allow_expired=True,
        )


def build_card_query(
    term: str,
    rarity: str = "",
    card_type: str = "",
    supertype: str = "",
) -> str:
    parts = [build_card_name_query(term)]

    if rarity.strip() and rarity.strip() != "Todas":
        parts.append(f'rarity:"{_clean_query_value(rarity)}"')

    if card_type.strip() and card_type.strip() != "Todos":
        parts.append(f"types:{_clean_query_value(card_type)}")

    if supertype.strip() and supertype.strip() != "Todos":
        parts.append(f"supertype:{_clean_query_value(supertype)}")

    return " ".join(parts)


def build_card_name_query(term: str) -> str:
    cleaned_term = _clean_query_value(term)

    if " " in cleaned_term:
        return f'name:"{cleaned_term}"'

    return f"name:{cleaned_term}"


def _clean_query_value(value: str) -> str:
    return value.strip().replace('"', "")
