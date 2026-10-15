from __future__ import annotations

import json
import tkinter as tk
from tkinter import messagebox
from typing import Callable

import ttkbootstrap as tb
from ttkbootstrap.tableview import Tableview


class PurchaseDetailsWindow:
    """Detalhes da compra e edição dos valores atuais por unidade."""

    def __init__(
        self, parent: tk.Misc, record: dict, *,
        format_money: Callable, format_date: Callable, calculate_total: Callable,
        payment_label: Callable, parse_money: Callable, money_input: Callable,
        valuation: Callable, format_percent: Callable, on_save: Callable,
    ) -> None:
        self.record = json.loads(json.dumps(record))
        self.original = json.dumps(self.record, sort_keys=True)
        self.format_money = format_money
        self.calculate_total = calculate_total
        self.parse_money = parse_money
        self.money_input = money_input
        self.valuation = valuation
        self.format_percent = format_percent
        self.on_save = on_save
        self.selected_index = None
        self.rows = {}
        self.root = top = tb.Toplevel(parent)
        top.withdraw()
        top.title('Detalhes da compra · Valor atual e lucro')
        top.transient(parent)
        top.protocol('WM_DELETE_WINDOW', self.close)
        top.bind('<Escape>', lambda e: self.close())
        top.bind('<Control-s>', lambda e: self.save())
        box = tb.Frame(top, padding=20)
        box.pack(fill='both', expand=True)
        tb.Label(box, text=record['supplier'], font=('Segoe UI', 20, 'bold')).pack(anchor='w')
        tb.Label(box, text=f'{format_date(record["date"])} · {record["reference"] or "Sem referência"} · {record["status"]} · {payment_label(record)}', bootstyle='secondary').pack(anchor='w', pady=(4, 12))
        area = tb.Frame(box)
        area.pack(fill='both', expand=True)
        area.rowconfigure(0, weight=1)
        area.columnconfigure(0, weight=1)
        columns = [('Carta / produto', 250), ('Nº', 85), ('Coleção', 100), ('Extras', 130), ('Idioma', 65), ('Qtd', 60), ('Preço pago unit.', 120), ('Total pago itens', 125), ('Valor atual unit.', 125), ('Valor atual total', 135)]
        self.table = Tableview(area, coldata=[{'text': t, 'width': w, 'minwidth': 60, 'stretch': True} for t, w in columns], rowdata=[], paginated=False, searchable=False, stripecolor=('#f8f9fa', '#212529'))
        self.table.grid(row=0, column=0, sticky='nsew')
        self.tree = self.table.view
        self.tree.configure(selectmode='browse')
        bar = tb.Scrollbar(area, orient='vertical', command=self.tree.yview)
        bar.grid(row=0, column=1, sticky='ns')
        self.tree.configure(yscrollcommand=bar.set)
        for index, cid in enumerate(self.tree['columns']):
            self.tree.column(cid, anchor='e' if index >= 6 else 'center' if index in (1, 4, 5) else 'w')
        self.tree.bind('<ButtonPress-1>', self._begin_column_resize, add='+')
        self.tree.bind('<ButtonRelease-1>', self._end_column_resize, add='+')
        self.tree.bind('<<TreeviewSelect>>', self.select_item)
        self.tree.bind('<Double-1>', self.focus_value)
        self.tree.bind('<F2>', self.focus_value)
        edit = tb.Labelframe(box, text=' Valor atual por carta ', padding=10)
        edit.pack(fill='x', pady=(12, 0))
        self.item_label = tb.Label(edit, text='Selecione uma carta na tabela.')
        self.item_label.pack(side='left', fill='x', expand=True)
        self.current_value = tk.StringVar()
        self.value_entry = tb.Entry(edit, textvariable=self.current_value, width=16, state='disabled')
        self.value_entry.pack(side='left', padx=8)
        self.value_entry.bind('<Return>', lambda e: self.apply_value())
        self.apply_button = tb.Button(edit, text='Aplicar valor', command=self.apply_value, state='disabled')
        self.apply_button.pack(side='left')
        tb.Label(box, text='Informe o valor atual por unidade. Em branco = não informado; 0 = valor zero. Aplique antes de selecionar outra carta.', bootstyle='secondary', wraplength=1100).pack(anchor='w', pady=(4, 8))
        total = calculate_total(record)
        tb.Label(box, text=f'Frete: {format_money(record["freight_cents"])} · Desconto: {format_money(record["discount_cents"])} · Custo da compra: {format_money(total)} · Pago: {format_money(record["paid_cents"])} · Saldo: {format_money(total-record["paid_cents"])}', wraplength=1100).pack(anchor='w', pady=(0, 6))
        self.profit_label = tb.Label(box, font=('Segoe UI', 12, 'bold'), wraplength=1100)
        self.profit_label.pack(anchor='w')
        tb.Label(box, text='Lucro líquido = valor atual total − custo da compra (com frete e desconto). Percentual sobre o custo.', bootstyle='secondary', wraplength=1100).pack(anchor='w', pady=(4, 8))
        notes = tk.Text(box, height=3, wrap='word', font=('Segoe UI', 9))
        notes.pack(fill='x')
        notes.insert('1.0', record['notes'] or 'Sem observações.')
        notes.configure(state='disabled')
        buttons = tb.Frame(box)
        buttons.pack(fill='x', pady=(10, 0))
        tb.Button(buttons, text='Salvar valores · Ctrl+S', command=self.save, bootstyle='success').pack(side='right')
        tb.Button(buttons, text='Fechar', command=self.close, bootstyle='secondary-outline').pack(side='right', padx=8)
        self.render()
        top.update_idletasks()
        self._position_window()
        top.deiconify()
        top.lift()
        top.grab_set()

    def _position_window(self) -> None:
        """Centraliza na área útil e reserva 100 px extras abaixo da janela."""
        left, top = 0, 0
        right, bottom = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        try:
            import ctypes
            from ctypes import wintypes
            rect = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
        except (AttributeError, OSError):
            pass
        available_w = max(1, right - left - 24)
        # A reserva adicional considera também a moldura e a barra de título.
        available_h = max(1, bottom - top - 100 - 40)
        width, height = min(1280, available_w), min(760, available_h)
        x = left + max(0, (right - left - width) // 2)
        y = top + max(0, (available_h - height) // 2)
        self.root.geometry(f'{width}x{height}{x:+d}{y:+d}')
        self.root.minsize(min(900, width), min(550, height))

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

    def render(self):
        selected = self.selected_index
        self.table.delete_rows()
        self.rows.clear()
        created = []
        money = self.format_money
        for index, item in enumerate(self.record['items']):
            current = item.get('current_price_cents')
            row = self.table.insert_row('end', (item['name'], item['number'], item['collection'], item['extras'], item['language'], item['quantity'], money(item['price_cents']), money(item['quantity']*item['price_cents']), 'Não informado' if current is None else money(current), '—' if current is None else money(current*item['quantity'])))
            created.append((row, index))
        current, profit, percent = self.valuation(self.record)
        self.table.insert_row('end', ('Total dos itens', '', '', '', '', sum(i['quantity'] for i in self.record['items']), '', money(sum(i['quantity']*i['price_cents'] for i in self.record['items'])), '', 'Não informado' if current is None else money(current)))
        self.table.load_table_data()
        self._restore_column_widths()
        for cid in self.tree['columns']:
            self.tree.heading(cid, command='')
        for row, index in created:
            self.rows[row.iid] = index
            if index == selected:
                self.tree.selection_set(row.iid)
                self.tree.focus(row.iid)
        if current is None:
            self.profit_label.configure(text='Preencha o valor atual de todas as cartas para calcular o lucro da compra.', bootstyle='secondary')
        else:
            self.profit_label.configure(text=f'Valor atual: {money(current)} · Lucro líquido: {money(profit)} · Lucro líquido: {self.format_percent(percent)}', bootstyle='danger' if profit < 0 else 'success')

    def select_item(self, _event=None):
        ids = self.tree.selection()
        self.selected_index = self.rows.get(ids[0]) if ids else None
        valid = self.selected_index is not None
        self.value_entry.configure(state='normal' if valid else 'disabled')
        self.apply_button.configure(state='normal' if valid else 'disabled')
        if valid:
            item = self.record['items'][self.selected_index]
            self.item_label.configure(text=f'{item["name"]} · Qtd: {item["quantity"]}')
            value = item.get('current_price_cents')
            self.current_value.set(self.money_input(value) if value is not None else '')
        else:
            self.item_label.configure(text='Selecione uma carta na tabela.')
            self.current_value.set('')

    def focus_value(self, event=None):
        if event is not None and hasattr(event, 'x') and getattr(event, 'num', None) == 1:
            iid = self.tree.identify_row(event.y)
            if iid:
                self.tree.selection_set(iid)
                self.select_item()
        if self.selected_index is not None:
            self.value_entry.focus_set()
            self.value_entry.selection_range(0, 'end')
        return 'break'

    def apply_value(self):
        if self.selected_index is None:
            return True
        try:
            raw = self.current_value.get().strip()
            value = self.parse_money(raw) if raw else None
        except ValueError as exc:
            messagebox.showerror('Valor atual', str(exc), parent=self.root)
            return False
        self.record['items'][self.selected_index]['current_price_cents'] = value
        self.render()
        return True

    def save(self):
        if not self.apply_value():
            return
        try:
            self.on_save(self.record)
        except Exception as exc:
            messagebox.showerror('Salvar valores', f'Não foi possível salvar os valores:\n{exc}', parent=self.root)
            return
        self.root.destroy()

    def close(self):
        changed = json.dumps(self.record, sort_keys=True) != self.original
        if self.selected_index is not None:
            value = self.record['items'][self.selected_index].get('current_price_cents')
            changed = changed or self.current_value.get().strip() != (self.money_input(value) if value is not None else '')
        if changed and not messagebox.askyesno('Descartar alterações', 'Fechar sem salvar os valores atuais?', parent=self.root):
            return
        self.root.destroy()
