from __future__ import annotations
# VERSAO_ATUALIZADA: PROFESSIONAL_AUTOCOMPLETE_API_V7_2026_09_26

import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests


@dataclass(frozen=True, slots=True)
class ApiCardSuggestion:
    """Sugestão de carta vinda da Pokémon TCG API."""

    name: str
    card_number: str
    collection_number: str
    set_name: str
    set_id: str
    series: str
    rarity: str
    display_text: str
    source: str = "API"


@dataclass(slots=True)
class ApiSearchResult:
    suggestions: list[ApiCardSuggestion]
    from_cache: bool = False
    total_count: int = 0


class PokemonTcgApiError(RuntimeError):
    """Erro controlado para falhas na Pokémon TCG API."""


class PokemonTcgSearchCache:
    """Cache SQLite para autocomplete da Pokémon TCG API.

    Mantém resultados de consultas já feitas para reduzir latência, evitar
    chamadas repetidas enquanto o usuário digita e proteger o app de falhas
    temporárias da API.
    """

    def __init__(self, db_path: str | Path = "data/pokemon_api_suggestions.sqlite3", ttl_hours: int = 24) -> None:
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
                CREATE TABLE IF NOT EXISTS api_suggestion_cache (
                    cache_key TEXT PRIMARY KEY,
                    query_text TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    total_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_api_suggestion_query ON api_suggestion_cache(query_text)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_api_suggestion_updated ON api_suggestion_cache(updated_at)")

    def get(self, query_text: str, allow_expired: bool = False) -> ApiSearchResult | None:
        cache_key = self.make_key(query_text)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload_json, total_count, updated_at FROM api_suggestion_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()

        if not row:
            return None

        updated_at = self._parse_timestamp(row["updated_at"])
        if datetime.now() - updated_at > self.ttl and not allow_expired:
            return None

        try:
            raw_items = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            return None

        suggestions = [self._dict_to_suggestion(item) for item in raw_items if isinstance(item, dict)]
        return ApiSearchResult(suggestions=suggestions, from_cache=True, total_count=int(row["total_count"] or 0))

    def set(self, query_text: str, result: ApiSearchResult) -> None:
        cache_key = self.make_key(query_text)
        payload_json = json.dumps([self._suggestion_to_dict(item) for item in result.suggestions], ensure_ascii=False)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO api_suggestion_cache (
                    cache_key, query_text, payload_json, total_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(cache_key) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    total_count = excluded.total_count,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (cache_key, query_text, payload_json, int(result.total_count)),
            )

    @staticmethod
    def make_key(query_text: str) -> str:
        normalized = normalize_user_query(query_text).lower()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(str(value), fmt)
            except ValueError:
                continue
        return datetime.now()

    @staticmethod
    def _suggestion_to_dict(item: ApiCardSuggestion) -> dict[str, Any]:
        return {
            "name": item.name,
            "card_number": item.card_number,
            "collection_number": item.collection_number,
            "set_name": item.set_name,
            "set_id": item.set_id,
            "series": item.series,
            "rarity": item.rarity,
            "display_text": item.display_text,
            "source": item.source,
        }

    @staticmethod
    def _dict_to_suggestion(item: dict[str, Any]) -> ApiCardSuggestion:
        return ApiCardSuggestion(
            name=str(item.get("name", "")),
            card_number=str(item.get("card_number", "")),
            collection_number=str(item.get("collection_number", "")),
            set_name=str(item.get("set_name", "")),
            set_id=str(item.get("set_id", "")),
            series=str(item.get("series", "")),
            rarity=str(item.get("rarity", "")),
            display_text=str(item.get("display_text", "")),
            source=str(item.get("source", "API")),
        )


class PokemonTcgApiClient:
    """Cliente mínimo para buscar cartas na Pokémon TCG API v2.

    A versão V7 remove wildcard no campo name, porque a API pode responder
    erro 500 para consultas parciais como name:bulba*. Em vez disso, a busca
    usa tokens sanitizados e filtros locais. Isso deixa o autocomplete estável.
    """

    BASE_URL = "https://api.pokemontcg.io/v2"

    def __init__(self, timeout: int = 12, cache: PokemonTcgSearchCache | None = None) -> None:
        self.timeout = timeout
        self.cache = cache or PokemonTcgSearchCache()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "pokemon-sales-autocomplete/1.0"})
        api_key = os.getenv("POKEMON_TCG_API_KEY", "").strip()
        if api_key:
            self.session.headers.update({"X-Api-Key": api_key})

    def search_suggestions(self, query_text: str, limit: int = 12) -> ApiSearchResult:
        cleaned = normalize_user_query(query_text)
        if len(cleaned) < 2:
            return ApiSearchResult(suggestions=[], total_count=0)

        cached = self.cache.get(cleaned)
        if cached is not None:
            return cached

        search_plan = build_search_plan(cleaned, limit=limit)
        if search_plan.get("skip_request"):
            return ApiSearchResult(suggestions=[], total_count=0)

        params = {
            "q": search_plan["query"],
            "page": 1,
            "pageSize": search_plan["page_size"],
            "orderBy": "name,set.releaseDate",
        }
        url = f"{self.BASE_URL}/cards?{urlencode(params)}"

        try:
            payload = self._get_json_with_retry(url)
        except PokemonTcgApiError:
            stale = self.cache.get(cleaned, allow_expired=True)
            if stale is not None:
                return stale
            raise

        cards = payload.get("data", []) or []
        cards = apply_client_filters(cards, search_plan)
        suggestions = [card_to_suggestion(card) for card in cards[:limit]]
        result = ApiSearchResult(
            suggestions=suggestions,
            from_cache=False,
            total_count=int(payload.get("totalCount", len(cards)) or 0),
        )
        self.cache.set(cleaned, result)
        return result

    def _get_json_with_retry(self, url: str) -> dict[str, Any]:
        last_error: Exception | None = None

        for attempt in range(3):
            try:
                response = self.session.get(url, timeout=self.timeout)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Payload não é um objeto JSON.")
                return payload
            except requests.Timeout as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.35 * (attempt + 1))
                    continue
                raise PokemonTcgApiError("Tempo limite excedido ao consultar a Pokémon TCG API.") from exc
            except requests.HTTPError as exc:
                last_error = exc
                status_code = exc.response.status_code if exc.response is not None else None
                if status_code in {500, 502, 503, 504} and attempt < 2:
                    time.sleep(0.45 * (attempt + 1))
                    continue
                if status_code == 429:
                    raise PokemonTcgApiError("429: limite temporário da Pokémon TCG API. Aguarde alguns segundos.") from exc
                if status_code and status_code >= 500:
                    raise PokemonTcgApiError("A Pokémon TCG API respondeu instabilidade temporária. Use o cache/histórico e tente novamente.") from exc
                raise PokemonTcgApiError(f"Falha HTTP na Pokémon TCG API: {exc}") from exc
            except requests.RequestException as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.35 * (attempt + 1))
                    continue
                raise PokemonTcgApiError(f"Falha ao consultar a Pokémon TCG API: {exc}") from exc
            except ValueError as exc:
                raise PokemonTcgApiError("Resposta inválida recebida da Pokémon TCG API.") from exc

        raise PokemonTcgApiError(f"Falha ao consultar a Pokémon TCG API: {last_error}")


def build_search_plan(query_text: str, limit: int = 12) -> dict[str, Any]:
    """Monta uma busca tolerante para nome, número e total da coleção.

    Aceita:
    - bulbasaur (143/142)
    - bulbasaur 143/142
    - 143/142
    - bulbasaur
    - 143

    Importante: não usa wildcard (*). A API pode retornar erro 500 em
    consultas parciais com wildcard, então a filtragem fina acontece no cliente.
    """

    cleaned = normalize_user_query(query_text)
    name, card_number, collection_number, is_partial_code = parse_query_parts(cleaned)

    query_parts: list[str] = []
    page_size = max(24, min(250, limit * 6))

    if name:
        query_parts.append(f"name:{format_name_query(name)}")

    if card_number:
        query_parts.append(f"number:{escape_query_token(card_number)}")
        page_size = max(page_size, 120)

    if not query_parts:
        safe_term = sanitize_name_fragment(cleaned)
        if not safe_term or len(safe_term) < 2:
            return {
                "query": "",
                "name": "",
                "card_number": "",
                "collection_number": "",
                "page_size": page_size,
                "skip_request": True,
                "is_partial_code": is_partial_code,
            }
        query_parts.append(f"name:{format_name_query(safe_term)}")

    return {
        "query": " ".join(query_parts),
        "name": name,
        "card_number": card_number,
        "collection_number": collection_number,
        "page_size": page_size,
        "skip_request": False,
        "is_partial_code": is_partial_code,
    }


def normalize_user_query(query_text: str) -> str:
    return " ".join(str(query_text or "").strip().split())


def parse_query_parts(query_text: str) -> tuple[str, str, str, bool]:
    cleaned = query_text.strip()

    # Código completo: 143/142, bulbasaur (143/142), bulbasaur 143/142.
    code_match = re.search(r"(?P<number>[A-Za-z0-9\-]+)\s*/\s*(?P<collection>[A-Za-z0-9\-]+)", cleaned)
    if code_match:
        card_number = code_match.group("number").strip()
        collection_number = code_match.group("collection").strip()
        name = cleaned[: code_match.start()].strip()
        name = sanitize_name_fragment(name)
        return name, card_number, collection_number, False

    # Código parcial durante digitação: 143/ ou bulbasaur (143/.
    partial_match = re.search(r"(?P<number>[A-Za-z0-9\-]+)\s*/\s*$", cleaned)
    if partial_match:
        card_number = partial_match.group("number").strip()
        name = cleaned[: partial_match.start()].strip()
        name = sanitize_name_fragment(name)
        return name, card_number, "", True

    # Número isolado: 143.
    if re.fullmatch(r"[A-Za-z0-9\-]+", cleaned) and any(char.isdigit() for char in cleaned):
        return "", cleaned, "", False

    return sanitize_name_fragment(cleaned), "", "", False


def sanitize_name_fragment(value: str) -> str:
    cleaned = str(value or "").strip()
    cleaned = cleaned.replace("(", " ").replace(")", " ")
    cleaned = cleaned.replace("/", " ")
    cleaned = cleaned.replace('"', " ")
    cleaned = cleaned.replace("*", " ")
    cleaned = " ".join(cleaned.split())
    return cleaned


def apply_client_filters(cards: list[dict[str, Any]], search_plan: dict[str, Any]) -> list[dict[str, Any]]:
    name = str(search_plan.get("name") or "").strip().lower()
    collection_number = str(search_plan.get("collection_number") or "").strip().lower()

    filtered: list[dict[str, Any]] = []
    for card in cards:
        card_name = str(card.get("name") or "").strip().lower()
        card_set = card.get("set", {}) or {}
        printed_total = str(card_set.get("printedTotal") or card_set.get("total") or "").strip().lower()

        if collection_number and printed_total != collection_number:
            continue

        if name:
            name_tokens = [token for token in name.split() if token]
            if name_tokens and not all(token in card_name for token in name_tokens):
                continue

        filtered.append(card)

    return filtered


def card_to_suggestion(card: dict[str, Any]) -> ApiCardSuggestion:
    card_set = card.get("set", {}) or {}
    name = str(card.get("name") or "")
    card_number = str(card.get("number") or "")
    collection_number = str(card_set.get("printedTotal") or card_set.get("total") or "")
    set_name = str(card_set.get("name") or "")
    set_id = str(card_set.get("id") or "")
    series = str(card_set.get("series") or "")
    rarity = str(card.get("rarity") or "")

    display_text = f"{name} ({card_number}/{collection_number})"
    return ApiCardSuggestion(
        name=name,
        card_number=card_number,
        collection_number=collection_number,
        set_name=set_name,
        set_id=set_id,
        series=series,
        rarity=rarity,
        display_text=display_text,
    )


def format_name_query(value: str) -> str:
    cleaned = sanitize_name_fragment(value)
    if not cleaned:
        return '""'
    if " " in cleaned:
        return f'"{cleaned}"'
    # Sem wildcard. Wildcard em name pode causar erro 500 na API.
    return escape_query_token(cleaned)


def escape_query_token(value: str) -> str:
    cleaned = str(value or "").strip().replace('"', "")
    cleaned = cleaned.replace("/", "")
    cleaned = cleaned.replace("*", "")
    if " " in cleaned:
        return f'"{cleaned}"'
    return cleaned
