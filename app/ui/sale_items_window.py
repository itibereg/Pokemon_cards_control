from __future__ import annotations

from pathlib import Path

import tkinter as tk
from typing import TYPE_CHECKING

import ttkbootstrap as tb
from ttkbootstrap.tableview import Tableview

from app.models import calculate_totals, format_brl, item_is_trade, display_item_extras, trade_summary

if TYPE_CHECKING:
    from app.ui.sales_window import SalesWindow


class SaleItemsWindow:
    """Visualização ampliada da venda atual, usando os mesmos objetos da tela principal."""

    COLUMNS = (
        ("name", "Carta", 330, "w"),
        ("number", "Nº Carta", 90, "center"),
        ("collection", "Coleção", 90, "center"),
        ("extras", "Extras", 160, "w"),
        ("language", "Idioma", 75, "center"),
        ("status", "Situação", 145, "center"),
        ("quantity", "Qtd", 70, "center"),
        ("price", "Preço Unit.", 125, "e"),
        ("total", "Total", 135, "e"),
    )

    def __init__(self, owner: SalesWindow) -> None:
        self.owner = owner
        self.sort_column: str | None = None
        self.sort_reverse = False
        self._item_to_iid: dict[str, str] = {}
        self._iid_to_item: dict[str, str] = {}
        self.window = tk.Toplevel(owner.root)
        self.window.withdraw()
        self.window.transient(owner.root)
        self.window.title("Itens da venda — visualização ampliada")
        self._position_window()
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Escape>", lambda _event: self.close())

        container = tb.Frame(self.window, padding=18)
        container.pack(fill="both", expand=True)
        header = tb.Frame(container)
        header.pack(fill="x", pady=(0, 12))
        title_box = tb.Frame(header)
        title_box.pack(side="left", fill="x", expand=True)
        self.title_label = tb.Label(title_box, text="Itens da venda", font=("Segoe UI", 18, "bold"))
        self.title_label.pack(anchor="w")
        tb.Label(title_box, text="Clique no cabeçalho para ordenar. Selecione uma carta para editar na venda.",
                 bootstyle="secondary").pack(anchor="w", pady=(3, 0))
        self.edit_button = tb.Button(header, text="Editar na venda", bootstyle="primary",
                                     command=self._edit_selected, state="disabled")
        self.edit_button.pack(side="right", padx=(8, 0))
        tb.Button(header, text="Fechar", bootstyle="secondary-outline", command=self.close).pack(side="right")

        table = tb.Labelframe(container, text=" Itens da venda ", padding=10, bootstyle="primary")
        table.pack(fill="both", expand=True)
        self.table = Tableview(
            master=table,
            coldata=[{"text": caption, "width": width, "minwidth": width,
                      "stretch": key in {"name", "extras"}, "anchor": anchor}
                     for key, caption, width, anchor in self.COLUMNS],
            rowdata=[],
            paginated=False,
            searchable=False,
            bootstyle="primary",
            # Tableview recebe (cor de fundo, cor do texto), não dois fundos.
            stripecolor=("#f8f9fa", "#212529"),
        )
        self.table.pack(fill="both", expand=True)
        self.tree = self.table.view
        # Usa o mesmo estilo efetivo da tabela principal, sem substituir o tema.
        self.tree.configure(selectmode="extended")
        main_style = owner.tree.cget("style")
        if main_style:
            self.tree.configure(style=main_style)
        for column, (key, caption, width, anchor) in zip(self.tree["columns"], self.COLUMNS):
            self.tree.heading(column, text=caption, anchor="center")
            self.tree.column(column, width=width, minwidth=width, anchor=anchor,
                             stretch=key in {"name", "extras"})
        # Ordenação usa os valores numéricos dos itens, não o texto em reais.
        self.tree.bind("<Button-1>", self._on_table_click)
        self.tree.tag_configure("item_separated", background="#E6F4EA", foreground="#1E4620")
        self.tree.tag_configure("item_not_found", background="#FDECEC", foreground="#8A1C1C")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Return>", self._edit_selected)
        self.tree.bind("<F2>", self._edit_selected)
        self.empty_label = tb.Label(table, text="Esta venda ainda não tem itens.", bootstyle="secondary",
                                    font=("Segoe UI", 12))

        footer = tb.Frame(container)
        footer.pack(fill="x", pady=(12, 0))
        self.summary_label = tb.Label(footer, bootstyle="secondary")
        self.summary_label.pack(side="left")
        self.total_label = tb.Label(footer, font=("Segoe UI", 12, "bold"), bootstyle="success")
        self.total_label.pack(side="right")
        self.refresh()
        self._language_flag_widgets = []
        self._language_flag_images = {}
        self._refresh_language_flags()

    @staticmethod
    def _language_display(value: str) -> str:
        code = "".join(str(value or "").strip().upper().split())
        return code

    @classmethod
    def _language_cell_text(cls, value: str) -> str:
        code = cls._language_display(value)
        return "" if code in {"PT", "EN"} else code

    def _forward_language_flag_event(self, event, sequence):
        canvas = event.widget
        x = canvas.winfo_rootx() - self.tree.winfo_rootx() + event.x
        y = canvas.winfo_rooty() - self.tree.winfo_rooty() + event.y
        if sequence == "<Double-Button-1>":
            event.x, event.y = x, y
            return self._on_double_click(event)
        options = dict(x=x, y=y, state=event.state)
        if sequence == "<MouseWheel>":
            options["delta"] = event.delta
        self.tree.event_generate(sequence, **options)
        return "break"

    def _draw_language_flag(self, canvas, code):
        """PNG original de 24 px; mantém referência para o Tk não descartar a imagem."""
        if code not in self._language_flag_images:
            name = {"PT": "brasil.png", "EN": "eua.png"}.get(code)
            path = Path(__file__).resolve().parent / "flags" / name if name else None
            try:
                image = tk.PhotoImage(master=self.window, file=str(path)) if path else None
            except tk.TclError:
                image = None
            self._language_flag_images[code] = image
        canvas.delete("all")
        image = self._language_flag_images.get(code)
        if image is not None:
            canvas.create_image(12, 12, image=image, anchor="center")

    def _refresh_language_flags(self):
        """Desenha bandeiras reais sobre as células visíveis, junto ao código do idioma."""
        try:
            if not self.tree.winfo_exists():
                return
            columns = self.tree["columns"]
            used = 0
            for sale_item_id in self.tree.get_children():
                item = self.owner.item_by_id.get(self._iid_to_item.get(str(sale_item_id), ""))
                code = self._language_display(getattr(item, "language", "") or "PT") if item else ""
                if code not in {"PT", "EN"}:
                    continue
                bounds = self.tree.bbox(sale_item_id, columns[4])
                if not bounds:
                    continue
                x, y, width, height = bounds
                flag_x = x + (width - 24) // 2
                flag_y = y + (height - 24) // 2
                if flag_x < 0 or flag_x + 24 > self.tree.winfo_width() or flag_y < 0:
                    continue
                if self.tree.identify_region(flag_x + 11, flag_y + 12) != "cell":
                    continue
                if used == len(self._language_flag_widgets):
                    canvas = tk.Canvas(self.tree, width=24, height=24, highlightthickness=0,
                                       borderwidth=0, takefocus=False)
                    for sequence in ("<Button-1>", "<ButtonRelease-1>", "<Double-Button-1>",
                                     "<Button-3>", "<ButtonRelease-3>", "<MouseWheel>",
                                     "<Button-4>", "<Button-5>"):
                        canvas.bind(sequence, lambda event, seq=sequence: self._forward_language_flag_event(event, seq))
                    canvas._flag_code = None
                    self._language_flag_widgets.append(canvas)
                canvas = self._language_flag_widgets[used]
                if canvas._flag_code != code:
                    self._draw_language_flag(canvas, code)
                    canvas._flag_code = code
                style = tb.Style()
                tree_style = self.tree.cget("style") or "Treeview"
                background = style.lookup(tree_style, "background") or "white"
                if sale_item_id in self.tree.selection():
                    background = style.lookup(tree_style, "background", ("selected",)) or background
                else:
                    for tag in self.tree.item(sale_item_id, "tags"):
                        tag_background = self.tree.tag_configure(tag, "background")
                        if tag_background:
                            background = tag_background
                            break
                canvas.configure(background=background)
                if self._language_flag_images.get(code) is not None:
                    canvas.place(x=flag_x, y=flag_y, width=24, height=24)
                else:
                    canvas.place_forget()
                canvas.tk.call("raise", str(canvas))
                used += 1
            for canvas in self._language_flag_widgets[used:]:
                canvas.place_forget()
            self.window.after(120, self._refresh_language_flags)
        except tk.TclError:
            return

    def _position_window(self) -> None:
        """Centraliza na área útil, reservando 100 px abaixo da janela."""
        left, top = 0, 0
        right = self.window.winfo_screenwidth()
        bottom = self.window.winfo_screenheight()
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
        self.window.geometry(f"{width}x{height}{x:+d}{y:+d}")
        self.window.minsize(min(880, width), min(450, height))

    def show(self, item_id: str | None = None) -> None:
        self.refresh()
        iid = self._item_to_iid.get(item_id)
        if iid and self.tree.exists(iid):
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.tree.see(iid)
        self.window.deiconify()
        self.window.lift()
        self.tree.focus_set()
        self._on_select()

    def refresh(self) -> None:
        if not self.window.winfo_exists():
            return
        selected = [self._iid_to_item.get(iid) for iid in self.tree.selection()]
        scroll = self.tree.yview()
        title = self.owner.sale_title_var.get().strip() or "Venda atual"
        self.window.title(f"Itens da venda — {title}")
        self.title_label.configure(text=title)
        items = list(self.owner.items)
        if self.sort_column:
            items.sort(key=lambda item: self._sort_value(item, self.sort_column), reverse=self.sort_reverse)
        # Mantém o conjunto de dados do Tableview e a árvore sincronizados.
        self.table.delete_rows()
        self._item_to_iid.clear()
        self._iid_to_item.clear()
        totals = calculate_totals(items)
        # Mantém todos os itens na lista e na contagem, mas exclui os não encontrados do valor.
        billable_items = [
            item
            for item in items
            if str(getattr(item, "separation_status", "") or "").strip().casefold()
            not in {"não encontrado", "nao encontrado"}
        ]
        billable_totals = calculate_totals(billable_items)
        records = [(item, self.table.insert_row("end", self._row_values(item))) for item in items]
        total_row = self.table.insert_row(
            "end",
            ("Total da venda", "", "", "", "", "", totals.unit_count, "",
             format_brl(billable_totals.net_total)),
        )
        self.table.load_table_data()
        self.tree.tag_configure("item_trade", background="#EDE7F6", foreground="#512DA8")
        self.tree.tag_configure("sale_total", font=("Segoe UI", 9, "bold"))
        self.tree.item(total_row.iid, tags=("sale_total",))
        for item, row in records:
            iid = row.iid
            self._item_to_iid[item.id] = iid
            self._iid_to_item[iid] = item.id
            status_tags = ("item_trade",) if item_is_trade(item) else self.owner._item_status_tag(item.separation_status)
            # Preserva o zebrado do Tableview em itens pendentes; a situação
            # tem prioridade de cor nas linhas separadas/não encontradas.
            tags = status_tags or self.tree.item(iid, "tags")
            self.tree.item(iid, tags=tags)
        restored = [self._item_to_iid[item_id] for item_id in selected if item_id in self._item_to_iid]
        if restored:
            self.tree.selection_set(restored)
        if scroll:
            self.tree.yview_moveto(scroll[0])
        if items:
            self.empty_label.place_forget()
        else:
            self.empty_label.place(relx=0.5, rely=0.5, anchor="center")
        self.summary_label.configure(text=f"{totals.item_count} item(ns) · {totals.unit_count} unidade(s)")
        cards, trades, receivable = trade_summary(items)
        self.summary_label.configure(text=(
            f"{totals.item_count} item(ns) · {totals.unit_count} unidade(s)\n"
            f"Valor das cartas: {format_brl(cards)} · Abatimento por trocas: {format_brl(trades)}"
        ))
        self.total_label.configure(text=f"A receber: {format_brl(receivable)}")
        self._on_select()

    @staticmethod
    def _row_values(item) -> tuple:
        return (item.card.display_name, item.card.card_number, item.card.collection_number,
                display_item_extras(item), SaleItemsWindow._language_cell_text(item.language or "PT"), item.separation_status or "Pendente",
                item.quantity, format_brl(item.unit_price), format_brl(item.net_total))

    @classmethod
    def _sort_value(cls, item, column: str):
        if column == "quantity":
            return item.quantity
        if column == "price":
            return item.unit_price
        if column == "total":
            return item.net_total
        index = next(i for i, spec in enumerate(cls.COLUMNS) if spec[0] == column)
        return str(cls._row_values(item)[index]).casefold()

    def _on_table_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "heading":
            return None
        column = self.tree.identify_column(event.x)
        displayed = self.tree["displaycolumns"]
        columns = self.tree["columns"] if displayed in ("#all", ("#all",)) else displayed
        try:
            cid = columns[int(column.lstrip("#")) - 1]
            index = list(self.tree["columns"]).index(str(cid))
            self._sort_by(self.COLUMNS[index][0])
        except (ValueError, IndexError):
            pass
        return "break"

    def _sort_by(self, column: str) -> None:
        self.sort_reverse = not self.sort_reverse if self.sort_column == column else False
        self.sort_column = column
        self.refresh()

    def _on_select(self, _event=None) -> None:
        selected = self.tree.selection()
        valid = len(selected) == 1 and self._iid_to_item.get(selected[0]) in self.owner.item_by_id
        self.edit_button.configure(state="normal" if valid else "disabled")

    def _on_double_click(self, event) -> str:
        if self.tree.identify_region(event.x, event.y) == "cell":
            iid = self.tree.identify_row(event.y)
            if iid:
                self.tree.selection_set(iid)
                self._edit_selected()
        return "break"

    def _edit_selected(self, _event=None) -> str:
        selected = self.tree.selection()
        if len(selected) != 1 or self._iid_to_item.get(selected[0]) not in self.owner.item_by_id:
            return "break"
        item_id = self._iid_to_item[selected[0]]
        self.window.withdraw()
        self.owner.root.lift()
        self.owner._begin_item_edit(item_id)
        if self.owner.selected_item_id == item_id:
            self.close()
            self.owner.quantity_entry.focus_set()
        else:
            # Se o usuário cancelar a troca de um rascunho, mantém esta janela.
            self.window.deiconify()
            self.window.lift()
        return "break"

    def close(self) -> None:
        if getattr(self.owner, "_sale_items_window", None) is self:
            self.owner._sale_items_window = None
        self.window.destroy()
