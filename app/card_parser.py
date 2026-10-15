from __future__ import annotations
# VERSAO_ATUALIZADA: FREE_ADD_V9_2026_09_26

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParsedCard:
    """Carta digitada pelo usuário.

    Aceita formatos estruturados, como:
    - bulbasaur (143/142)
    - bulbasaur 143/142
    - 143/142

    Também aceita texto livre, como:
    - vulpix
    - caixa surpresa pokemon
    - qualquer descrição manual
    """

    name: str
    card_number: str
    collection_number: str
    raw_text: str

    @property
    def code(self) -> str:
        if self.card_number and self.collection_number:
            return f"{self.card_number}/{self.collection_number}"
        if self.card_number:
            return self.card_number
        return ""

    @property
    def display_name(self) -> str:
        if self.name and self.code:
            return f"{self.name} ({self.code})"
        if self.name:
            return self.name
        if self.code:
            return f"Carta {self.code}"
        return self.raw_text or "Item sem descrição"


class CardParseError(ValueError):
    """Erro de validação para texto de carta vazio."""


_PATTERNS = (
    # bulbasaur (143/142)
    re.compile(
        r"^\s*(?P<name>.*?)\s*\(\s*(?P<number>[^/\s()]+)\s*/\s*(?P<collection>[^)\s]+)\s*\)\s*$",
        re.IGNORECASE,
    ),
    # bulbasaur 143/142
    re.compile(
        r"^\s*(?P<name>.*?)\s+(?P<number>[^/\s()]+)\s*/\s*(?P<collection>\S+)\s*$",
        re.IGNORECASE,
    ),
    # 143/142
    re.compile(
        r"^\s*(?P<number>[^/\s()]+)\s*/\s*(?P<collection>\S+)\s*$",
        re.IGNORECASE,
    ),
)


def parse_card_text(text: str) -> ParsedCard:
    """Interpreta a entrada da carta.

    Regra atual: a venda não deve bloquear cadastro manual.
    Se o texto não estiver em um formato estruturado, ele vira item livre.
    """

    raw_text = " ".join(str(text or "").strip().split())
    if not raw_text:
        raise CardParseError("Digite a carta ou descrição do item.")

    for pattern in _PATTERNS:
        match = pattern.match(raw_text)
        if not match:
            continue

        groups = match.groupdict()
        name = clean_name(groups.get("name", ""))
        card_number = clean_code_part(groups.get("number", ""))
        collection_number = clean_code_part(groups.get("collection", ""))

        if card_number and collection_number:
            return ParsedCard(
                name=name,
                card_number=card_number,
                collection_number=collection_number,
                raw_text=raw_text,
            )

    # Fallback profissional: aceita livremente qualquer texto digitado.
    return ParsedCard(
        name=clean_name(raw_text),
        card_number="",
        collection_number="",
        raw_text=raw_text,
    )


def clean_name(value: str | None) -> str:
    return " ".join(str(value or "").strip().split())


def clean_code_part(value: str | None) -> str:
    return str(value or "").strip().replace("(", "").replace(")", "")
