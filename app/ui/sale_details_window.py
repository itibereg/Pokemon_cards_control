from __future__ import annotations

from pathlib import Path

from datetime import datetime
import tkinter as tk

import ttkbootstrap as tb
from ttkbootstrap.constants import BOTH, CENTER, E, END, LEFT, RIGHT, W, X
from ttkbootstrap.tableview import Tableview

from app.models import calculate_totals, format_brl, item_is_trade, display_item_extras, trade_summary
from app.sales_store import SavedSale


# VERSAO_ATUALIZADA: SALE_DETAILS_WINDOW_V92_2026_09_28


class SaleDetailsWindow:
    """Janela somente de leitura com todos os dados de uma venda salva."""

    def __init__(self, parent: tk.Misc, sale: SavedSale) -> None:
        self.parent = parent
        self.sale = sale
        self.item_by_id = {str(item.id): item for item in sale.items}

        self.root = tb.Toplevel(parent)
        self.root.withdraw()
        self.root.title(f"Detalhes da venda · {sale.title}")
        self.root.minsize(980, 620)
        self.root.transient(parent)

        self._configure_style()
        self._configure_geometry()
        self._build_ui()
        self._show()

    def _configure_style(self) -> None:
        style = tb.Style()
        style.configure("SaleDetailsTitle.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("SaleDetailsSection.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("SaleDetailsMetric.TLabel", font=("Segoe UI", 12, "bold"))
        style.configure("SaleDetailsCaption.TLabel", font=("Segoe UI", 8))
        style.configure("Treeview", rowheight=28, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

    def _configure_geometry(self) -> None:
        try:
            self.parent.update_idletasks()
            parent_x = self.parent.winfo_rootx()
            parent_y = self.parent.winfo_rooty()
            parent_w = max(self.parent.winfo_width(), 1000)
            parent_h = max(self.parent.winfo_height(), 650)

            width = min(max(1080, int(parent_w * 0.82)), 1320)
            height = min(max(680, int(parent_h * 0.88)), 860)

            x = parent_x + max((parent_w - width) // 2, 0)
            y = parent_y + max((parent_h - height) // 2, 0)
            self.root.geometry(f"{width}x{height}+{x}+{y}")
        except tk.TclError:
            self.root.geometry("1080x720")

    def _show(self) -> None:
        self.root.update_idletasks()
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass

    @staticmethod
    def _display_status(value: str) -> str:
        return "Separado" if value == "Liberado" else (value or "-")

    @staticmethod
    def _format_datetime(value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            return "-"
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(raw[:19], fmt).strftime("%d/%m/%Y %H:%M")
            except ValueError:
                continue
        return raw

    def _build_ui(self) -> None:
        container = tb.Frame(self.root, padding=(18, 14))
        container.pack(fill=BOTH, expand=True)

        self._build_header(container)
        self._build_overview(container)
        self._build_items(container)
        self._build_notes(container)

    def _build_header(self, parent: tb.Frame) -> None:
        header = tb.Frame(parent)
        header.pack(fill=X, pady=(0, 12))

        title_box = tb.Frame(header)
        title_box.pack(side=LEFT, fill=X, expand=True)

        tb.Label(
            title_box,
            text=self.sale.title,
            style="SaleDetailsTitle.TLabel",
        ).pack(anchor=W)

        subtitle_parts = []
        if self.sale.customer_name:
            subtitle_parts.append(self.sale.customer_name)
        if self.sale.customer_phone:
            subtitle_parts.append(self.sale.customer_phone)

        tb.Label(
            title_box,
            text=" · ".join(subtitle_parts) or "Cliente não informado",
            bootstyle="secondary",
            font=("Segoe UI", 9),
        ).pack(anchor=W, pady=(2, 0))

        status_style = {
            "Aberta": "primary",
            "Liberado": "warning",
            "Finalizada": "success",
            "Cancelada": "danger",
        }.get(self.sale.status, "secondary")

        tb.Label(
            header,
            text=self._display_status(self.sale.status),
            bootstyle=status_style,
            font=("Segoe UI", 10, "bold"),
            padding=(10, 5),
        ).pack(side=RIGHT)

    def _build_overview(self, parent: tb.Frame) -> None:
        overview = tb.Frame(parent)
        overview.pack(fill=X, pady=(0, 12))

        left = tb.Labelframe(
            overview,
            text=" Dados da venda ",
            padding=(12, 10),
            bootstyle="secondary",
        )
        left.pack(side=LEFT, fill=BOTH, expand=True, padx=(0, 10))

        info = (
            ("Cliente", self.sale.customer_name or "-"),
            ("Telefone", self.sale.customer_phone or "-"),
            ("Status", self._display_status(self.sale.status)),
            ("Pagamento", self.sale.payment_status or "Pendente"),
            ("Pago em", self._format_datetime(self.sale.paid_at)),
            ("Entrega", self._format_datetime(self.sale.completed_at)),
            ("Local da entrega", self.sale.completed_location or "-"),
            ("Criada em", self._format_datetime(self.sale.created_at)),
            ("Última atualização", self._format_datetime(self.sale.updated_at)),
        )

        for row, (label, value) in enumerate(info):
            tb.Label(
                left,
                text=f"{label}:",
                font=("Segoe UI", 8, "bold"),
                bootstyle="secondary",
            ).grid(row=row, column=0, sticky=W, padx=(0, 8), pady=2)

            tb.Label(
                left,
                text=value,
                font=("Segoe UI", 9),
            ).grid(row=row, column=1, sticky=W, pady=2)

        left.grid_columnconfigure(1, weight=1)

        totals = calculate_totals(self.sale.items)
        # Mantém a contagem de todos os itens, mas cobra apenas os encontrados.
        billable_items = [
            item
            for item in self.sale.items
            if str(getattr(item, "separation_status", "") or "").strip().casefold()
            not in {"não encontrado", "nao encontrado"}
        ]
        billable_totals = calculate_totals(billable_items)

        metrics = tb.Frame(overview)
        metrics.pack(side=RIGHT, fill="y")

        cards, trades, receivable = trade_summary(self.sale.items)
        metric_data = (
            ("Itens / Unidades", f"{totals.item_count} / {totals.unit_count}"),
            ("Valor das cartas", format_brl(cards)),
            ("Abatimento por trocas", format_brl(trades)),
            ("Total a receber", format_brl(receivable)),
        )

        for index, (caption, value) in enumerate(metric_data):
            card = tb.Frame(
                metrics,
                padding=(12, 10),
                bootstyle="secondary",
                width=170,
                height=72,
            )
            card.pack(fill=X, pady=(0, 7 if index < len(metric_data) - 1 else 0))
            card.pack_propagate(False)

            tb.Label(
                card,
                text=value,
                style="SaleDetailsMetric.TLabel",
                bootstyle="inverse-secondary",
            ).pack(anchor=W)

            tb.Label(
                card,
                text=caption,
                style="SaleDetailsCaption.TLabel",
                bootstyle="inverse-secondary",
            ).pack(anchor=W, pady=(2, 0))

    def _build_items(self, parent: tb.Frame) -> None:
        section = tb.Labelframe(
            parent,
            text=" Itens da venda ",
            padding=(10, 8),
            bootstyle="primary",
        )
        section.pack(fill=BOTH, expand=True, pady=(0, 10))

        columns = [
            {"text": "Carta", "stretch": True, "width": 250},
            {"text": "Nº", "stretch": False, "width": 80},
            {"text": "Coleção", "stretch": False, "width": 85},
            {"text": "Extras", "stretch": False, "width": 110},
            {"text": "Idioma", "stretch": False, "width": 70},
            {"text": "Situação", "stretch": False, "width": 120},
            {"text": "Qtd", "stretch": False, "width": 60},
            {"text": "Preço Unit.", "stretch": False, "width": 105},
            {"text": "Total", "stretch": False, "width": 105},
        ]

        self.table = Tableview(
            master=section,
            coldata=columns,
            rowdata=[],
            paginated=False,
            searchable=False,
            bootstyle="primary",
            stripecolor=("white", "#f8f9fa"),
        )
        self.table.pack(fill=BOTH, expand=True)

        tree = self.table.view
        self.tree = tree
        tree.configure(selectmode="browse")

        for index, column in enumerate(tree["columns"]):
            anchor = E if index in {7, 8} else CENTER if index in {1, 2, 4, 5, 6} else W
            tree.column(column, anchor=anchor)
            tree.heading(column, anchor=CENTER)

        tree.tag_configure(
            "Separado",
            background="#EAF8EE",
            foreground="#25613A",
        )
        tree.tag_configure(
            "Não encontrado",
            background="#FDECEC",
            foreground="#8B2F2F",
        )

        tree.tag_configure("item_trade", background="#EDE7F6", foreground="#512DA8")
        for item in self.sale.items:
            separation_status = getattr(item, "separation_status", "") or "Pendente"
            tree.insert(
                "",
                END,
                iid=str(item.id),
                values=(
                    item.card.display_name,
                    item.card.card_number,
                    item.card.collection_number,
                    display_item_extras(item),
                    self._language_cell_text(getattr(item, "language", "") or "PT"),
                    separation_status,
                    item.quantity,
                    format_brl(item.unit_price),
                    format_brl(item.net_total),
                ),
                tags=("item_trade",) if item_is_trade(item) else (
                    (separation_status,) if separation_status in {"Separado", "Não encontrado"} else ()),
            )

        billable_totals = calculate_totals([
            item
            for item in self.sale.items
            if str(getattr(item, "separation_status", "") or "").strip().casefold()
            not in {"não encontrado", "nao encontrado"}
        ])
        tree.tag_configure("sale_total", font=("Segoe UI", 9, "bold"))
        tree.insert(
            "",
            END,
            values=("Total da venda", "", "", "", "", "", sum(item.quantity for item in self.sale.items), "",
                    format_brl(billable_totals.net_total)),
            tags=("sale_total",),
        )

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
            return "break"
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
                image = tk.PhotoImage(master=self.root, file=str(path)) if path else None
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
                item = self.item_by_id.get(str(sale_item_id))
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
            self.root.after(120, self._refresh_language_flags)
        except tk.TclError:
            return

    def _build_notes(self, parent: tb.Frame) -> None:
        box = tb.Labelframe(
            parent,
            text=" Observações ",
            padding=(10, 7),
            bootstyle="secondary",
        )
        box.pack(fill=X)

        text = tk.Text(
            box,
            height=3,
            wrap="word",
            font=("Segoe UI", 9),
            relief="flat",
            background="#f8f9fa",
        )
        text.pack(fill=X)
        text.insert("1.0", self.sale.notes or "Sem observações.")
        text.configure(state="disabled")
