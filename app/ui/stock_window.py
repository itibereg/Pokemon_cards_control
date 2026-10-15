from __future__ import annotations

from decimal import Decimal
import hashlib
import os
from io import BytesIO
from pathlib import Path
import threading
import tkinter as tk

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import ttkbootstrap as tb
from PIL import Image, ImageTk
from ttkbootstrap.constants import BOTH, CENTER, E, END, LEFT, RIGHT, W, X
from ttkbootstrap.tableview import Tableview
from tkinter import filedialog, messagebox, simpledialog

from app.stock_store import StockStore


# VERSAO_ATUALIZADA: POKEMON_API_IMAGE_CACHE_V76_2026_09_28


class StockWindow:
    """Estoque local com layout operacional, filtros rápidos e métricas."""

    FILTERS = (
        ("Todos", "Todos"),
        ("Disponível", "Disponíveis"),
        ("Sem estoque", "Sem estoque"),
    )

    def __init__(self, root: tk.Toplevel) -> None:
        self.root = root
        self.root.withdraw()
        self.root.title("Estoque")
        self.root.minsize(1120, 650)

        self.search_var = tb.StringVar()
        self.status_var = tb.StringVar(value="Todos")
        self.store = StockStore()
        self._filter_after_id: str | None = None

        self.filter_buttons: dict[str, tb.Button] = {}
        self.metric_labels: dict[str, tb.Label] = {}
        self._detail_photo: ImageTk.PhotoImage | None = None
        self._detail_item_id: int | None = None
        self._image_request_id = 0
        self._image_cache: dict[str, Image.Image] = {}
        self._bulk_image_download_running = False
        self._bulk_image_download_cancelled = False
        self._zoom_window: tk.Toplevel | None = None
        self._zoom_photo: ImageTk.PhotoImage | None = None
        self._current_full_image: Image.Image | None = None

        # Cache persistente das imagens baixadas da Pokémon TCG API.
        # Depois da primeira consulta, a imagem é aberta diretamente do disco.
        self.image_cache_dir = Path("data") / "card_image_cache"
        self.image_cache_dir.mkdir(parents=True, exist_ok=True)
        self.pokemon_api_base_url = "https://api.pokemontcg.io/v2"
        self.pokemon_api_session = requests.Session()
        self.pokemon_api_session.headers.update(
            {"User-Agent": "pokemon-cards-stock/1.0"}
        )
        retry_policy = Retry(
            total=3,
            connect=2,
            read=2,
            status=3,
            backoff_factor=0.6,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_policy)
        self.pokemon_api_session.mount("https://", adapter)
        self.pokemon_api_session.mount("http://", adapter)

        api_key = os.getenv("POKEMON_TCG_API_KEY", "").strip()
        if api_key:
            self.pokemon_api_session.headers.update({"X-Api-Key": api_key})

        self._configure_style()
        self._configure_geometry()
        self._build_ui()
        self._bind_events()
        self._refresh_table()
        self._show_window()

    def _configure_style(self) -> None:
        style = tb.Style()
        style.configure("StockTitle.TLabel", font=("Segoe UI", 17, "bold"))
        style.configure("StockSubtitle.TLabel", font=("Segoe UI", 9))
        style.configure("StockMetricValue.TLabel", font=("Segoe UI", 15, "bold"))
        style.configure("StockMetricCaption.TLabel", font=("Segoe UI", 8))
        style.configure("StockSection.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("StockSelected.TLabel", font=("Segoe UI", 9, "bold"))
        style.configure("Treeview", rowheight=30, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

    def _get_work_area(self) -> tuple[int, int, int, int]:
        width = self.root.winfo_screenwidth()
        height = self.root.winfo_screenheight()
        try:
            import ctypes
            from ctypes import wintypes

            rect = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                return rect.left, rect.top, rect.right, rect.bottom
        except Exception:
            pass
        return 0, 0, width, height

    def _configure_geometry(self) -> None:
        left, top, right, bottom = self._get_work_area()
        work_w = max(1, right - left)
        work_h = max(1, bottom - top)
        width = min(max(1220, int(work_w * 0.90)), work_w - 36)
        height = min(max(700, int(work_h * 0.90)), work_h - 36)
        x = left + max(0, (work_w - width) // 2)
        y = top + max(0, (work_h - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _show_window(self) -> None:
        self.root.protocol("WM_DELETE_WINDOW", self._close_window)
        self.root.update_idletasks()
        self.root.deiconify()
        self.root.lift()
        self.root.after_idle(self._fit_stock_columns_to_table)
        self.search_entry.focus_set()

    def _close_window(self) -> None:
        self._bulk_image_download_cancelled = True
        self._bulk_image_download_running = False
        self._hide_image_zoom()
        try:
            if self.root.winfo_exists():
                self.root.destroy()
        except tk.TclError:
            pass

    @staticmethod
    def _format_brl_decimal(value: Decimal) -> str:
        value = value.quantize(Decimal("0.01"))
        integer, cents = f"{value:.2f}".split(".")
        groups = []
        while integer:
            groups.append(integer[-3:])
            integer = integer[:-3]
        return f"R$ {'.'.join(reversed(groups))},{cents}"

    def _build_ui(self) -> None:
        container = tb.Frame(self.root, padding=(16, 12))
        container.pack(fill=BOTH, expand=True)

        self._build_header(container)
        self._build_metrics(container)
        self._build_search_and_filters(container)
        self._build_table(container)
        self._build_footer(container)

    def _build_header(self, parent: tb.Frame) -> None:
        header = tb.Frame(parent)
        header.pack(fill=X, pady=(0, 12))

        title_box = tb.Frame(header)
        title_box.pack(side=LEFT, fill=X, expand=True)

        tb.Label(
            title_box,
            text="Estoque",
            style="StockTitle.TLabel",
        ).pack(anchor=W)

        tb.Label(
            title_box,
            text="Inventário, disponibilidade e preços das cartas",
            style="StockSubtitle.TLabel",
            bootstyle="secondary",
        ).pack(anchor=W, pady=(1, 0))

        self.import_button = tb.Button(
            header,
            text="Importar da Liga",
            bootstyle="success",
            command=self._import_from_liga,
            width=17,
        )
        self.import_button.pack(side=RIGHT)

        self.download_images_button = tb.Button(
            header,
            text="Baixar imagens",
            bootstyle="primary-outline",
            command=self._start_bulk_image_download,
            width=16,
        )
        self.download_images_button.pack(side=RIGHT, padx=(0, 8))

    def _build_metrics(self, parent: tb.Frame) -> None:
        metrics = tb.Frame(parent)
        metrics.pack(fill=X, pady=(0, 12))

        definitions = (
            ("variants", "Variantes"),
            ("units", "Unidades"),
            ("available", "Disponíveis"),
            ("value", "Valor do estoque"),
        )

        for index, (key, caption) in enumerate(definitions):
            card = tb.Frame(metrics, padding=(12, 9), bootstyle="secondary")
            card.pack(
                side=LEFT,
                fill=X,
                expand=True,
                padx=(0, 7 if index < len(definitions) - 1 else 0),
            )

            value_label = tb.Label(
                card,
                text="0",
                style="StockMetricValue.TLabel",
                bootstyle="inverse-secondary",
            )
            value_label.pack(anchor=W)

            tb.Label(
                card,
                text=caption,
                style="StockMetricCaption.TLabel",
                bootstyle="inverse-secondary",
            ).pack(anchor=W, pady=(1, 0))

            self.metric_labels[key] = value_label

    def _build_search_and_filters(self, parent: tb.Frame) -> None:
        search_area = tb.Frame(parent)
        search_area.pack(fill=X, pady=(0, 9))

        search_box = tb.Frame(search_area)
        search_box.pack(side=LEFT, fill=X, expand=True, padx=(0, 10))

        tb.Label(
            search_box,
            text="Buscar carta, número ou coleção",
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor=W, pady=(0, 4))

        search_row = tb.Frame(search_box)
        search_row.pack(fill=X)

        self.search_entry = tb.Entry(
            search_row,
            textvariable=self.search_var,
            font=("Segoe UI", 10),
        )
        self.search_entry.pack(side=LEFT, fill=X, expand=True)

        self.clear_button = tb.Button(
            search_row,
            text="Limpar",
            bootstyle="secondary-outline",
            command=self._clear_filters,
            width=8,
        )
        self.clear_button.pack(side=LEFT, padx=(6, 0))

        filters_box = tb.Frame(search_area)
        filters_box.pack(side=RIGHT)

        tb.Label(
            filters_box,
            text="Status",
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor=W, pady=(0, 4))

        filter_row = tb.Frame(filters_box)
        filter_row.pack(fill=X)

        for status, caption in self.FILTERS:
            button = tb.Button(
                filter_row,
                text=caption,
                command=lambda value=status: self._set_status_filter(value),
                padding=(9, 5),
            )
            button.pack(side=LEFT, padx=(0, 5 if status != "Sem estoque" else 0))
            self.filter_buttons[status] = button

        self._update_filter_button_styles()

    def _build_selection_actions(self, parent: tb.Frame) -> None:
        bar = tb.Frame(parent, padding=(10, 7), bootstyle="secondary")
        bar.pack(fill=X, pady=(0, 8))

        self.selection_label = tb.Label(
            bar,
            text="Nenhuma carta selecionada",
            style="StockSelected.TLabel",
            bootstyle="inverse-secondary",
        )
        self.selection_label.pack(side=LEFT, fill=X, expand=True)

        self.price_button = tb.Button(
            bar,
            text="Definir preço",
            bootstyle="info-outline",
            command=self._edit_selected_price,
            state="disabled",
            width=13,
        )
        self.price_button.pack(side=RIGHT)

    def _build_table(self, parent: tb.Frame) -> None:
        """Corpo principal em duas colunas: estoque à esquerda e detalhes à direita."""
        body = tb.Frame(parent)
        body.pack(fill=BOTH, expand=True)

        # A tabela ocupa aproximadamente 2/3 e o painel de detalhes 1/3.
        # Ao redimensionar a janela, o espaço extra é distribuído entre os painéis,
        # mas as colunas da tabela mantêm larguras fixas.
        body.columnconfigure(0, weight=67, minsize=760)
        body.columnconfigure(1, weight=33, minsize=430)
        body.rowconfigure(0, weight=1)

        section = tb.Frame(body)
        section.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        section.rowconfigure(1, weight=1)
        section.columnconfigure(0, weight=1)

        section_header = tb.Frame(section)
        section_header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        section_header.columnconfigure(0, weight=1)

        tb.Label(
            section_header,
            text="Itens em estoque",
            style="StockSection.TLabel",
        ).grid(row=0, column=0, sticky="w")

        self.table_summary_label = tb.Label(
            section_header,
            text="0 itens",
            bootstyle="secondary",
            font=("Segoe UI", 8),
        )
        self.table_summary_label.grid(row=0, column=1, sticky="e")

        # Larguras-base. Carta, Coleção e Extras recebem dinamicamente o espaço
        # restante para que as colunas preencham exatamente a largura da tabela.
        columns = [
            {"text": "Carta", "stretch": False, "width": 185},
            {"text": "Nº", "stretch": False, "width": 58},
            {"text": "Coleção", "stretch": False, "width": 92},
            {"text": "Extras", "stretch": False, "width": 88},
            {"text": "Idioma", "stretch": False, "width": 58},
            {"text": "Situação", "stretch": False, "width": 88},
            {"text": "Qtd", "stretch": False, "width": 48},
            {"text": "Preço", "stretch": False, "width": 86},
            {"text": "Total", "stretch": False, "width": 86},
        ]

        self._stock_column_base_widths = [185, 58, 92, 88, 58, 88, 48, 86, 86]
        self._stock_flexible_columns = {
            0: 0.50,
            2: 0.28,
            3: 0.22,
        }

        table_host = tb.Frame(section)
        table_host.grid(row=1, column=0, sticky="nsew")
        table_host.rowconfigure(0, weight=1)
        table_host.columnconfigure(0, weight=1)

        self.table = Tableview(
            master=table_host,
            coldata=columns,
            rowdata=[],
            paginated=False,
            searchable=False,
            bootstyle="secondary",
            stripecolor=("white", "#fafafa"),
        )
        self.table.grid(row=0, column=0, sticky="nsew")

        self.tree = self.table.view
        self.tree.configure(selectmode="browse")

        for index, column in enumerate(self.tree["columns"]):
            anchor = E if index in {7, 8} else CENTER if index in {1, 4, 5, 6} else W
            width = columns[index]["width"]
            self.tree.column(
                column,
                anchor=anchor,
                width=width,
                minwidth=max(40, width - 15),
                stretch=False,
            )
            self.tree.heading(column, anchor=CENTER)

        self.tree.tag_configure("available", foreground="#235C35")
        self.tree.tag_configure("empty", foreground="#8B2F2F")

        self._build_details_panel(body)

    def _build_details_panel(self, parent: tb.Frame) -> None:
        """Painel lateral redesenhado para aproveitar melhor o espaço disponível."""
        self.details_panel = tb.Frame(parent, padding=(12, 10))
        self.details_panel.grid(row=0, column=1, sticky="nsew")
        self.details_panel.columnconfigure(0, weight=1)
        self.details_panel.rowconfigure(5, weight=1)

        # Cabeçalho e ação principal.
        top = tb.Frame(self.details_panel)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        top.columnconfigure(0, weight=1)

        tb.Label(
            top,
            text="Detalhes da carta",
            style="StockSection.TLabel",
        ).grid(row=0, column=0, sticky="w")

        self.detail_status = tb.Label(
            top,
            text="",
            bootstyle="secondary",
            font=("Segoe UI", 8, "bold"),
        )
        self.detail_status.grid(row=0, column=1, sticky="e")

        # Resumo principal com imagem fixa e texto flexível.
        overview = tb.Frame(self.details_panel)
        overview.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        overview.columnconfigure(1, weight=1)

        self.image_frame = tb.Frame(
            overview,
            width=135,
            height=188,
            bootstyle="secondary",
        )
        self.image_frame.grid(row=0, column=0, rowspan=4, sticky="nw", padx=(0, 12))
        self.image_frame.grid_propagate(False)

        self.image_label = tb.Label(
            self.image_frame,
            text="Selecione\numa carta",
            anchor=CENTER,
            justify=CENTER,
            bootstyle="inverse-secondary",
            wraplength=115,
        )
        self.image_label.pack(fill=BOTH, expand=True)
        self.image_label.bind("<Enter>", self._show_image_zoom)
        self.image_label.bind("<Leave>", self._hide_image_zoom)
        self.image_label.bind("<Motion>", self._move_image_zoom)

        self.detail_name = tb.Label(
            overview,
            text="Nenhuma carta selecionada",
            font=("Segoe UI", 12, "bold"),
            wraplength=280,
            justify=LEFT,
        )
        self.detail_name.grid(row=0, column=1, sticky="nw")

        self.detail_meta = tb.Label(
            overview,
            text="",
            bootstyle="secondary",
            font=("Segoe UI", 8),
            wraplength=280,
            justify=LEFT,
        )
        self.detail_meta.grid(row=1, column=1, sticky="nw", pady=(3, 0))

        self.detail_hint = tb.Label(
            overview,
            text="Selecione uma carta para visualizar os dados do estoque.",
            bootstyle="secondary",
            font=("Segoe UI", 8),
            wraplength=280,
            justify=LEFT,
        )
        self.detail_hint.grid(row=2, column=1, sticky="nw", pady=(10, 8))

        self.selection_label = tb.Label(
            overview,
            text="",
            bootstyle="secondary",
            font=("Segoe UI", 8),
            wraplength=280,
            justify=LEFT,
        )
        self.selection_label.grid(row=3, column=1, sticky="sw")

        # Métricas compactas em uma única linha.
        metrics = tb.Frame(self.details_panel)
        metrics.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        for column in range(3):
            metrics.columnconfigure(column, weight=1, uniform="metric")

        self.detail_price_value = self._detail_metric(metrics, "Preço", 0)
        self.detail_qty_value = self._detail_metric(metrics, "Quantidade", 1)
        self.detail_total_value = self._detail_metric(metrics, "Valor total", 2)

        # Ações diretamente no painel de detalhes.
        actions = tb.Frame(self.details_panel)
        actions.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        actions.columnconfigure(0, weight=1)

        self.price_button = tb.Button(
            actions,
            text="Definir preço",
            bootstyle="info-outline",
            command=self._edit_selected_price,
            state="disabled",
        )
        self.price_button.grid(row=0, column=0, sticky="ew")

        tb.Separator(self.details_panel).grid(
            row=4,
            column=0,
            sticky="ew",
            pady=(0, 8),
        )

        # Histórico mais compacto, mas ocupa a largura total do painel.
        history_area = tb.Frame(self.details_panel)
        history_area.grid(row=5, column=0, sticky="nsew")
        history_area.columnconfigure(0, weight=1)
        history_area.rowconfigure(1, weight=1)

        history_header = tb.Frame(history_area)
        history_header.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        history_header.columnconfigure(0, weight=1)

        tb.Label(
            history_header,
            text="Histórico de alterações",
            font=("Segoe UI", 9, "bold"),
        ).grid(row=0, column=0, sticky="w")

        self.history_count_label = tb.Label(
            history_header,
            text="0 registros",
            bootstyle="secondary",
            font=("Segoe UI", 8),
        )
        self.history_count_label.grid(row=0, column=1, sticky="e")

        history_frame = tb.Frame(history_area)
        history_frame.grid(row=1, column=0, sticky="nsew")
        history_frame.columnconfigure(0, weight=1)
        history_frame.rowconfigure(0, weight=1)

        self.history_tree = tb.Treeview(
            history_frame,
            columns=("when", "action", "details"),
            show="headings",
            height=7,
        )
        self.history_tree.heading("when", text="Data")
        self.history_tree.heading("action", text="Alteração")
        self.history_tree.heading("details", text="Detalhes")
        self.history_tree.column("when", width=82, minwidth=74, anchor=CENTER, stretch=False)
        self.history_tree.column("action", width=108, minwidth=90, anchor=W, stretch=False)
        self.history_tree.column("details", width=190, minwidth=130, anchor=W, stretch=True)

        scroll = tb.Scrollbar(
            history_frame,
            orient="vertical",
            command=self.history_tree.yview,
        )
        self.history_tree.configure(yscrollcommand=scroll.set)
        self.history_tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")

    def _detail_metric(self, parent: tb.Frame, caption: str, column: int) -> tb.Label:
        card = tb.Frame(
            parent,
            padding=(7, 6),
            bootstyle="secondary",
            height=58,
        )
        card.grid(
            row=0,
            column=column,
            sticky="ew",
            padx=(0 if column == 0 else 4, 0),
        )
        card.grid_propagate(False)

        value = tb.Label(
            card,
            text="-",
            font=("Segoe UI", 10, "bold"),
            anchor=W,
            bootstyle="inverse-secondary",
        )
        value.pack(anchor=W, fill=X)

        tb.Label(
            card,
            text=caption,
            bootstyle="inverse-secondary",
            font=("Segoe UI", 7),
            anchor=W,
        ).pack(anchor=W, fill=X, pady=(2, 0))
        return value

    def _build_footer(self, parent: tb.Frame) -> None:
        footer = tb.Frame(parent)
        footer.pack(fill=X, pady=(8, 0))

        self.summary_label = tb.Label(
            footer,
            text="0 exibidos · 0 unidades",
            bootstyle="secondary",
            font=("Segoe UI", 8),
        )
        self.summary_label.pack(side=LEFT)

        self.import_status_label = tb.Label(
            footer,
            text="Estoque local pronto.",
            bootstyle="secondary",
            font=("Segoe UI", 8),
        )
        self.import_status_label.pack(side=RIGHT)

    def _bind_events(self) -> None:
        self.search_entry.bind("<KeyRelease>", self._on_filter_changed)
        self.tree.bind("<<TreeviewSelect>>", self._on_selection_changed)
        self.tree.bind("<Double-1>", lambda _event: self._edit_selected_price())
        self.tree.bind("<Configure>", self._on_stock_table_resize, add="+")

        self.root.bind("<Control-f>", self._focus_search)
        self.root.bind("<Control-i>", lambda _event: self._import_from_liga())
        self.root.bind("<Escape>", lambda _event: self._clear_filters())

    def _on_stock_table_resize(self, _event: tk.Event | None = None) -> None:
        """Distribui o espaço horizontal para as colunas ocuparem toda a tabela."""
        try:
            self.root.after_idle(self._fit_stock_columns_to_table)
        except tk.TclError:
            pass

    def _fit_stock_columns_to_table(self) -> None:
        if not hasattr(self, "tree"):
            return

        columns = list(self.tree["columns"])
        if not columns or len(columns) != len(self._stock_column_base_widths):
            return

        available = max(1, self.tree.winfo_width() - 4)
        base_total = sum(self._stock_column_base_widths)
        widths = list(self._stock_column_base_widths)

        if available > base_total:
            extra = available - base_total
            used = 0
            flexible_indices = list(self._stock_flexible_columns)

            for index in flexible_indices[:-1]:
                addition = int(extra * self._stock_flexible_columns[index])
                widths[index] += addition
                used += addition

            widths[flexible_indices[-1]] += max(0, extra - used)

        elif available < base_total:
            deficit = base_total - available
            minimums = {0: 125, 2: 72, 3: 70}

            for index in (0, 2, 3):
                if deficit <= 0:
                    break
                reducible = max(0, widths[index] - minimums[index])
                reduction = min(reducible, deficit)
                widths[index] -= reduction
                deficit -= reduction

        for index, column in enumerate(columns):
            self.tree.column(
                column,
                width=widths[index],
                minwidth=40 if index not in {0, 2, 3} else 70,
                stretch=False,
            )

    def _focus_search(self, _event: tk.Event | None = None) -> str:
        self.search_entry.focus_set()
        self.search_entry.selection_range(0, END)
        return "break"

    def _on_filter_changed(self, _event: tk.Event | None = None) -> None:
        if self._filter_after_id:
            self.root.after_cancel(self._filter_after_id)
        self._filter_after_id = self.root.after(180, self._refresh_table)

    def _set_status_filter(self, status: str) -> None:
        self.status_var.set(status)
        self._update_filter_button_styles()
        self._refresh_table()

    def _update_filter_button_styles(self) -> None:
        active = self.status_var.get().strip() or "Todos"
        for status, button in self.filter_buttons.items():
            button.configure(
                bootstyle="primary" if status == active else "secondary-outline"
            )

    def _clear_filters(self) -> None:
        self.search_var.set("")
        self.status_var.set("Todos")
        self._update_filter_button_styles()
        self._refresh_table()
        self.search_entry.focus_set()

    def _on_selection_changed(self, _event: tk.Event | None = None) -> None:
        selected = self.tree.selection()
        if not selected:
            self.selection_label.configure(
                text="Nenhuma carta selecionada",
                bootstyle="secondary",
            )
            self.price_button.configure(state="disabled")
            self._clear_details_panel()
            return

        item_id = int(selected[0])
        item = self.store.get_item(item_id)
        if item is None:
            self._clear_details_panel()
            return

        self._detail_item_id = item_id
        details = " · ".join(
            part for part in (item.display_name, item.card_number, item.edition_name) if part
        )
        self.selection_label.configure(
            text=f"{details} · {item.quantity} un.",
            bootstyle="secondary",
        )
        self.price_button.configure(state="normal")

        self._render_details(item)

    def _clear_details_panel(self) -> None:
        self._detail_item_id = None
        self._image_request_id += 1
        self._detail_photo = None
        self._current_full_image = None
        self._hide_image_zoom()

        if not hasattr(self, "detail_name"):
            return

        self.detail_name.configure(text="Nenhuma carta selecionada")
        self.detail_meta.configure(text="")
        self.selection_label.configure(text="")
        self.detail_status.configure(text="")
        self.detail_hint.configure(
            text="Selecione uma carta para visualizar os dados do estoque."
        )
        self.detail_price_value.configure(text="-")
        self.detail_qty_value.configure(text="-")
        self.detail_total_value.configure(text="-")
        self.image_label.configure(image="", text="Selecione uma carta")

        for iid in self.history_tree.get_children(""):
            self.history_tree.delete(iid)
        self.history_count_label.configure(text="0 registros")

    def _render_details(self, item) -> None:
        self.detail_name.configure(text=item.display_name)

        meta_parts = [
            part
            for part in (
                item.card_number,
                item.edition_name,
                item.extras,
                item.language,
                item.quality,
            )
            if str(part or "").strip()
        ]
        self.detail_meta.configure(text=" · ".join(meta_parts))
        self.detail_hint.configure(
            text=f"{item.quantity} unidade(s) disponível(is) nesta variante."
            if item.quantity > 0
            else "Esta variante está sem estoque no momento."
        )
        self.detail_status.configure(
            text=item.status,
            bootstyle="success" if item.quantity > 0 else "danger",
        )

        self.detail_price_value.configure(text=item.price_display or "Sem preço")
        self.detail_qty_value.configure(text=str(item.quantity))
        self.detail_total_value.configure(text=item.total_display or "-")

        self._render_history(item.id)
        self._load_card_image(item)

    def _render_history(self, item_id: int) -> None:
        for iid in self.history_tree.get_children(""):
            self.history_tree.delete(iid)

        changes = self.store.get_history(item_id, limit=30)
        self.history_count_label.configure(
            text=f"{len(changes)} registro(s)"
        )

        for change in changes:
            when = self._format_history_datetime(change.created_at)
            details = change.details.strip()

            if change.old_quantity != change.new_quantity:
                qty_text = f"Qtd {change.old_quantity} → {change.new_quantity}"
                details = f"{qty_text} · {details}" if details else qty_text

            if change.old_price != change.new_price:
                old_price = change.old_price or "sem preço"
                new_price = change.new_price or "sem preço"
                price_text = f"Preço {old_price} → {new_price}"
                details = f"{price_text} · {details}" if details else price_text

            self.history_tree.insert(
                "",
                END,
                values=(when, change.action, details),
            )

    @staticmethod
    def _format_history_datetime(value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            return "-"
        try:
            dt = __import__("datetime").datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
            return dt.strftime("%d/%m %H:%M")
        except ValueError:
            return raw

    def _image_cache_path(self, item) -> Path:
        """Arquivo único por carta, compartilhado entre variantes Foil/qualidade."""
        identity = "|".join(
            (
                str(getattr(item, "liga_card_id", "") or "").strip(),
                str(getattr(item, "edition_name", "") or "").strip(),
                str(getattr(item, "card_number", "") or "").strip(),
                str(getattr(item, "name_en", "") or "").strip(),
                str(getattr(item, "name_pt", "") or "").strip(),
            )
        ).casefold()
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        return self.image_cache_dir / f"{digest}.jpg"

    def _start_bulk_image_download(self) -> None:
        """Baixa para o cache as imagens de todas as cartas do estoque encontradas na API."""
        if self._bulk_image_download_running:
            return

        items = self.store.list_items(search="", status="Todos")
        if not items:
            messagebox.showinfo(
                "Baixar imagens",
                "O estoque está vazio.",
                parent=self.root,
            )
            return

        unique_items = []
        seen_paths: set[str] = set()
        for item in items:
            cache_path = self._image_cache_path(item)
            key = str(cache_path)
            if key in seen_paths:
                continue
            seen_paths.add(key)
            unique_items.append(item)

        cached_count = sum(
            1 for item in unique_items if self._image_cache_path(item).exists()
        )
        missing_count = len(unique_items) - cached_count

        if missing_count == 0:
            messagebox.showinfo(
                "Baixar imagens",
                (
                    f"As {len(unique_items)} imagem(ns) identificadas do estoque "
                    "já estão no cache local."
                ),
                parent=self.root,
            )
            return

        confirmed = messagebox.askyesno(
            "Baixar imagens",
            (
                f"Serão verificadas {len(unique_items)} carta(s) únicas do estoque.\n\n"
                f"Já em cache: {cached_count}\n"
                f"Para baixar: {missing_count}\n\n"
                "O sistema consultará a Pokémon TCG API e salvará as imagens em:\n"
                "data/card_image_cache/\n\n"
                "Deseja continuar?"
            ),
            parent=self.root,
        )
        if not confirmed:
            return

        self._bulk_image_download_running = True
        self._bulk_image_download_cancelled = False
        self.download_images_button.configure(
            text="Cancelar download",
            command=self._cancel_bulk_image_download,
            state="normal",
            bootstyle="danger-outline",
        )
        self._set_import_status(
            f"Imagens: preparando download de {missing_count} carta(s)..."
        )

        thread = threading.Thread(
            target=self._bulk_image_download_worker,
            args=(unique_items,),
            daemon=True,
        )
        thread.start()

    def _cancel_bulk_image_download(self) -> None:
        if not self._bulk_image_download_running:
            return
        self._bulk_image_download_cancelled = True
        self.download_images_button.configure(
            text="Cancelando...",
            state="disabled",
        )
        self._set_import_status("Imagens: cancelamento solicitado...")

    @staticmethod
    def _candidate_image_urls(url: str) -> list[str]:
        """Gera variações úteis para URLs de imagem retornadas sem extensão."""
        clean = str(url or "").strip()
        if not clean:
            return []

        candidates = [clean]
        lower = clean.lower()

        # Alguns registros do TCGdex/cache local armazenam apenas a raiz do asset.
        if "assets.tcgdex.net" in lower and not lower.endswith(
            (".jpg", ".jpeg", ".png", ".webp")
        ):
            candidates.extend(
                [
                    f"{clean}/high.webp",
                    f"{clean}/low.webp",
                    f"{clean}.webp",
                    f"{clean}.png",
                    f"{clean}.jpg",
                ]
            )

        result: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            if candidate not in seen:
                seen.add(candidate)
                result.append(candidate)
        return result

    def _download_valid_image(self, urls: list[str], *, timeout=(4, 8)) -> Image.Image:
        """Baixa a primeira URL que realmente retornar uma imagem válida."""
        last_error = "imagem não encontrada"

        for url in urls:
            try:
                response = requests.get(
                    url,
                    headers={
                        "User-Agent": "pokemon-cards-stock/1.0",
                        "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.8",
                    },
                    timeout=timeout,
                    allow_redirects=True,
                )

                if response.status_code != 200:
                    last_error = f"HTTP {response.status_code}"
                    continue

                content_type = str(response.headers.get("Content-Type", "")).lower()
                payload = response.content

                # Evita enviar HTML/JSON de erro para o Pillow.
                if not payload:
                    last_error = "resposta vazia"
                    continue

                if (
                    content_type
                    and not content_type.startswith("image/")
                    and payload[:20].lstrip().startswith((b"<", b"{", b"["))
                ):
                    last_error = f"conteúdo inválido ({content_type})"
                    continue

                try:
                    image = Image.open(BytesIO(payload))
                    image.load()
                    return image.convert("RGB")
                except Exception:
                    last_error = f"arquivo retornado não é uma imagem ({content_type or 'tipo desconhecido'})"
                    continue

            except requests.RequestException as exc:
                last_error = str(exc)
                continue

        raise LookupError(last_error)

    def _bulk_image_download_worker(self, items: list) -> None:
        downloaded = 0
        cached = 0
        failed = 0
        failures: list[str] = []
        total = len(items)
        processed = 0

        for index, item in enumerate(items, start=1):
            if self._bulk_image_download_cancelled:
                break

            cache_path = self._image_cache_path(item)
            card_name = str(
                getattr(item, "display_name", "")
                or getattr(item, "name_en", "")
                or getattr(item, "name_pt", "")
                or "Carta"
            )

            # Atualiza antes de iniciar a consulta para deixar claro qual carta
            # está sendo processada, mesmo durante uma chamada de rede.
            self._queue_bulk_progress(
                index - 1,
                total,
                downloaded,
                cached,
                failed,
                current_card=card_name,
            )

            if cache_path.exists():
                cached += 1
                processed += 1
                self._queue_bulk_progress(
                    processed,
                    total,
                    downloaded,
                    cached,
                    failed,
                    current_card=card_name,
                )
                continue

            try:
                image_url = ""

                # Para lote usamos uma busca curta. A política pesada de retries
                # da seleção individual poderia fazer uma única carta "prender"
                # o lote por muito tempo.
                try:
                    name_en = self._clean_api_name(getattr(item, "name_en", ""))
                    number = self._clean_api_name(getattr(item, "card_number", ""))

                    queries = []
                    if name_en and number:
                        queries.append(f'name:"{name_en}" number:{number}')
                    if name_en:
                        queries.append(f'name:"{name_en}"')
                    if number:
                        queries.append(f'number:{number}')

                    for query in queries[:2]:
                        if self._bulk_image_download_cancelled:
                            break

                        response = requests.get(
                            f"{self.pokemon_api_base_url}/cards",
                            params={
                                "q": query,
                                "page": 1,
                                "pageSize": 20,
                                "select": "id,name,number,set,images",
                            },
                            headers=dict(self.pokemon_api_session.headers),
                            timeout=(4, 6),
                        )

                        if response.status_code in {429, 500, 502, 503, 504}:
                            continue

                        response.raise_for_status()
                        payload = response.json()
                        cards = payload.get("data", []) if isinstance(payload, dict) else []
                        if cards:
                            card = self._choose_api_card(item, cards)
                            if card:
                                images = card.get("images") or {}
                                image_url = str(
                                    images.get("large") or images.get("small") or ""
                                ).strip()
                                if image_url:
                                    break
                except Exception:
                    # Falha da Pokémon TCG API não interrompe o lote.
                    image_url = ""

                # Fallback imediato para o catálogo TCGdex local.
                fallback_url = self.store.find_image_url(item)

                candidate_urls: list[str] = []
                if image_url:
                    candidate_urls.extend(self._candidate_image_urls(image_url))
                if fallback_url:
                    candidate_urls.extend(self._candidate_image_urls(fallback_url))

                # Remove duplicados preservando prioridade: Pokémon TCG API primeiro,
                # depois TCGdex/cache local.
                candidate_urls = list(dict.fromkeys(candidate_urls))

                if not candidate_urls:
                    raise LookupError("imagem não encontrada")

                image = self._download_valid_image(
                    candidate_urls,
                    timeout=(4, 8),
                )

                cache_path.parent.mkdir(parents=True, exist_ok=True)
                image.save(
                    cache_path,
                    format="JPEG",
                    quality=92,
                    optimize=True,
                )
                downloaded += 1

            except Exception as exc:  # noqa: BLE001
                failed += 1
                if len(failures) < 20:
                    short_error = " ".join(str(exc).split())
                    if len(short_error) > 80:
                        short_error = short_error[:77] + "..."
                    failures.append(f"{card_name}: {short_error}")

            processed += 1
            self._queue_bulk_progress(
                processed,
                total,
                downloaded,
                cached,
                failed,
                current_card=card_name,
            )

        cancelled = self._bulk_image_download_cancelled
        try:
            if self._widget_exists(self.root):
                self.root.after(
                    0,
                    lambda d=downloaded, c=cached, f=failed, errs=tuple(failures), p=processed, t=total, x=cancelled:
                        self._finish_bulk_image_download(d, c, f, errs, p, t, x),
                )
        except tk.TclError:
            pass

    @staticmethod
    def _widget_exists(widget) -> bool:
        try:
            return widget is not None and bool(widget.winfo_exists())
        except (tk.TclError, AttributeError):
            return False

    def _queue_bulk_progress(
        self,
        current: int,
        total: int,
        downloaded: int,
        cached: int,
        failed: int,
        current_card: str = "",
    ) -> None:
        card_suffix = f" · {current_card}" if current_card else ""
        message = (
            f"Imagens: {current}/{total} · "
            f"baixadas {downloaded} · cache {cached} · falhas {failed}"
            f"{card_suffix}"
        )

        def update_progress() -> None:
            try:
                if not self._widget_exists(self.root):
                    return
                self._set_import_status(message)

                button = getattr(self, "download_images_button", None)
                if (
                    self._bulk_image_download_running
                    and not self._bulk_image_download_cancelled
                    and self._widget_exists(button)
                ):
                    button.configure(text=f"Cancelar ({current}/{total})")
            except tk.TclError:
                return

        try:
            if self._widget_exists(self.root):
                self.root.after(0, update_progress)
        except tk.TclError:
            pass

    def _finish_bulk_image_download(
        self,
        downloaded: int,
        cached: int,
        failed: int,
        failures: tuple[str, ...],
        processed: int,
        total: int,
        cancelled: bool,
    ) -> None:
        self._bulk_image_download_running = False
        self._bulk_image_download_cancelled = False

        if not self._widget_exists(self.root):
            return

        button = getattr(self, "download_images_button", None)
        if self._widget_exists(button):
            try:
                button.configure(
                    text="Baixar imagens",
                    command=self._start_bulk_image_download,
                    state="normal",
                    bootstyle="primary-outline",
                )
            except tk.TclError:
                pass

        if cancelled:
            self._set_import_status(
                f"Imagens: cancelado em {processed}/{total} · "
                f"{downloaded} baixada(s) · {cached} cache · {failed} falha(s)."
            )
        else:
            self._set_import_status(
                f"Imagens: concluído {processed}/{total} · "
                f"{downloaded} baixada(s) · {cached} cache · {failed} falha(s)."
            )

        if self._detail_item_id is not None:
            item = self.store.get_item(self._detail_item_id)
            if item is not None:
                self._load_card_image(item)

        details = ""
        if failures:
            details = "\n\nAlgumas falhas:\n" + "\n".join(failures)

        title = "Download cancelado" if cancelled else "Download de imagens"
        intro = (
            "Download interrompido. O que já foi baixado permanece em cache."
            if cancelled
            else "Download concluído."
        )

        messagebox.showinfo(
            title,
            (
                f"{intro}\n\n"
                f"Processadas: {processed}/{total}\n"
                f"Baixadas: {downloaded}\n"
                f"Já em cache: {cached}\n"
                f"Não encontradas / falhas: {failed}\n\n"
                "Cache: data/card_image_cache/"
                f"{details}"
            ),
            parent=self.root,
        )

    def _load_card_image(self, item) -> None:
        """Mostra primeiro o cache local; consulta a API apenas se necessário."""
        self._image_request_id += 1
        request_id = self._image_request_id
        self._detail_photo = None

        cache_path = self._image_cache_path(item)

        # 1) Cache em memória.
        memory_key = str(cache_path)
        cached = self._image_cache.get(memory_key)
        if cached is not None:
            try:
                if cache_path.exists():
                    self._current_full_image = Image.open(cache_path).convert("RGB")
            except OSError:
                self._current_full_image = None

            self._apply_card_image(
                request_id,
                cached.copy(),
                source_text="Imagem em cache",
            )
            return

        # 2) Cache persistente no disco.
        if cache_path.exists():
            try:
                full_image = Image.open(cache_path).convert("RGB")
                self._current_full_image = full_image.copy()

                image = full_image.copy()
                image.thumbnail((129, 182), Image.Resampling.LANCZOS)
                self._image_cache[memory_key] = image.copy()
                self._apply_card_image(
                    request_id,
                    image,
                    source_text="Imagem em cache",
                )
                return
            except (OSError, ValueError):
                # Cache danificado: remove e consulta a API novamente.
                try:
                    cache_path.unlink()
                except OSError:
                    pass

        # 3) Não existe localmente: consulta a Pokémon TCG API em segundo plano.
        self.image_label.configure(
            image="",
            text="Buscando imagem\nna Pokémon TCG API...",
        )
        self.detail_status.configure(
            text="Buscando imagem...",
            bootstyle="info",
        )

        thread = threading.Thread(
            target=self._pokemon_api_image_worker,
            args=(request_id, item, cache_path),
            daemon=True,
        )
        thread.start()

    @staticmethod
    def _clean_api_name(value: str) -> str:
        return " ".join(str(value or "").replace('"', "").strip().split())

    def _pokemon_api_search_cards(self, item) -> list[dict]:
        """Busca candidatos na Pokémon TCG API com tolerância a falhas 5xx."""
        name_en = self._clean_api_name(getattr(item, "name_en", ""))
        number = self._clean_api_name(getattr(item, "card_number", ""))

        queries: list[str] = []
        if name_en and number:
            queries.append(f'name:"{name_en}" number:{number}')
        if name_en:
            queries.append(f'name:"{name_en}"')
        if number:
            queries.append(f'number:{number}')

        seen: set[str] = set()
        last_error: str = ""

        for query in queries:
            if query in seen:
                continue
            seen.add(query)

            try:
                response = self.pokemon_api_session.get(
                    f"{self.pokemon_api_base_url}/cards",
                    params={
                        "q": query,
                        "page": 1,
                        "pageSize": 50 if query.startswith("number:") else 20,
                        "orderBy": "-set.releaseDate",
                        "select": "id,name,number,set,images",
                    },
                    timeout=12,
                )

                # Em 5xx/429, tenta a próxima consulta ao invés de encerrar tudo.
                if response.status_code >= 500 or response.status_code == 429:
                    last_error = f"HTTP {response.status_code} em {query}"
                    continue

                response.raise_for_status()
                payload = response.json()
                cards = payload.get("data", []) if isinstance(payload, dict) else []
                if cards:
                    return cards

            except requests.RequestException as exc:
                last_error = str(exc)
                continue
            except ValueError as exc:
                last_error = f"Resposta inválida da API: {exc}"
                continue

        if last_error:
            raise LookupError(
                f"Pokémon TCG API indisponível ou sem resultado ({last_error})."
            )
        return []

    def _choose_api_card(self, item, cards: list[dict]) -> dict | None:
        """Escolhe o candidato mais provável sem depender do ID da LigaPokemon."""
        if not cards:
            return None

        target_number = str(getattr(item, "card_number", "") or "").strip().casefold()
        target_name_en = self._clean_api_name(getattr(item, "name_en", "")).casefold()
        target_edition = str(getattr(item, "edition_name", "") or "").strip().casefold()

        def score(card: dict) -> tuple[int, int]:
            score_value = 0

            number = str(card.get("number", "") or "").strip().casefold()
            name = str(card.get("name", "") or "").strip().casefold()
            set_data = card.get("set") or {}
            set_name = str(set_data.get("name", "") or "").strip().casefold()

            if target_number and number == target_number:
                score_value += 50
            if target_name_en and name == target_name_en:
                score_value += 80
            elif target_name_en and target_name_en in name:
                score_value += 30

            # Nome de coleção é somente um bônus porque LigaPokemon e Pokémon TCG
            # API nem sempre usam exatamente a mesma nomenclatura.
            if target_edition and set_name:
                if target_edition == set_name:
                    score_value += 30
                elif target_edition in set_name or set_name in target_edition:
                    score_value += 12

            images = card.get("images") or {}
            if images.get("large") or images.get("small"):
                score_value += 5

            release_date = str(set_data.get("releaseDate", "") or "")
            return score_value, int(release_date.replace("/", "") or "0")

        return max(cards, key=score)

    def _pokemon_api_image_worker(self, request_id: int, item, cache_path: Path) -> None:
        api_error = ""
        try:
            image_url = ""

            try:
                cards = self._pokemon_api_search_cards(item)
                card = self._choose_api_card(item, cards)
                if card:
                    images = card.get("images") or {}
                    image_url = str(
                        images.get("large") or images.get("small") or ""
                    ).strip()
            except Exception as exc:  # noqa: BLE001
                api_error = str(exc)

            # Fallback: usa o cache TCGdex já existente no sistema quando a
            # Pokémon TCG API estiver instável ou não encontrar a carta.
            source_text = "Pokémon TCG API"
            if not image_url:
                fallback_url = self.store.find_image_url(item)
                if fallback_url:
                    image_url = fallback_url
                    source_text = "TCGdex (fallback)"

            if not image_url:
                raise LookupError(
                    api_error or "Carta não encontrada na Pokémon TCG API."
                )

            candidate_urls = self._candidate_image_urls(image_url)
            image = self._download_valid_image(
                candidate_urls,
                timeout=(5, 12),
            )
            self._current_full_image = image.copy()

            cache_path.parent.mkdir(parents=True, exist_ok=True)
            image.save(cache_path, format="JPEG", quality=92, optimize=True)

            display_image = image.copy()
            display_image.thumbnail((129, 182), Image.Resampling.LANCZOS)
            self._image_cache[str(cache_path)] = display_image.copy()

            self.root.after(
                0,
                lambda rid=request_id, img=display_image, src=source_text:
                    self._apply_card_image(
                        rid,
                        img,
                        source_text=f"{src} · salva em cache",
                    ),
            )
        except Exception as exc:  # noqa: BLE001
            error_message = str(exc)
            self.root.after(
                0,
                lambda rid=request_id, msg=error_message:
                    self._apply_card_image_error(rid, msg),
            )

    def _apply_card_image(
        self,
        request_id: int,
        image: Image.Image,
        *,
        source_text: str = "",
    ) -> None:
        if request_id != self._image_request_id:
            return

        photo = ImageTk.PhotoImage(image)
        self._detail_photo = photo
        self.image_label.configure(image=photo, text="")

        # Mantém o status principal da variante, mas informa a origem da imagem
        # na linha auxiliar para não confundir com Disponível/Sem estoque.
        if hasattr(self, "detail_hint") and source_text:
            current = self.detail_hint.cget("text")
            suffix = f"  •  {source_text}"
            if suffix not in str(current):
                self.detail_hint.configure(text=f"{current}{suffix}")

        item = self.store.get_item(self._detail_item_id) if self._detail_item_id else None
        if item is not None:
            self.detail_status.configure(
                text=item.status,
                bootstyle="success" if item.quantity > 0 else "danger",
            )

    def _apply_card_image_error(self, request_id: int, message: str = "") -> None:
        if request_id != self._image_request_id:
            return

        self._detail_photo = None
        self._current_full_image = None
        self._hide_image_zoom()
        self.image_label.configure(
            image="",
            text="Imagem não encontrada\nna Pokémon TCG API.",
        )

        item = self.store.get_item(self._detail_item_id) if self._detail_item_id else None
        if item is not None:
            self.detail_status.configure(
                text=item.status,
                bootstyle="success" if item.quantity > 0 else "danger",
            )

        if hasattr(self, "detail_hint"):
            base = self.detail_hint.cget("text")
            if "Imagem:" not in str(base):
                short_message = " ".join(str(message or "").split())
                if len(short_message) > 95:
                    short_message = short_message[:92] + "..."
                self.detail_hint.configure(
                    text=f"{base}  •  Imagem: {short_message or 'não encontrada'}"
                )

    def _show_image_zoom(self, event: tk.Event | None = None) -> None:
        """Mostra uma prévia ampliada enquanto o mouse estiver sobre a imagem."""
        if self._current_full_image is None:
            return

        self._hide_image_zoom()

        zoom = tk.Toplevel(self.root)
        zoom.overrideredirect(True)
        zoom.attributes("-topmost", True)
        zoom.configure(bg="white")

        image = self._current_full_image.copy()
        image.thumbnail((420, 585), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(image)

        label = tk.Label(
            zoom,
            image=photo,
            bg="white",
            bd=1,
            relief="solid",
        )
        label.pack()

        self._zoom_window = zoom
        self._zoom_photo = photo

        if event is not None:
            self._position_zoom_window(event.x_root, event.y_root)
        else:
            x = self.image_label.winfo_rootx() + self.image_label.winfo_width() + 12
            y = self.image_label.winfo_rooty()
            self._position_zoom_window(x, y, absolute=True)

    def _move_image_zoom(self, event: tk.Event) -> None:
        if self._zoom_window is None:
            return
        self._position_zoom_window(event.x_root, event.y_root)

    def _position_zoom_window(
        self,
        x_root: int,
        y_root: int,
        *,
        absolute: bool = False,
    ) -> None:
        if self._zoom_window is None:
            return

        self._zoom_window.update_idletasks()
        width = self._zoom_window.winfo_reqwidth()
        height = self._zoom_window.winfo_reqheight()

        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()

        if absolute:
            x = x_root
            y = y_root
        else:
            x = x_root + 18
            y = y_root + 18

        if x + width > screen_w - 8:
            x = max(8, x_root - width - 18)

        if y + height > screen_h - 8:
            y = max(8, screen_h - height - 8)

        self._zoom_window.geometry(f"+{x}+{y}")

    def _hide_image_zoom(self, _event: tk.Event | None = None) -> None:
        if self._zoom_window is not None:
            try:
                if self._zoom_window.winfo_exists():
                    self._zoom_window.destroy()
            except tk.TclError:
                pass

        self._zoom_window = None
        self._zoom_photo = None

    def _refresh_table(self) -> None:
        self._filter_after_id = None

        selected_before = self.tree.selection()
        selected_id = selected_before[0] if selected_before else None

        for iid in self.tree.get_children(""):
            self.tree.delete(iid)

        search = self.search_var.get().strip()
        status = self.status_var.get().strip() or "Todos"

        # Uma consulta sem filtro de status fornece os contadores rápidos da busca.
        search_items = self.store.list_items(search=search, status="Todos")
        if status == "Todos":
            items = search_items
        else:
            items = [
                item for item in search_items
                if item.status == status
            ]

        for item in items:
            self.tree.insert(
                "",
                END,
                iid=str(item.id),
                values=(
                    item.display_name,
                    item.card_number,
                    item.edition_name,
                    item.extras or "",
                    item.language or "PT",
                    item.status,
                    item.quantity,
                    item.price_display,
                    item.total_display,
                ),
                tags=("available" if item.quantity > 0 else "empty",),
            )

        # Métricas globais do estoque.
        all_items = self.store.list_items(search="", status="Todos")
        total_variants = len(all_items)
        total_units = sum(item.quantity for item in all_items)
        available_variants = sum(1 for item in all_items if item.quantity > 0)
        stock_value = sum(
            (
                item.unit_price_decimal * item.quantity
                for item in all_items
                if item.unit_price_decimal is not None
            ),
            Decimal("0.00"),
        )

        self.metric_labels["variants"].configure(text=str(total_variants))
        self.metric_labels["units"].configure(text=str(total_units))
        self.metric_labels["available"].configure(text=str(available_variants))
        self.metric_labels["value"].configure(text=self._format_brl_decimal(stock_value))

        # Contadores rápidos respeitam a busca atual.
        counts = {
            "Todos": len(search_items),
            "Disponível": sum(1 for item in search_items if item.quantity > 0),
            "Sem estoque": sum(1 for item in search_items if item.quantity <= 0),
        }
        captions = dict(self.FILTERS)
        for key, button in self.filter_buttons.items():
            button.configure(text=f"{captions[key]} ({counts[key]})")

        shown_units = sum(item.quantity for item in items)
        self.table_summary_label.configure(
            text=f"{len(items)} variante(s) · {shown_units} unidade(s)"
        )
        self.summary_label.configure(
            text=f"{len(items)} exibido(s) · {shown_units} unidade(s)"
        )

        if selected_id and self.tree.exists(selected_id):
            self.tree.selection_set(selected_id)
            self.tree.see(selected_id)
            self._on_selection_changed()
        else:
            self._on_selection_changed()

    def _edit_selected_price(self) -> None:
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning(
                "Preço",
                "Selecione uma carta do estoque.",
                parent=self.root,
            )
            return

        item_id = int(selected[0])
        row_values = self.tree.item(selected[0], "values") or ()
        current = str(row_values[7] if len(row_values) > 7 else "").replace("R$", "").strip()

        value = simpledialog.askstring(
            "Preço unitário",
            "Informe o preço unitário. Deixe vazio para manter sem preço:",
            initialvalue=current,
            parent=self.root,
        )
        if value is None:
            return

        try:
            self.store.update_price(item_id, value)
        except ValueError as exc:
            messagebox.showwarning("Preço", str(exc), parent=self.root)
            return

        self._refresh_table()
        if self.tree.exists(str(item_id)):
            self.tree.selection_set(str(item_id))
            self.tree.see(str(item_id))
            self._on_selection_changed()

    def _set_import_status(self, text: str) -> None:
        label = getattr(self, "import_status_label", None)
        if label is not None:
            try:
                label.configure(text=text)
            except tk.TclError:
                pass

    def _import_from_liga(self) -> None:
        file_path = filedialog.askopenfilename(
            parent=self.root,
            title="Importar estoque da LigaPokemon",
            filetypes=[
                ("LigaPokemon / Excel", "*.csv *.xlsx *.xlsm"),
                ("CSV", "*.csv"),
                ("Excel", "*.xlsx *.xlsm"),
                ("Todos os arquivos", "*.*"),
            ],
        )
        if not file_path:
            return

        self.import_button.configure(state="disabled")
        self._set_import_status("Importando arquivo da LigaPokemon...")
        self.root.update_idletasks()

        try:
            result = self.store.import_ligapokemon(file_path)
        except Exception as exc:  # noqa: BLE001
            self._set_import_status("Falha na importação.")
            messagebox.showerror(
                "Importar da Liga",
                f"Não foi possível importar o arquivo:\n\n{exc}",
                parent=self.root,
            )
            return
        finally:
            self.import_button.configure(state="normal")

        self._refresh_table()
        self._set_import_status(
            f"LigaPokemon: {result.variants_imported} variante(s), "
            f"{result.total_units} unidade(s) importadas."
        )

        messagebox.showinfo(
            "Importar da Liga",
            "Importação concluída com sucesso.\n\n"
            f"Linhas lidas: {result.rows_read}\n"
            f"Variantes importadas: {result.variants_imported}\n"
            f"Novas: {result.inserted}\n"
            f"Atualizadas: {result.updated}\n"
            f"Unidades no arquivo: {result.total_units}\n"
            f"Ignoradas: {result.skipped}\n\n"
            "Ao importar o mesmo arquivo novamente, as quantidades são sincronizadas e não duplicadas.",
            parent=self.root,
        )
