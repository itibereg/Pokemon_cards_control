from __future__ import annotations

import csv
import json
import os
import queue
import threading
import sqlite3
import tkinter as tk
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog
from uuid import uuid4

import ttkbootstrap as tb
from ttkbootstrap.tableview import Tableview

from app.ui.purchase_details_window import PurchaseDetailsWindow
from app.ui.purchase_price_updater import MypPriceProvider, SourceBlocked, QuoteUnavailable, fingerprint, SOURCE, CRITERION

STATUSES = ('Não informado', 'Aguardando', 'Recebida', 'Cancelada')
EXTRAS = ('', 'Holo', 'Reverse Holo', 'Full Art', 'Full Art Especial', 'Ace Spec', 'Shattered Holo')

def money_cents(value: str) -> int:
    raw = str(value).strip().replace('R$', '').replace(' ', '')
    if ',' in raw:
        raw = raw.replace('.', '').replace(',', '.')
    try:
        amount = Decimal(raw or '0')
        if not amount.is_finite() or amount < 0:
            raise ValueError('Informe um valor monetário válido, maior ou igual a zero.')
        return int((amount * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except (InvalidOperation, OverflowError) as exc:
        raise ValueError('Informe um valor monetário válido. Exemplo: 1.250,90.') from exc


def brl(cents: int) -> str:
    value = f'{Decimal(cents) / 100:,.2f}'.replace(',', '_').replace('.', ',').replace('_', '.')
    return f'R$ {value}'


def money_input(cents: int) -> str:
    return f'{cents // 100},{cents % 100:02d}'


def parse_date(value: str) -> str:
    for fmt in ('%d/%m/%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError('Informe a data no formato DD/MM/AAAA.')


def display_date(value: str) -> str:
    return date.fromisoformat(value).strftime('%d/%m/%Y') if value else 'Não informada'


def purchase_total(record: dict) -> int:
    return sum(i['quantity'] * i['price_cents'] for i in record['items']) + record['freight_cents'] - record['discount_cents']


def valuation_metrics(record: dict):
    """Valor atual total, lucro sobre o custo completo e percentual sobre esse custo."""
    prices = [item.get('current_price_cents') for item in record['items']]
    if not prices or any(value is None for value in prices):
        return None, None, None
    current = sum(item['quantity'] * value for item, value in zip(record['items'], prices))
    cost = purchase_total(record)
    profit = current - cost
    percent = Decimal(profit) * 100 / Decimal(cost) if cost else None
    return current, profit, percent


def percent_text(value) -> str:
    if value is None:
        return '—'
    return f'{value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):.2f}%'.replace('.', ',')


def valuation_columns(record: dict) -> tuple:
    current, profit, percent = valuation_metrics(record)
    return ('Não informado' if current is None else brl(current),
            '—' if profit is None else brl(profit), percent_text(percent))


def payment_status(record: dict) -> str:
    total = purchase_total(record)
    return 'Pago' if record['paid_cents'] >= total else 'Parcial' if record['paid_cents'] else 'Pendente'


def validate_purchase(record: dict) -> None:
    if not record['supplier'].strip():
        raise ValueError('Informe o fornecedor.')
    if record['date']:
        parse_date(record['date'])
    if record['status'] not in STATUSES:
        raise ValueError('Selecione a situação de recebimento.')
    if not record['items']:
        raise ValueError('Adicione pelo menos um item à compra.')
    for item in record['items']:
        if not item['name'].strip() or type(item['quantity']) is not int or item['quantity'] <= 0:
            raise ValueError('Cada item deve ter uma descrição e quantidade inteira positiva.')
        current = item.get('current_price_cents')
        if current is not None and (type(current) is not int or current < 0):
            raise ValueError('O valor atual deve ser positivo ou zero, ou ficar em branco.')
        if type(item['price_cents']) is not int or item['price_cents'] < 0:
            raise ValueError('O preço dos itens não pode ser negativo.')
    for field in ('freight_cents', 'discount_cents', 'paid_cents'):
        if type(record[field]) is not int or record[field] < 0:
            raise ValueError('Frete, desconto e pagamento devem ser valores positivos ou zero.')
    total = purchase_total(record)
    if total < 0:
        raise ValueError('O desconto não pode superar o valor dos itens e do frete.')
    if record['paid_cents'] > total:
        raise ValueError('O valor pago não pode superar o total da compra.')


class PurchaseStore:
    """Banco independente; não altera automaticamente o estoque do aplicativo."""

    def __init__(self, path: Path | str | None = None):
        base = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'PokemonCards'
        self.path = Path(path) if path is not None else base / 'purchases.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS purchases (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS purchase_imports (id TEXT PRIMARY KEY)')

    def import_records(self, payload: dict) -> int:
        """Importa compras ausentes e permite restaurar excluídas, preservando as existentes."""
        if not isinstance(payload, dict):
            raise ValueError('Arquivo de importação inválido.')
        import_id = payload.get('import_id')
        records = payload.get('purchases')
        if not isinstance(import_id, str) or not import_id.strip() or not isinstance(records, list) or not records:
            raise ValueError('O arquivo deve informar uma identificação e uma lista de compras.')
        seen = set()
        strings = ('id', 'supplier', 'date', 'reference', 'status', 'notes', 'created_at', 'updated_at')
        for record in records:
            if not isinstance(record, dict) or any(not isinstance(record.get(k), str) for k in strings):
                raise ValueError('Dados da compra ausentes ou inválidos no arquivo.')
            if not record['id'] or record['id'] in seen:
                raise ValueError('Cada compra deve ter uma identificação única no arquivo.')
            seen.add(record['id'])
            if not isinstance(record.get('items'), list):
                raise ValueError('Lista de itens inválida.')
            for item in record['items']:
                if not isinstance(item, dict) or any(not isinstance(item.get(k), str) for k in ('name', 'number', 'collection', 'extras', 'language')):
                    raise ValueError('Dados de um item ausentes ou inválidos.')
            try:
                validate_purchase(record)
            except (KeyError, TypeError) as exc:
                raise ValueError('Valores ou quantidades ausentes no arquivo.') from exc
        with sqlite3.connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            count = 0
            for record in records:
                cursor = db.execute('INSERT OR IGNORE INTO purchases(id, payload) VALUES (?, ?)',
                                    (record['id'], json.dumps(record, ensure_ascii=False)))
                count += cursor.rowcount
            db.execute('INSERT OR IGNORE INTO purchase_imports(id) VALUES (?)', (import_id,))
        return count

    def apply_quotes(self, quotes):
        changed = 0
        with sqlite3.connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            for record_id, index, original, quote in quotes:
                found = db.execute('SELECT payload FROM purchases WHERE id=?', (record_id,)).fetchone()
                if not found:
                    continue
                record = json.loads(found[0])
                if record['status'] == 'Cancelada' or index >= len(record['items']):
                    continue
                item = record['items'][index]
                if fingerprint(item) != original:
                    continue
                item.update(current_price_cents=quote.cents, current_price_source=SOURCE,
                            current_price_url=quote.url, current_price_checked_at=quote.checked_at,
                            current_price_criterion=CRITERION)
                record['updated_at'] = quote.checked_at
                validate_purchase(record)
                db.execute('UPDATE purchases SET payload=? WHERE id=?', (json.dumps(record, ensure_ascii=False),record_id))
                changed += 1
        return changed

    def all(self) -> list[dict]:
        with sqlite3.connect(self.path) as db:
            return [json.loads(row[0]) for row in db.execute('SELECT payload FROM purchases')]

    def save(self, record: dict) -> None:
        validate_purchase(record)
        payload = json.dumps(record, ensure_ascii=False)
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO purchases(id, payload) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload', (record['id'], payload))

    def mark_received(self, record_ids) -> int:
        """Atualiza apenas o recebimento em lote, com uma única transação."""
        changed = 0
        stamp = datetime.now().isoformat(timespec='seconds')
        with sqlite3.connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            for record_id in dict.fromkeys(record_ids):
                row = db.execute('SELECT payload FROM purchases WHERE id=?', (record_id,)).fetchone()
                if row is None:
                    continue
                record = json.loads(row[0])
                if record['status'] in ('Recebida', 'Cancelada'):
                    continue
                record.update(status='Recebida', received_date=date.today().isoformat(), updated_at=stamp)
                validate_purchase(record)
                db.execute('UPDATE purchases SET payload=? WHERE id=?',
                           (json.dumps(record, ensure_ascii=False), record_id))
                changed += 1
        return changed

    def delete(self, record_id: str) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute('DELETE FROM purchases WHERE id=?', (record_id,))


class PurchasesWindow:
    """Módulo de compras: histórico, filtros, resumo financeiro e cadastro de itens."""

    def __init__(self, root: tk.Toplevel, store_path: Path | str | None = None):
        self.root = root
        self.store = PurchaseStore(store_path)
        self.records = self.store.all()
        self.visible_records: list[dict] = []
        self.row_records: dict[str, dict] = {}
        self.sort_index = 0
        self.sort_reverse = True
        self.root.title('Compras realizadas · Pokémon Cards')
        self._position_window()
        self.search = tk.StringVar()
        self.status = tk.StringVar(value='Todas')
        self.payment = tk.StringVar(value='Todos')
        self.start_date = tk.StringVar()
        self.end_date = tk.StringVar()
        self._build_ui()
        self.refresh()
        self.root.bind('<Control-n>', lambda e: self.new_purchase())
        self.root.bind('<Control-f>', lambda e: self.search_entry.focus_set())
        self.root.bind('<Control-r>', lambda e: self.reload())

    def _position_window(self) -> None:
        """Centraliza na área útil, reservando 100 px abaixo da janela."""
        left, top = 0, 0
        right = self.root.winfo_screenwidth()
        bottom = self.root.winfo_screenheight()
        try:
            import ctypes
            from ctypes import wintypes
            rect = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
        except (AttributeError, OSError):
            pass
        # Margem adicional para a moldura e a barra de título do sistema.
        available_w = max(1, right - left - 24)
        available_h = max(1, bottom - top - 70 - 40)
        width = min(1600, int(available_w * 0.94))
        height = min(900, int(available_h * 0.90))
        width, height = max(1, width), max(1, height)
        x = left + max(0, (right - left - width) // 2)
        y = top + max(0, (bottom - top - 70 - height - 40) // 2)
        self.root.geometry(f"{width}x{height}{x:+d}{y:+d}")
        self.root.minsize(min(880, width), min(450, height))


    def _build_ui(self):
        outer = tb.Frame(self.root, padding=20)
        outer.pack(fill='both', expand=True)
        header = tb.Frame(outer)
        header.pack(fill='x', pady=(0, 16))
        tb.Label(header, text='Compras realizadas', font=('Segoe UI', 23, 'bold')).pack(side='left')
        tb.Button(header, text='+ Nova compra', command=self.new_purchase, bootstyle='primary', padding=(16, 10)).pack(side='right')
        tb.Label(outer, text='Registre suas aquisições e acompanhe valores, pagamentos e recebimentos.', bootstyle='secondary').pack(anchor='w', pady=(0, 14))
        cards = tb.Frame(outer)
        cards.pack(fill='x', pady=(0, 16))
        self.metrics = []
        for col, caption in enumerate(('Compras ativas', 'Cartas adquiridas', 'Valor das compras', 'Saldo a pagar')):
            cards.columnconfigure(col, weight=1)
            card = tb.Labelframe(cards, text=f' {caption} ', padding=12, bootstyle='secondary')
            card.grid(row=0, column=col, sticky='ew', padx=(0 if col == 0 else 6, 0))
            label = tb.Label(card, font=('Segoe UI', 18, 'bold'))
            label.pack(anchor='w')
            self.metrics.append(label)
        filters = tb.Labelframe(outer, text=' Buscar e filtrar ', padding=12)
        filters.pack(fill='x', pady=(0, 12))
        filters.columnconfigure(0, weight=1)
        self.search_entry = tb.Entry(filters, textvariable=self.search)
        self.search_entry.grid(row=1, column=0, sticky='ew', padx=(0, 8))
        tb.Label(filters, text='Fornecedor, referência ou carta').grid(row=0, column=0, sticky='w')
        for col, (caption, var, values) in enumerate((('Recebimento', self.status, ('Todas', *STATUSES)), ('Pagamento', self.payment, ('Todos', 'Pago', 'Parcial', 'Pendente'))), 1):
            tb.Label(filters, text=caption).grid(row=0, column=col, sticky='w')
            combo = tb.Combobox(filters, textvariable=var, values=values, state='readonly', width=14)
            combo.grid(row=1, column=col, padx=(0, 8))
            combo.bind('<<ComboboxSelected>>', lambda e: self.refresh())
        for col, caption, var in ((3, 'De (DD/MM/AAAA)', self.start_date), (4, 'Até (DD/MM/AAAA)', self.end_date)):
            tb.Label(filters, text=caption).grid(row=0, column=col, sticky='w')
            entry = tb.Entry(filters, textvariable=var, width=14)
            entry.grid(row=1, column=col, padx=(0, 8))
            entry.bind('<Return>', lambda e: self.refresh())
        tb.Button(filters, text='Filtrar', command=self.refresh).grid(row=1, column=5, padx=(0, 6))
        tb.Button(filters, text='Limpar', command=self.clear_filters, bootstyle='secondary-outline').grid(row=1, column=6)
        self.search.trace_add('write', lambda *_: self.refresh())
        actions = tb.Frame(outer)
        actions.pack(fill='x', pady=(0, 8))
        self.selected_buttons = []
        for caption, command, style in (('Detalhes', self.details, 'info-outline'), ('Valor atual', self.edit_current_value, 'success-outline'), ('Editar', self.edit_purchase, 'primary-outline'), ('Excluir', self.delete_purchase, 'danger-outline')):
            button = tb.Button(actions, text=caption, command=command, bootstyle=style, state='disabled')
            button.pack(side='left', padx=(0, 8))
            self.selected_buttons.append(button)
        self.received_button = tb.Button(actions, text='Recebimento OK', command=self.mark_selected_received,
                                         bootstyle='success', state='disabled')
        self.received_button.pack(side='left', padx=(0, 8))
        self.update_values_button = tb.Button(actions, text='Atualizar valores', command=self.update_market_values, bootstyle='success')
        self.update_values_button.pack(side='right', padx=(8, 0))
        tb.Button(actions, text='Atualizar', command=self.reload, bootstyle='secondary-outline').pack(side='right')
        tb.Button(actions, text='Importar compras', command=self.import_purchases, bootstyle='info-outline').pack(side='right', padx=(0, 8))
        tb.Button(actions, text='Exportar CSV', command=self.export_csv, bootstyle='success-outline').pack(side='right', padx=8)
        columns = [
            ('Data', 100), ('Fornecedor', 210), ('Referência', 140), ('Qtd cartas', 95),
            ('Total', 125), ('Pago', 125), ('Saldo', 125), ('Pagamento', 110), ('Recebimento', 120),
            ('Valor atual', 130), ('Lucro líquido R$', 140), ('Lucro líquido %', 130),
        ]
        table_area = tb.Frame(outer)
        table_area.pack(fill='both', expand=True)
        table_area.columnconfigure(0, weight=1)
        table_area.rowconfigure(0, weight=1)
        self.table = Tableview(table_area, coldata=[{'text': title, 'width': width, 'minwidth': 60, 'stretch': True} for title, width in columns], rowdata=[], paginated=False, searchable=False, bootstyle='primary', stripecolor=('#f8f9fa', '#212529'))
        self.table.grid(row=0, column=0, sticky='nsew')
        self.tree = self.table.view
        self.vertical_scrollbar = tb.Scrollbar(
            table_area, orient='vertical', command=self.tree.yview,
        )
        self.vertical_scrollbar.grid(row=0, column=1, sticky='ns')
        self.tree.configure(selectmode='extended', yscrollcommand=self.vertical_scrollbar.set)
        for index, cid in enumerate(self.tree['columns']):
            self.tree.heading(cid, command=lambda index=index: self.sort_by(index))
            self.tree.column(cid, anchor='e' if index in (4, 5, 6, 9, 10, 11) else 'center' if index in (0, 3, 7, 8) else 'w')
        for tag, bg, fg in (('received', '#E6F4EA', '#1E4620'), ('waiting', '#FFF4D6', '#6B4F00'), ('cancelled', '#FDECEC', '#8A1C1C'), ('unknown', '#EEF2F6', '#334155')):
            self.tree.tag_configure(tag, background=bg, foreground=fg)
        self.tree.bind('<ButtonPress-1>', self._begin_column_resize, add='+')
        self.tree.bind('<ButtonRelease-1>', self._end_column_resize, add='+')
        self.tree.bind('<<TreeviewSelect>>', self._selection_changed)
        self.tree.bind('<Control-a>', self._select_all_purchases)
        self.tree.bind('<Control-A>', self._select_all_purchases)
        self.tree.bind('<Double-1>', self._double_click)
        self.tree.bind('<Return>', lambda e: self.details())
        self.tree.bind('<F2>', lambda e: self.edit_purchase())
        self.tree.bind('<Delete>', lambda e: self.delete_purchase())
        self.hint = tb.Label(outer, bootstyle='secondary')
        self.hint.pack(anchor='w', pady=(10, 0))

    def _begin_column_resize(self, event):
        self._resizing_column = self.tree.identify_region(event.x, event.y) == 'separator'
        if not self._resizing_column:
            return
        # Congela a distribuição automática antes do arraste nativo do Treeview.
        self._column_widths = {cid: self.tree.column(cid, 'width') for cid in self.tree['columns']}
        self._restore_column_widths()

    def _end_column_resize(self, _event=None):
        if not getattr(self, '_resizing_column', False):
            return
        self._resizing_column = False
        self._column_widths = {cid: self.tree.column(cid, 'width') for cid in self.tree['columns']}
        self.tree.after_idle(self._restore_column_widths)

    def _restore_column_widths(self):
        for cid, width in getattr(self, '_column_widths', {}).items():
            self.tree.column(cid, width=width, stretch=False)

    @staticmethod
    def sort_key(record, index):
        total = purchase_total(record)
        values = (record['date'], record['supplier'].casefold(), record['reference'].casefold(),
                  sum(i['quantity'] for i in record['items']), total, record['paid_cents'],
                  total-record['paid_cents'], payment_status(record), record['status'])
        if index >= 9:
            value = valuation_metrics(record)[index-9]
            return (value is not None, value if value is not None else 0)
        return values[index]

    def sort_by(self, index):
        self.sort_reverse = not self.sort_reverse if self.sort_index == index else False
        self.sort_index = index
        self.refresh()

    def selected_records(self):
        return [self.row_records[iid] for iid in self.tree.selection() if iid in self.row_records]

    def selected(self):
        records = self.selected_records()
        return records[0] if len(records) == 1 else None

    def _selection_changed(self, _event=None):
        records = self.selected_records()
        for button in self.selected_buttons:
            button.configure(state='normal' if len(records) == 1 else 'disabled')
        self.received_button.configure(state='normal' if any(
            record['status'] not in ('Recebida', 'Cancelada') for record in records) else 'disabled')

    def _select_all_purchases(self, _event=None):
        self.tree.selection_set(self.tree.get_children())
        self._selection_changed()
        return 'break'

    def mark_selected_received(self):
        records = self.selected_records()
        if not records:
            return
        try:
            changed = self.store.mark_received([record['id'] for record in records])
        except (OSError, sqlite3.Error, ValueError) as exc:
            messagebox.showerror('Recebimento OK', f'Não foi possível registrar o recebimento:\n{exc}', parent=self.root)
            return
        self.reload()
        self.hint.configure(text=f'{changed} compra(s) marcada(s) como Recebida. Compras canceladas não foram alteradas.',
                            bootstyle='success')

    def _double_click(self, event):
        if self.tree.identify_region(event.x, event.y) == 'cell':
            iid = self.tree.identify_row(event.y)
            if iid:
                self.tree.selection_set(iid)
                if self.tree.identify_column(event.x) == '#10':
                    self.edit_current_value()
                else:
                    self.details()
        return 'break'

    def clear_filters(self):
        self.status.set('Todas')
        self.payment.set('Todos')
        self.start_date.set('')
        self.end_date.set('')
        self.search.set('')
        self.refresh()

    def reload(self):
        try:
            self.records = self.store.all()
            self.refresh()
        except (OSError, sqlite3.Error, ValueError) as exc:
            messagebox.showerror('Compras', f'Não foi possível carregar as compras:\n{exc}', parent=self.root)

    def refresh(self):
        try:
            start = parse_date(self.start_date.get()) if self.start_date.get().strip() else ''
            end = parse_date(self.end_date.get()) if self.end_date.get().strip() else ''
            if start and end and start > end:
                raise ValueError('A data inicial deve ser anterior ou igual à data final.')
        except ValueError as exc:
            self.hint.configure(text=str(exc), bootstyle='danger')
            return
        selected_ids = {record['id'] for record in self.selected_records()}
        query = self.search.get().strip().casefold()
        rows = []
        for record in self.records:
            searchable = ' '.join([record['supplier'], record['reference'], *(i['name'] for i in record['items'])]).casefold()
            if query and query not in searchable:
                continue
            if self.status.get() != 'Todas' and record['status'] != self.status.get():
                continue
            if self.payment.get() != 'Todos' and payment_status(record) != self.payment.get():
                continue
            if ((start or end) and not record['date']) or (start and record['date'] < start) or (end and record['date'] > end):
                continue
            rows.append(record)
        rows.sort(key=lambda r: self.sort_key(r, self.sort_index), reverse=self.sort_reverse)
        self.visible_records = rows
        self.table.delete_rows()
        self.row_records.clear()
        created = []
        for record in rows:
            total = purchase_total(record)
            row = self.table.insert_row('end', (display_date(record['date']), record['supplier'], record['reference'], sum(i['quantity'] for i in record['items']), brl(total), brl(record['paid_cents']), brl(total-record['paid_cents']), payment_status(record), record['status'], *valuation_columns(record)))
            created.append((row, record))
        self.table.load_table_data()
        self._restore_column_widths()
        restored_selection = []
        for row, record in created:
            self.row_records[row.iid] = record
            self.tree.item(row.iid, tags=({'Recebida': 'received', 'Aguardando': 'waiting', 'Cancelada': 'cancelled', 'Não informado': 'unknown'}[record['status']],))
            if record['id'] in selected_ids:
                restored_selection.append(row.iid)
        if restored_selection:
            self.tree.selection_set(restored_selection)
            self.tree.focus(restored_selection[0])
        active = [r for r in rows if r['status'] != 'Cancelada']
        values = (str(len(active)), str(sum(i['quantity'] for r in active for i in r['items'])), brl(sum(purchase_total(r) for r in active)), brl(sum(purchase_total(r)-r['paid_cents'] for r in active)))
        for label, value in zip(self.metrics, values):
            label.configure(text=value)
        self.hint.configure(text=f'{len(rows)} compra(s) exibida(s) · Resumo dos filtros, sem compras canceladas · Ctrl/Shift: selecionar várias · Ctrl+A: todas · F2: editar', bootstyle='secondary')
        self._selection_changed()

    def new_purchase(self):
        PurchaseEditor(self)

    def edit_purchase(self):
        record = self.selected()
        if record:
            PurchaseEditor(self, record)

    def delete_purchase(self):
        record = self.selected()
        if record and messagebox.askyesno('Excluir compra', f'Excluir a compra de {record["supplier"]}, em {display_date(record["date"])}?\nEsta ação não pode ser desfeita.', parent=self.root):
            try:
                self.store.delete(record['id'])
                self.reload()
            except sqlite3.Error as exc:
                messagebox.showerror('Compras', str(exc), parent=self.root)

    def details(self):
        record = self.selected()
        if record:
            PurchaseDetailsWindow(
                self.root,
                record,
                format_money=brl,
                format_date=display_date,
                calculate_total=purchase_total,
                payment_label=payment_status,
                parse_money=money_cents,
                money_input=money_input,
                valuation=valuation_metrics,
                format_percent=percent_text,
                on_save=self.save_current_values,
            )

    def save_current_values(self, record):
        record['updated_at'] = datetime.now().isoformat(timespec='seconds')
        self.store.save(record)
        self.reload()

    def edit_current_value(self):
        record = self.selected()
        if not record:
            return
        if len(record['items']) != 1:
            self.details()
            return
        item = record['items'][0]
        current = item.get('current_price_cents')
        text = simpledialog.askstring(
            'Valor atual da carta',
            f'{item["name"]} · Quantidade: {item["quantity"]}\n'
            'Valor atual por unidade (R$). Deixe em branco para remover:',
            initialvalue=money_input(current) if current is not None else '', parent=self.root,
        )
        if text is None:
            return
        try:
            updated = json.loads(json.dumps(record))
            updated['items'][0]['current_price_cents'] = money_cents(text) if text.strip() else None
            self.save_current_values(updated)
        except (ValueError, OSError, sqlite3.Error) as exc:
            messagebox.showerror('Valor atual', str(exc), parent=self.root)

    def update_market_values(self):
        if getattr(self, '_price_update_running', False):
            return
        try:
            records = self.store.all()
        except (OSError, ValueError, sqlite3.Error) as exc:
            messagebox.showerror('Atualizar valores', str(exc), parent=self.root)
            return
        targets = [(r['id'], index, json.loads(json.dumps(item)))
                   for r in records if r['status'] != 'Cancelada'
                   for index, item in enumerate(r['items'])]
        if not targets:
            messagebox.showinfo('Atualizar valores', 'Não há cartas em compras ativas.', parent=self.root)
            return
        if not getattr(self, '_api_general_reference_allowed', False):
            if not messagebox.askyesno(
                'Critério da API oficial',
                'A API do MYP Cards fornece preços gerais em reais, sem filtro documentado por idioma ou condição NM.\n\n'
                'Deseja usar essa cotação geral como referência? Ela não garante o preço de uma oferta exclusivamente em português.',
                parent=self.root, default='no',
            ):
                return
            self._api_general_reference_allowed = True
        token = getattr(self, '_myp_api_token', '') or os.environ.get('MYPCARDS_API_TOKEN', '').strip()
        if not token:
            token = simpledialog.askstring(
                'Token da API MYP Cards',
                'Informe o token autorizado fornecido pelo suporte do MYP Cards.\n'
                'O token ficará somente na memória durante esta execução.',
                show='*', parent=self.root,
            )
        if not token or not token.strip():
            return
        token = token.strip()
        if '\n' in token or '\r' in token:
            messagebox.showerror('Token', 'Token inválido.', parent=self.root)
            return
        self._myp_api_token = token
        self._price_update_running = True
        self.update_values_button.configure(state='disabled')
        events = queue.Queue()
        stop = threading.Event()
        progress = tb.Toplevel(self.root)
        progress.title('Atualizar valores · API MYP Cards')
        progress.geometry('740x400')
        progress.transient(self.root)
        progress.grab_set()
        box = tb.Frame(progress, padding=16)
        box.pack(fill='both', expand=True)
        tb.Label(box, text='Consultando API oficial · Cotação geral', font=('Segoe UI', 14, 'bold')).pack(anchor='w')
        tb.Label(box, text='Menor preço geral em reais por unidade, sem filtro de idioma/NM. Sem correspondência confirmada, mantém o valor anterior.', wraplength=700, bootstyle='secondary').pack(anchor='w', pady=8)
        status = tb.Label(box, text=f'0 / {len(targets)} cartas')
        status.pack(anchor='w')
        bar = tb.Progressbar(box, maximum=len(targets), mode='determinate')
        bar.pack(fill='x', pady=8)
        log = tk.Text(box, height=8, wrap='word', state='disabled', font=('Segoe UI',9))
        log.pack(fill='both', expand=True)
        def cancel():
            stop.set()
            status.configure(text='Cancelando após concluir a consulta em andamento…')
            cancel_button.configure(state='disabled')
        cancel_button = tb.Button(box, text='Cancelar', command=cancel, bootstyle='secondary')
        cancel_button.pack(anchor='e', pady=(8,0))
        progress.protocol('WM_DELETE_WINDOW', cancel)
        def worker():
            provider = MypPriceProvider(token=token)
            quotes = []
            cache = {}
            for n, (record_id, index, item) in enumerate(targets,1):
                if stop.is_set():
                    break
                key = fingerprint(item)[:5]
                try:
                    if key not in cache:
                        if cache and stop.wait(1.0):
                            break
                        try:
                            cache[key] = provider.get_quote(item)
                        except QuoteUnavailable as exc:
                            cache[key] = exc
                    result = cache[key]
                    if isinstance(result, Exception):
                        raise result
                    quotes.append((record_id,index,fingerprint(item),result))
                    message = f'{item["name"]} ({item.get("number", "")}): {brl(result.cents)}'
                except SourceBlocked as exc:
                    events.put(('blocked',n,str(exc)))
                    break
                except Exception as exc:
                    message = f'{item["name"]}: {exc}'
                events.put(('progress',n,message))
            events.put(('done',quotes,stop.is_set()))
        def poll():
            try:
                if not self.root.winfo_exists():
                    stop.set()
                    return
                while True:
                    event = events.get_nowait()
                    if event[0] in ('progress', 'blocked'):
                        _,number,message = event
                        if event[0] == 'blocked' and ('token' in message.casefold() or 'acesso' in message.casefold()):
                            self._myp_api_token = ''
                        status.configure(text=f'{number} / {len(targets)} cartas')
                        bar.configure(value=number)
                        log.configure(state='normal')
                        log.insert('end',message+'\n')
                        log.see('end')
                        log.configure(state='disabled')
                    else:
                        _,quotes,cancelled = event
                        try:
                            saved = self.store.apply_quotes(quotes)
                            self.reload()
                            status.configure(text=f'{saved} valor(es) salvo(s). Cartas sem confirmação mantiveram seus valores.' + (' Consulta cancelada.' if cancelled else ''))
                        except (OSError, ValueError, sqlite3.Error) as exc:
                            status.configure(text=f'Não foi possível salvar os valores: {exc}', bootstyle='danger')
                        self._price_update_running = False
                        self.update_values_button.configure(state='normal')
                        cancel_button.configure(text='Fechar',state='normal',command=progress.destroy)
                        progress.protocol('WM_DELETE_WINDOW',progress.destroy)
                        return
            except queue.Empty:
                self.root.after(100,poll)
            except tk.TclError:
                stop.set()
                self._price_update_running = False
        threading.Thread(target=worker,daemon=True).start()
        self.root.after(100,poll)

    def import_purchases(self):
        path = filedialog.askopenfilename(parent=self.root, title='Importar compras',
                                          filetypes=[('Dados de compras', '*.json')])
        if not path:
            return
        try:
            with open(path, encoding='utf-8-sig') as handle:
                payload = json.load(handle)
            count = self.store.import_records(payload)
            self.reload()
            messagebox.showinfo('Importar compras', f'{count} compra(s) adicionada(s).' if count else
                                'Todas as compras deste arquivo já estão cadastradas. Nenhuma compra foi duplicada ou alterada.', parent=self.root)
        except (OSError, sqlite3.Error, ValueError) as exc:
            messagebox.showerror('Importar compras', str(exc), parent=self.root)

    def export_csv(self):
        if not self.visible_records:
            messagebox.showinfo('Exportar', 'Não há compras nos filtros atuais.', parent=self.root)
            return
        path = filedialog.asksaveasfilename(parent=self.root, title='Exportar compras filtradas', defaultextension='.csv', initialfile='compras_realizadas.csv', filetypes=[('Arquivo CSV', '*.csv')])
        if not path:
            return
        def safe(value):
            value = str(value)
            return "'"+value if value.lstrip().startswith(('=', '+', '-', '@')) else value
        try:
            with open(path, 'w', newline='', encoding='utf-8-sig') as handle:
                writer = csv.writer(handle, delimiter=';')
                writer.writerow(('Data', 'Fornecedor', 'Referência', 'Quantidade', 'Total', 'Pago', 'Saldo', 'Pagamento', 'Recebimento', 'Valor atual', 'Lucro líquido R$', 'Lucro líquido %', 'Observações'))
                for r in self.visible_records:
                    total = purchase_total(r)
                    writer.writerow([safe(v) for v in (display_date(r['date']), r['supplier'], r['reference'], sum(i['quantity'] for i in r['items']), money_input(total), money_input(r['paid_cents']), money_input(total-r['paid_cents']), payment_status(r), r['status'], *valuation_columns(r), r['notes'])])
            messagebox.showinfo('Exportar', 'Arquivo CSV exportado.', parent=self.root)
        except OSError as exc:
            messagebox.showerror('Exportar', str(exc), parent=self.root)


class PurchaseEditor:
    def __init__(self, owner: PurchasesWindow, record: dict | None = None):
        self.owner = owner
        self.record = json.loads(json.dumps(record)) if record else None
        self.items = self.record['items'] if record else []
        self.edit_index = None
        self.top = tb.Toplevel(owner.root)
        self.top.title('Editar compra' if record else 'Nova compra')
        width = min(1180, max(800, self.top.winfo_screenwidth() - 80))
        height = min(850, max(560, self.top.winfo_screenheight() - 100))
        self.top.geometry(f'{width}x{height}')
        self.top.minsize(min(930, width), min(680, height))
        self.top.transient(owner.root)
        self.top.grab_set()
        self.top.protocol('WM_DELETE_WINDOW', self.close)
        self.top.bind('<Escape>', lambda e: self.close())
        self.top.bind('<Control-s>', lambda e: self.save())
        box = tb.Frame(self.top, padding=18)
        box.pack(fill='both', expand=True)
        tb.Label(box, text='Editar compra' if record else 'Nova compra', font=('Segoe UI', 21, 'bold')).pack(anchor='w', pady=(0, 12))
        data = record or {}
        self.vars = {key: tk.StringVar(value=value) for key, value in dict(supplier=data.get('supplier', ''), date=display_date(data.get('date', date.today().isoformat())) if data.get('date', date.today().isoformat()) else '', reference=data.get('reference', ''), status=data.get('status', 'Recebida'), freight=money_input(data.get('freight_cents', 0)), discount=money_input(data.get('discount_cents', 0)), paid=money_input(data.get('paid_cents', 0))).items()}
        header = tb.Labelframe(box, text=' Dados da compra ', padding=10)
        header.pack(fill='x', pady=(0, 10))
        for col, (key, caption, width) in enumerate((('supplier', 'Fornecedor *', 30), ('date', 'Data (DD/MM/AAAA)', 16), ('reference', 'Referência / pedido', 22), ('status', 'Recebimento', 15))):
            header.columnconfigure(col, weight=1)
            tb.Label(header, text=caption).grid(row=0, column=col, sticky='w')
            widget = tb.Combobox(header, textvariable=self.vars[key], values=STATUSES, state='readonly', width=width) if key == 'status' else tb.Entry(header, textvariable=self.vars[key], width=width)
            widget.grid(row=1, column=col, sticky='ew', padx=(0, 8))
            if key == 'supplier':
                widget.focus_set()
        item_box = tb.Labelframe(box, text=' Itens da compra ', padding=10)
        item_box.pack(fill='both', expand=True)
        form = tb.Frame(item_box)
        form.pack(fill='x')
        self.item_vars = {k: tk.StringVar(value='1' if k == 'quantity' else '0,00' if k == 'price' else 'PT' if k == 'language' else '') for k in ('name', 'number', 'collection', 'extras', 'language', 'quantity', 'price')}
        specs = (('name', 'Carta / produto *', 26), ('number', 'Nº', 8), ('collection', 'Coleção', 12), ('extras', 'Extras', 16), ('language', 'Idioma', 6), ('quantity', 'Qtd *', 6), ('price', 'Preço unit. *', 12))
        for col, (key, caption, width) in enumerate(specs):
            form.columnconfigure(col, weight=1 if key == 'name' else 0)
            tb.Label(form, text=caption).grid(row=0, column=col, sticky='w')
            widget = tb.Combobox(form, textvariable=self.item_vars[key], values=EXTRAS if key == 'extras' else ('PT', 'EN', 'JP', 'ES', 'FR', 'DE', 'IT', 'KO', 'ZH'), width=width) if key in ('extras', 'language') else tb.Entry(form, textvariable=self.item_vars[key], width=width)
            widget.grid(row=1, column=col, sticky='ew', padx=(0, 6))
        tools = tb.Frame(item_box)
        tools.pack(fill='x', pady=8)
        self.add_button = tb.Button(tools, text='Adicionar item', command=self.add_item)
        self.add_button.pack(side='left')
        tb.Button(tools, text='Limpar campos', command=self.clear_item, bootstyle='secondary-outline').pack(side='left', padx=8)
        tb.Button(tools, text='Remover selecionado', command=self.remove_item, bootstyle='danger-outline').pack(side='right')
        self.item_table = Tableview(item_box, coldata=[{'text': t, 'width': w, 'stretch': t == 'Carta / produto'} for t, w in [('Carta / produto', 250), ('Nº', 60), ('Coleção', 95), ('Extras', 145), ('Idioma', 65), ('Qtd', 60), ('Preço unit.', 105), ('Total', 105)]], rowdata=[], paginated=False, searchable=False, height=6, stripecolor=('#f8f9fa', '#212529'))
        self.item_table.pack(fill='both', expand=True)
        self.item_tree = self.item_table.view
        self.item_tree.configure(selectmode='browse')
        self.item_tree.bind('<<TreeviewSelect>>', self.select_item)
        self.item_rows = {}
        payment = tb.Labelframe(box, text=' Valores e pagamento ', padding=10)
        payment.pack(fill='x', pady=10)
        for col, (key, caption) in enumerate((('freight', 'Frete (R$)'), ('discount', 'Desconto (R$)'), ('paid', 'Valor já pago (R$)'))):
            tb.Label(payment, text=caption).grid(row=0, column=col, sticky='w')
            tb.Entry(payment, textvariable=self.vars[key], width=17).grid(row=1, column=col, padx=(0, 12))
            self.vars[key].trace_add('write', lambda *_: self.update_total())
        self.total_label = tb.Label(payment, font=('Segoe UI', 12, 'bold'), bootstyle='success')
        self.total_label.grid(row=1, column=3, sticky='e')
        payment.columnconfigure(3, weight=1)
        tb.Label(box, text='Observações').pack(anchor='w')
        self.notes = tk.Text(box, height=3, wrap='word', font=('Segoe UI', 10))
        self.notes.pack(fill='x', pady=(3, 8))
        self.notes.insert('1.0', data.get('notes', ''))
        buttons = tb.Frame(box)
        buttons.pack(fill='x')
        tb.Button(buttons, text='Salvar compra · Ctrl+S', command=self.save, bootstyle='success', padding=(14, 8)).pack(side='right')
        tb.Button(buttons, text='Cancelar', command=self.close, bootstyle='secondary-outline').pack(side='right', padx=8)
        self.render_items()
        self.initial_state = self.state()

    def state(self):
        return json.dumps([self.items, {k: v.get() for k, v in self.vars.items()}, self.notes.get('1.0', 'end-1c'), {k:v.get() for k,v in self.item_vars.items()}], ensure_ascii=False, sort_keys=True)

    def close(self):
        if self.state() != self.initial_state and not messagebox.askyesno('Descartar alterações', 'Fechar sem salvar as alterações desta compra?', parent=self.top):
            return
        self.top.destroy()

    def select_item(self, _event=None):
        selection = self.item_tree.selection()
        index = self.item_rows.get(selection[0]) if selection else None
        if index is None:
            return
        self.edit_index = index
        item = self.items[index]
        for key, var in self.item_vars.items():
            var.set(money_input(item['price_cents']) if key == 'price' else str(item[key]))
        self.add_button.configure(text='Aplicar alteração')

    def clear_item(self):
        self.edit_index = None
        for key, var in self.item_vars.items():
            var.set('1' if key == 'quantity' else '0,00' if key == 'price' else 'PT' if key == 'language' else '')
        self.item_tree.selection_remove(*self.item_tree.selection())
        self.add_button.configure(text='Adicionar item')

    def add_item(self):
        try:
            item = {key: var.get().strip() for key, var in self.item_vars.items() if key not in ('price', 'quantity')}
            item['quantity'] = int(self.item_vars['quantity'].get())
            item['price_cents'] = money_cents(self.item_vars['price'].get())
            if not item['name'] or item['quantity'] <= 0:
                raise ValueError('Informe a carta / produto e uma quantidade inteira positiva.')
        except ValueError as exc:
            messagebox.showerror('Item da compra', str(exc), parent=self.top)
            return
        item['current_price_cents'] = self.items[self.edit_index].get('current_price_cents') if self.edit_index is not None else None
        if self.edit_index is None:
            self.items.append(item)
        else:
            self.items[self.edit_index] = item
        self.clear_item()
        self.render_items()

    def remove_item(self):
        selection = self.item_tree.selection()
        index = self.item_rows.get(selection[0]) if selection else None
        if index is not None:
            self.items.pop(index)
            self.clear_item()
            self.render_items()

    def render_items(self):
        self.item_table.delete_rows()
        self.item_rows.clear()
        rows = []
        for index, i in enumerate(self.items):
            row = self.item_table.insert_row('end', (i['name'], i['number'], i['collection'], i['extras'], i['language'], i['quantity'], brl(i['price_cents']), brl(i['quantity']*i['price_cents'])))
            rows.append((row, index))
        self.item_table.insert_row('end', ('Total dos itens', '', '', '', '', sum(i['quantity'] for i in self.items), '', brl(sum(i['quantity']*i['price_cents'] for i in self.items))))
        self.item_table.load_table_data()
        for cid in self.item_tree['columns']:
            self.item_tree.heading(cid, command='')
        for row, index in rows:
            self.item_rows[row.iid] = index
        self.update_total()

    def update_total(self):
        try:
            total = sum(i['quantity']*i['price_cents'] for i in self.items) + money_cents(self.vars['freight'].get()) - money_cents(self.vars['discount'].get())
            paid = money_cents(self.vars['paid'].get())
            if total < 0 or paid > total:
                raise ValueError()
            self.total_label.configure(text=f'Total: {brl(total)} · Saldo: {brl(total-paid)}', bootstyle='success')
        except ValueError:
            self.total_label.configure(text='Revise desconto e pagamento', bootstyle='danger')

    def save(self):
        if self.item_vars['name'].get().strip():
            messagebox.showwarning('Item em edição', 'Clique em Adicionar item ou Aplicar alteração antes de salvar. Para descartar os campos, clique em Limpar campos.', parent=self.top)
            return
        try:
            record = dict(id=self.record['id'] if self.record else str(uuid4()), created_at=self.record['created_at'] if self.record else datetime.now().isoformat(timespec='seconds'), updated_at=datetime.now().isoformat(timespec='seconds'), supplier=self.vars['supplier'].get().strip(), date=parse_date(self.vars['date'].get()) if self.vars['date'].get().strip() else '', reference=self.vars['reference'].get().strip(), status=self.vars['status'].get(), items=self.items, freight_cents=money_cents(self.vars['freight'].get()), discount_cents=money_cents(self.vars['discount'].get()), paid_cents=money_cents(self.vars['paid'].get()), notes=self.notes.get('1.0', 'end-1c').strip())
            if self.record:
                for field in ('source_row', 'source_sheet'):
                    if field in self.record:
                        record[field] = self.record[field]
            self.owner.store.save(record)
        except (ValueError, sqlite3.Error, OSError) as exc:
            messagebox.showerror('Salvar compra', str(exc), parent=self.top)
            return
        self.top.destroy()
        self.owner.reload()
