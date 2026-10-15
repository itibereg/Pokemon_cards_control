# Pokémon Sales Simple v3

Programa simples para montar vendas/orçamentos de cartas Pokémon.

## O que esta versão faz

- Janela única de vendas.
- Adiciona carta digitando:
  - `bulbasaur (143/142)`
  - `bulbasaur 143/142`
  - `143/142`
- Mostra sugestões enquanto você digita.
- As sugestões combinam:
  - histórico local em SQLite;
  - Pokémon TCG API com todos os catálogos/coleções disponíveis na API.
- Aceita busca por nome, número ou código completo:
  - `bul`
  - `bulbasaur`
  - `143`
  - `143/142`
  - `bulbasaur 143/142`
- Cache local das sugestões da API:
  - `data/pokemon_api_suggestions.sqlite3`
- Histórico local das cartas adicionadas:
  - `data/card_catalog.sqlite3`
- Quantidade por item.
- Preço unitário em R$.
- Desconto percentual por item.
- Soma automática.
- Remove item selecionado.
- Limpa venda.
- Exporta para Excel `.xlsx`.

## Como rodar

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

## Observações sobre a API

A busca usa a Pokémon TCG API v2. Sem chave, pode haver limite de requisições. Se quiser usar uma chave, defina a variável de ambiente:

```powershell
$env:POKEMON_TCG_API_KEY="sua_chave"
python main.py
```

O programa continua permitindo adicionar cartas manualmente mesmo se a API estiver indisponível.


## V4

Autocomplete estabilizado: debounce maior, uma consulta de API por vez, cooldown para limite temporário e mensagens menos agressivas quando o usuário digita rápido.
