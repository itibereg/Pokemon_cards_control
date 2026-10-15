"""Integração com a API oficial do MYP Cards.

Documentação: https://mypcards.github.io/mypcards-api/
A API publica cotações gerais em BRL, sem filtro documentado de idioma/NM.
O aplicativo solicita aceitação desse critério antes de consultar valores.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

SOURCE = 'MYP Cards · API oficial'
CRITERION = 'Menor preço geral em BRL da impressão exata. API sem filtro documentado de idioma ou condição NM.'
BASE_URL = 'https://mypcards.com/api/v1'


class QuoteUnavailable(ValueError):
    pass


class SourceBlocked(QuoteUnavailable):
    pass


def normalized(value):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD', str(value)) if not unicodedata.combining(c)).casefold().split())


def fingerprint(item):
    return tuple(str(item.get(k,'')) for k in ('name','number','collection','extras','language','quantity','price_cents','current_price_cents'))


def number_key(value):
    return '/'.join(part.lstrip('0') or '0' for part in normalized(value).replace('#','').split('/'))


@dataclass(frozen=True)
class Quote:
    cents: int
    url: str
    checked_at: str
    offer_count: int


def matching_products(cards, item):
    aliases = {normalized(n) for n in str(item.get('name','')).split(' / ')}
    number = str(item.get('number','')).strip().lstrip('#')
    collection = str(item.get('collection','')).strip()
    if '/' not in number and number.isdigit() and collection.isdigit():
        number += '/' + collection
    language = normalized(item.get('language',''))
    if language not in ('','pt','pt-br','portugues','portugues brasileiro'):
        raise QuoteUnavailable('Carta cadastrada em outro idioma; mantido o valor atual.')
    if not number or not aliases or aliases <= {'','x'}:
        raise QuoteUnavailable('Nome ou número da carta não informado.')
    if normalized(item.get('extras','')) in ('reverse holo','shattered holo','first edition','1a edicao'):
        raise QuoteUnavailable('A API não permite confirmar o preço desta variante de acabamento.')
    matches = {}
    for product in cards:
        if not isinstance(product,dict):
            continue
        if not aliases.intersection({normalized(product.get('name_pt') or ''),normalized(product.get('name_en') or '')}):
            continue
        if not product.get('name_pt'):
            continue
        if collection and not collection.isdigit() and normalized(collection) not in {
            normalized(product.get('edition_code') or ''),normalized(product.get('edition_pt') or ''),normalized(product.get('edition_en') or '')}:
            continue
        numbers = []
        code = str(product.get('card_code') or '')
        if code:
            numbers.append(re.split(r'[-_]',code)[-1])
        for label in product.get('deck_labels') or []:
            numbers.extend(re.findall(r'\(([^()]*)\)',str(label)))
        if not any(number_key(n)==number_key(number) for n in numbers):
            continue
        identity = product.get('internal_code')
        if identity is not None:
            matches[str(identity)] = product
    if not matches:
        raise QuoteUnavailable('API sem correspondência confirmada de nome, número e edição.')
    if len(matches)!=1:
        raise QuoteUnavailable('Mais de uma impressão corresponde à carta. Informe a coleção para desempatar.')
    return next(iter(matches.values()))


def parse_product_quote(product):
    stock = product.get('available_quantity')
    if type(stock) is not int or stock <= 0:
        raise QuoteUnavailable('API sem estoque confirmado para essa impressão.')
    raw = product.get('min_price')
    try:
        amount = Decimal(str(raw))
        if not amount.is_finite() or amount <= 0:
            raise ValueError()
        cents = int((amount*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
    except (ValueError,InvalidOperation,OverflowError) as exc:
        raise QuoteUnavailable('API sem preço mínimo válido em reais.') from exc
    if cents <= 0:
        raise QuoteUnavailable('Preço mínimo abaixo de um centavo; mantido o valor anterior.')
    return Quote(cents,str(product.get('link') or BASE_URL),datetime.now().isoformat(timespec='seconds'),stock)


class SameHostRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme != 'https' or urlparse(newurl).hostname not in ('mypcards.com','www.mypcards.com'):
            raise SourceBlocked('Redirecionamento externo recusado para proteger o token da API.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class MypPriceProvider:
    def __init__(self, token, timeout=15, fetch=None):
        self.token = str(token).strip()
        if not self.token or '\n' in self.token or '\r' in self.token:
            raise SourceBlocked('Configure um token autorizado do MYP Cards.')
        self.timeout = timeout
        self.fetch = fetch
        self.name_cache = {}

    def _request(self, path):
        if self.fetch is not None:
            return self.fetch(path)
        request = Request(BASE_URL+path,headers={'X-Api-Token':self.token,'Accept':'application/json','Accept-Language':'pt-BR','User-Agent':'PokemonCards/1.0'})
        try:
            with build_opener(SameHostRedirect()).open(request,timeout=self.timeout) as response:
                data = response.read(5_000_001)
                if len(data)>5_000_000:
                    raise QuoteUnavailable('Resposta da API excedeu o tamanho esperado.')
                result=json.loads(data.decode('utf-8-sig'))
        except HTTPError as exc:
            if exc.code == 404:
                return {'cards': []}
            if exc.code in (401,403):
                raise SourceBlocked('A API recusou o acesso. Confira o token e a autorização com o suporte do MYP Cards.') from exc
            if exc.code==429:
                raise SourceBlocked('Limite de consultas da API atingido. Aguarde antes de tentar novamente.') from exc
            raise QuoteUnavailable(f'A API respondeu HTTP {exc.code}.') from exc
        except (URLError,TimeoutError,OSError) as exc:
            raise QuoteUnavailable('Não foi possível conectar à API do MYP Cards.') from exc
        except (UnicodeError,ValueError) as exc:
            raise QuoteUnavailable('Resposta inválida da API do MYP Cards.') from exc
        if not isinstance(result,dict):
            raise QuoteUnavailable('Formato de resposta da API não reconhecido.')
        return result

    def get_quote(self,item):
        name = str(item.get('name','')).strip()
        if normalized(name) in ('','x') or not item.get('number'):
            raise QuoteUnavailable('Nome ou número da carta não informado.')
        cards = []
        for alias in name.split(' / '):
            if alias not in self.name_cache:
                payload=self._request('/pokemon/carta/'+quote(alias.strip(),safe=''))
                values=payload.get('cards')
                if not isinstance(values,list):
                    raise QuoteUnavailable('A API não retornou uma lista de cartas válida.')
                self.name_cache[alias]=values
            cards.extend(self.name_cache[alias])
        return parse_product_quote(matching_products(cards,item))
