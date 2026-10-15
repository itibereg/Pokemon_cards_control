from __future__ import annotations
# VERSAO_ATUALIZADA: OFFICIAL_COLLECTION_NUMBER_V23_2026_09_29

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

import requests

from app.catalog import CardSuggestion


TCGDEX_BASE_URL = "https://api.tcgdex.net/v2"
DEFAULT_LANGUAGES = ("pt", "en")
USER_AGENT = "PokemonSalesSimple/1.0 (+local-card-cache)"
ProgressCallback = Callable[[str], None]


class CardCacheUpdateError(RuntimeError):
    """Erro controlado ao atualizar o cache local de cartas."""


@dataclass(frozen=True, slots=True)
class CardCacheStats:
    card_count: int
    set_count: int
    languages: str
    updated_at: str
    source: str


@dataclass(frozen=True, slots=True)
class CardCacheUpdateResult:
    card_count: int
    set_count: int
    languages: str
    updated_at: str
    source: str


class TcgDexCardCacheStore:
    """Cache local pesquisável com dados do TCGdex.

    A atualização baixa a lista de sets e cartas da API pública do TCGdex e
    grava tudo em SQLite. O autocomplete consulta este SQLite primeiro, sem
    precisar chamar a internet a cada tecla digitada.
    """

    def __init__(self, db_path: str | Path = "data/tcgdex_card_cache.sqlite3") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        install_sqlite_functions(conn)
        return conn

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tcgdex_cards (
                    cache_key TEXT PRIMARY KEY,
                    card_id TEXT NOT NULL,
                    lang TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    name_norm TEXT NOT NULL DEFAULT '',
                    local_id TEXT NOT NULL DEFAULT '',
                    local_id_norm TEXT NOT NULL DEFAULT '',
                    number_text TEXT NOT NULL DEFAULT '',
                    number_norm TEXT NOT NULL DEFAULT '',
                    set_id TEXT NOT NULL DEFAULT '',
                    set_name TEXT NOT NULL DEFAULT '',
                    set_name_norm TEXT NOT NULL DEFAULT '',
                    series_name TEXT NOT NULL DEFAULT '',
                    series_name_norm TEXT NOT NULL DEFAULT '',
                    card_count TEXT NOT NULL DEFAULT '',
                    image_url TEXT NOT NULL DEFAULT '',
                    display_text TEXT NOT NULL DEFAULT '',
                    display_norm TEXT NOT NULL DEFAULT '',
                    search_norm TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'TCGdex',
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(card_id, lang)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tcgdex_sets (
                    cache_key TEXT PRIMARY KEY,
                    set_id TEXT NOT NULL,
                    lang TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    series_name TEXT NOT NULL DEFAULT '',
                    card_count TEXT NOT NULL DEFAULT '',
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(set_id, lang)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tcgdex_cache_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tcgdex_cards_name ON tcgdex_cards(name_norm)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tcgdex_cards_number ON tcgdex_cards(number_norm)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tcgdex_cards_local_id ON tcgdex_cards(local_id_norm)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tcgdex_cards_set_id ON tcgdex_cards(set_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tcgdex_cards_search ON tcgdex_cards(search_norm)")

    def get_stats(self) -> CardCacheStats:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS card_count,
                    COUNT(DISTINCT set_id) AS set_count,
                    GROUP_CONCAT(DISTINCT lang) AS languages
                FROM tcgdex_cards
                """
            ).fetchone()
            meta_rows = conn.execute("SELECT key, value FROM tcgdex_cache_meta").fetchall()

        meta = {str(item["key"]): str(item["value"] or "") for item in meta_rows}
        return CardCacheStats(
            card_count=int(row["card_count"] or 0) if row else 0,
            set_count=int(row["set_count"] or 0) if row else 0,
            languages=str(row["languages"] or "") if row else "",
            updated_at=meta.get("updated_at", ""),
            source=meta.get("source", "TCGdex"),
        )

    def has_cards(self) -> bool:
        return self.get_stats().card_count > 0

    def update_all_cards(
        self,
        languages: Iterable[str] = DEFAULT_LANGUAGES,
        progress_callback: ProgressCallback | None = None,
    ) -> CardCacheUpdateResult:
        langs = tuple(lang.strip().lower() for lang in languages if str(lang or "").strip())
        if not langs:
            langs = DEFAULT_LANGUAGES

        self._progress(progress_callback, "iniciando atualização")
        all_set_rows: list[dict[str, Any]] = []
        all_card_rows: list[dict[str, Any]] = []
        updated_at = datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%d %H:%M:%S")

        for lang in langs:
            self._progress(progress_callback, f"baixando coleções {lang.upper()}")
            sets_payload = self._fetch_collection(f"/{lang}/sets")
            sets_by_id = self._build_sets_index(sets_payload, lang)
            all_set_rows.extend(sets_by_id.values())

            self._progress(progress_callback, f"baixando cartas {lang.upper()}")
            cards_payload = self._fetch_collection(f"/{lang}/cards")
            card_rows = [
                self._card_payload_to_row(card, lang, sets_by_id, updated_at)
                for card in cards_payload
                if isinstance(card, dict)
            ]
            all_card_rows.extend(card_rows)
            self._progress(progress_callback, f"{len(card_rows)} carta(s) {lang.upper()} processada(s)")

        if not all_card_rows:
            raise CardCacheUpdateError("Nenhuma carta foi retornada pelo TCGdex.")

        self._progress(progress_callback, "gravando cache local")
        with self.connect() as conn:
            conn.execute("DELETE FROM tcgdex_cards")
            conn.execute("DELETE FROM tcgdex_sets")
            conn.executemany(
                """
                INSERT INTO tcgdex_sets (
                    cache_key, set_id, lang, name, series_name, card_count,
                    raw_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row["cache_key"], row["set_id"], row["lang"], row["name"],
                        row["series_name"], row["card_count"], row["raw_json"], updated_at,
                    )
                    for row in all_set_rows
                ],
            )
            conn.executemany(
                """
                INSERT INTO tcgdex_cards (
                    cache_key, card_id, lang, name, name_norm, local_id, local_id_norm,
                    number_text, number_norm, set_id, set_name, set_name_norm,
                    series_name, series_name_norm, card_count, image_url, display_text,
                    display_norm, search_norm, source, raw_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row["cache_key"], row["card_id"], row["lang"], row["name"], row["name_norm"],
                        row["local_id"], row["local_id_norm"], row["number_text"], row["number_norm"],
                        row["set_id"], row["set_name"], row["set_name_norm"], row["series_name"],
                        row["series_name_norm"], row["card_count"], row["image_url"], row["display_text"],
                        row["display_norm"], row["search_norm"], row["source"], row["raw_json"], updated_at,
                    )
                    for row in all_card_rows
                ],
            )
            meta = {
                "updated_at": updated_at,
                "source": "TCGdex API",
                "languages": ",".join(langs),
            }
            conn.executemany(
                """
                INSERT INTO tcgdex_cache_meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                list(meta.items()),
            )

        stats = self.get_stats()
        self._progress(progress_callback, f"concluído: {stats.card_count} carta(s)")
        return CardCacheUpdateResult(
            card_count=stats.card_count,
            set_count=stats.set_count,
            languages=stats.languages,
            updated_at=stats.updated_at,
            source=stats.source,
        )

    def search(self, query: str, limit: int = 12, preferred_language: str = "pt") -> list[CardSuggestion]:
        cleaned = " ".join(str(query or "").strip().split())
        if len(cleaned) < 2:
            return []

        query_norm = normalize_text(cleaned)
        if not query_norm:
            return []

        like = f"%{query_norm}%"
        starts = f"{query_norm}%"
        tokens = [token for token in query_norm.split() if token]
        token_clause = ""
        params = {
            "query_norm": query_norm,
            "like": like,
            "starts": starts,
            "preferred_language": preferred_language,
            "limit": max(1, int(limit) * 4),
        }
        if len(tokens) >= 2:
            token_parts = []
            for index, token in enumerate(tokens[:6]):
                key = f"token_{index}"
                params[key] = f"%{token}%"
                token_parts.append(f"search_norm LIKE :{key}")
            token_clause = "OR (" + " AND ".join(token_parts) + ")"

        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM tcgdex_cards
                WHERE
                    number_norm LIKE :like
                    OR local_id_norm LIKE :like
                    OR name_norm LIKE :like
                    OR display_norm LIKE :like
                    OR set_name_norm LIKE :like
                    OR series_name_norm LIKE :like
                    OR search_norm LIKE :like
                    OR LOWER(card_id) LIKE :like
                    {token_clause}
                ORDER BY
                    CASE
                        WHEN number_norm = :query_norm THEN 0
                        WHEN local_id_norm = :query_norm THEN 1
                        WHEN LOWER(card_id) = :query_norm THEN 2
                        WHEN name_norm = :query_norm THEN 3
                        WHEN number_norm LIKE :starts THEN 4
                        WHEN local_id_norm LIKE :starts THEN 5
                        WHEN name_norm LIKE :starts THEN 6
                        WHEN display_norm LIKE :starts THEN 7
                        ELSE 8
                    END,
                    CASE WHEN lang = :preferred_language THEN 0 ELSE 1 END,
                    set_id ASC,
                    natural_sort_key(local_id) ASC,
                    name ASC
                LIMIT :limit
                """,
                params,
            ).fetchall()

        suggestions: list[CardSuggestion] = []
        seen_cards: set[str] = set()
        for row in rows:
            card_id = str(row["card_id"] or "")
            # Evita duplicar PT/EN para a mesma carta quando ambas casarem.
            if card_id in seen_cards:
                continue
            seen_cards.add(card_id)
            suggestions.append(self._row_to_suggestion(row))
            if len(suggestions) >= int(limit):
                break
        return suggestions

    def resolve_collection_number(self, edition_code: str = "", edition_name: str = "") -> str:
        """Obtém o denominador impresso pela identidade exata da coleção.

        Usa somente o cache local; não consulta a rede durante a digitação.
        Siglas da Liga não são IDs TCGdex. Por isso também compara o nome
        PT/EN e as siglas presentes nos metadados, sem aproximar nomes ou
        deduzir o total pela quantidade de cartas cadastradas.
        """
        code = normalize_text(edition_code)
        name = normalize_text(edition_name)
        if not code and not name:
            return ""
        stat = self.db_path.stat()
        revision = (stat.st_mtime_ns, stat.st_size)
        if getattr(self, "_set_numbers_revision", None) != revision:
            with self.connect() as conn:
                rows = conn.execute(
                    "SELECT set_id, name, card_count, raw_json FROM tcgdex_sets"
                ).fetchall()
            aliases: dict[str, set[str]] = {}
            totals: dict[str, set[str]] = {}
            for row in rows:
                set_id = str(row["set_id"] or "")
                try:
                    payload = json.loads(row["raw_json"] or "{}")
                except (TypeError, ValueError):
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                # O total oficial tem prioridade sobre o total com cartas secretas.
                count = self._extract_card_count(payload.get("cardCount")) or str(row["card_count"] or "")
                count = count.strip()
                if count.isdecimal() and int(count) > 0:
                    totals.setdefault(set_id, set()).add(str(int(count)))
                abbreviations = payload.get("abbreviations")
                extra_codes = list(abbreviations.values()) if isinstance(abbreviations, dict) else []
                for value in (set_id, row["name"], payload.get("name"),
                              payload.get("tcgOnline"), payload.get("ptcgoCode"), *extra_codes):
                    alias = normalize_text(value)
                    if alias:
                        aliases.setdefault(alias, set()).add(set_id)
            self._set_number_aliases = aliases
            self._set_number_totals = totals
            self._set_numbers_revision = revision

        by_code = self._set_number_aliases.get(code, set())
        by_name = self._set_number_aliases.get(name, set())
        candidates = by_code & by_name if by_code and by_name else by_code or by_name
        # Mesmo total em duas coleções não prova que se trata da mesma coleção.
        if len(candidates) != 1:
            return ""
        counts = self._set_number_totals.get(next(iter(candidates)), set())
        return next(iter(counts)) if len(counts) == 1 else ""

    def _fetch_collection(self, path: str) -> list[dict[str, Any]]:
        url = f"{TCGDEX_BASE_URL}{path}"
        try:
            response = self.session.get(url, timeout=35)
            response.raise_for_status()
            payload = response.json()
        except requests.Timeout as exc:
            raise CardCacheUpdateError("Tempo limite excedido ao baixar dados do TCGdex.") from exc
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", "")
            raise CardCacheUpdateError(f"TCGdex retornou erro HTTP {status}.") from exc
        except requests.RequestException as exc:
            raise CardCacheUpdateError(f"Falha de conexão com o TCGdex: {exc}") from exc
        except ValueError as exc:
            raise CardCacheUpdateError("O TCGdex retornou uma resposta que não é JSON válido.") from exc

        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("data", "cards", "sets", "results"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        raise CardCacheUpdateError(f"Formato inesperado do TCGdex em {url}.")

    def _build_sets_index(self, sets_payload: list[dict[str, Any]], lang: str) -> dict[str, dict[str, Any]]:
        sets_by_id: dict[str, dict[str, Any]] = {}
        for item in sets_payload:
            set_id = clean_string(item.get("id"))
            if not set_id:
                continue
            name = clean_string(item.get("name"))
            series_name = clean_string((item.get("serie") or {}).get("name") if isinstance(item.get("serie"), dict) else "")
            card_count = self._extract_card_count(item.get("cardCount"))
            row = {
                "cache_key": f"{lang}:{set_id}",
                "set_id": set_id,
                "lang": lang,
                "name": name,
                "series_name": series_name,
                "card_count": card_count,
                "raw_json": json.dumps(item, ensure_ascii=False, separators=(",", ":")),
            }
            sets_by_id[set_id] = row
        return sets_by_id

    def _card_payload_to_row(
        self,
        item: dict[str, Any],
        lang: str,
        sets_by_id: dict[str, dict[str, Any]],
        updated_at: str,
    ) -> dict[str, str]:
        card_id = clean_string(item.get("id"))
        local_id = clean_string(item.get("localId") or self._local_id_from_card_id(card_id))
        set_id = clean_string(item.get("set") or self._set_id_from_card_id(card_id))
        set_row = sets_by_id.get(set_id, {})
        name = clean_string(item.get("name"))
        set_name = clean_string(item.get("setName") or set_row.get("name") or set_id)
        series_name = clean_string(item.get("series") or set_row.get("series_name") or "")
        card_count = clean_string(set_row.get("card_count") or item.get("total") or "")
        image_url = clean_string(item.get("image"))
        number_text = make_number_text(local_id, card_count)
        display_text = make_display_text(name, number_text, set_name)
        search_norm = " ".join(
            part for part in [
                normalize_text(name),
                normalize_text(local_id),
                normalize_text(number_text),
                normalize_text(set_id),
                normalize_text(set_name),
                normalize_text(series_name),
                normalize_text(card_id),
                normalize_text(display_text),
            ] if part
        )
        return {
            "cache_key": f"{lang}:{card_id}",
            "card_id": card_id,
            "lang": lang,
            "name": name,
            "name_norm": normalize_text(name),
            "local_id": local_id,
            "local_id_norm": normalize_text(local_id),
            "number_text": number_text,
            "number_norm": normalize_text(number_text),
            "set_id": set_id,
            "set_name": set_name,
            "set_name_norm": normalize_text(set_name),
            "series_name": series_name,
            "series_name_norm": normalize_text(series_name),
            "card_count": card_count,
            "image_url": image_url,
            "display_text": display_text,
            "display_norm": normalize_text(display_text),
            "search_norm": search_norm,
            "source": "TCGdex",
            "raw_json": json.dumps(item, ensure_ascii=False, separators=(",", ":")),
            "updated_at": updated_at,
        }

    @staticmethod
    def _row_to_suggestion(row: sqlite3.Row) -> CardSuggestion:
        name = str(row["name"] or "")
        number_text = str(row["number_text"] or "")
        local_id = str(row["local_id"] or "")
        collection_number = str(row["card_count"] or "")
        set_name = str(row["set_name"] or "")
        lang = str(row["lang"] or "").upper()
        input_text = f"{name} {number_text}".strip() if number_text else name
        detail_parts = [part for part in [set_name, lang, "Cache TCGdex"] if part]
        detail = " · ".join(detail_parts)
        return CardSuggestion(
            name=name,
            card_number=local_id,
            collection_number=collection_number,
            display_text=f"{make_display_text(name, number_text, set_name)} · Cache TCGdex {lang}",
            input_text=input_text,
            usage_count=0,
            source="TCGdex",
            detail=detail,
        )

    @staticmethod
    def _extract_card_count(value: Any) -> str:
        if isinstance(value, dict):
            for key in ("official", "total", "printedTotal", "cards"):
                raw = value.get(key)
                if raw not in (None, ""):
                    return clean_string(raw)
        return clean_string(value)

    @staticmethod
    def _set_id_from_card_id(card_id: str) -> str:
        if "-" not in card_id:
            return ""
        return card_id.rsplit("-", 1)[0]

    @staticmethod
    def _local_id_from_card_id(card_id: str) -> str:
        if "-" not in card_id:
            return card_id
        return card_id.rsplit("-", 1)[1]

    @staticmethod
    def _progress(callback: ProgressCallback | None, message: str) -> None:
        if callback is not None:
            callback(message)


# SQLite sort helper: 2 < 10 and TG01 remains stable.
def natural_sort_key(value: object) -> str:
    text = str(value or "")
    parts = re.split(r"(\d+)", text)
    return "".join(part.zfill(8) if part.isdigit() else part.lower() for part in parts)


sqlite3.register_adapter(Path, str)
try:
    sqlite3.enable_callback_tracebacks(False)
except Exception:  # noqa: BLE001
    pass


def install_sqlite_functions(conn: sqlite3.Connection) -> None:
    conn.create_function("natural_sort_key", 1, natural_sort_key)


def clean_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return ""
    return " ".join(str(value).replace("\u00a0", " ").strip().split())


def normalize_text(value: Any) -> str:
    text = clean_string(value).casefold()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.replace("º", "").replace("ª", "")
    text = re.sub(r"[^a-z0-9/\- ]+", " ", text)
    return " ".join(text.split())


def make_number_text(local_id: str, card_count: str) -> str:
    local = clean_string(local_id)
    total = clean_string(card_count)
    if local and total:
        return f"{local}/{total}"
    return local


def make_display_text(name: str, number_text: str, set_name: str) -> str:
    clean_name = clean_string(name)
    clean_number = clean_string(number_text)
    clean_set = clean_string(set_name)
    parts = []
    if clean_name and clean_number:
        parts.append(f"{clean_name} ({clean_number})")
    elif clean_name:
        parts.append(clean_name)
    elif clean_number:
        parts.append(clean_number)
    if clean_set:
        parts.append(clean_set)
    return " · ".join(parts)
