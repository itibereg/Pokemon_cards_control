from __future__ import annotations
# VERSAO_ATUALIZADA: NOT_FOUND_EXCLUDED_FROM_TOTAL_V103_2026_09_30

from dataclasses import replace
from pathlib import Path
from datetime import datetime
import re
import math
import threading
import time
import unicodedata

import tkinter as tk
from tkinter import font as tkfont
import ttkbootstrap as tb
from ttkbootstrap.constants import *
from ttkbootstrap.tableview import Tableview
from tkinter import filedialog, messagebox

from app.card_parser import CardParseError, parse_card_text
from app.catalog import CardCatalogStore, CardSuggestion
from app.card_cache import CardCacheUpdateError, TcgDexCardCacheStore, CardCacheUpdateResult
from app.excel_exporter import export_sale_to_xlsx
from app.models import (
    SaleItem,
    item_is_trade, display_item_extras, trade_summary,
    calculate_totals,
    format_brl,
    parse_money_brl,
    ITEM_STATUS_SEPARATED,
    ITEM_STATUS_NOT_FOUND,
)
from app.pokemon_tcg_api import ApiCardSuggestion, PokemonTcgApiClient, PokemonTcgApiError
from app.sales_store import SalesHistoryStore, SavedSaleSummary, SALE_STATUS_OPTIONS
from app.stock_store import StockStore, StockItem, StockQuantityError
from app.ui.stock_window import StockWindow
from app.ui.sale_details_window import SaleDetailsWindow


# Largura do espaço principal de trabalho. O histórico ocupa o painel lateral.
SALE_PANEL_WIDTH = 1050
DELIVERY_LOCATIONS = ("Domínio", "Multiverso", "Countergames")
TABLE_SURFACE_WIDTH = SALE_PANEL_WIDTH

# Larguras-base das colunas da venda. A tabela distribui o espaço disponível
# sem a antiga coluna vazia de margem.
TABLE_BASE_COLUMN_WIDTHS = [220, 80, 82, 105, 72, 132, 54, 105, 112]
TABLE_MIN_COLUMN_WIDTHS = [160, 68, 68, 85, 62, 120, 50, 94, 100]
TABLE_EXTRA_WIDTH_RATIOS = [0.25, 0.05, 0.06, 0.10, 0.06, 0.16, 0.05, 0.12, 0.15]
HISTORY_PANEL_MIN_WIDTH = 360



def _customer_search_key(value: str) -> str:
    """Busca por nome sem diferenças de acentos, caixa ou espaços repetidos."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).casefold().split())


def _customer_contacts_from_sales(summaries, format_name, format_phone):
    """Agrupa contatos sem misturar telefones diferentes de um mesmo nome."""
    contacts = {}
    for sale in summaries:
        name = format_name(getattr(sale, "customer_name", "") or "").strip()
        if not name:
            continue
        phone = format_phone(getattr(sale, "customer_phone", "") or "")
        key = (_customer_search_key(name), phone)
        contacts.setdefault(key, (name, phone))
    names_with_phone = {key[0] for key in contacts if key[1]}
    return sorted(
        (contact for key, contact in contacts.items() if key[1] or key[0] not in names_with_phone),
        key=lambda contact: (_customer_search_key(contact[0]), contact[1]),
    )


class CustomerAutocompleteEntry(tb.Entry):
    """Sugestões do histórico; inspirado no AutocompleteEntry de general_functions."""

    def __init__(self, master, *, on_selected, **kwargs):
        super().__init__(master, **kwargs)
        self._on_selected = on_selected
        self._contacts = []
        self._hits = []
        self._search_after_id = None
        self._popup = tk.Toplevel(self)
        self._popup.withdraw()
        self._popup.overrideredirect(True)
        self._popup.transient(self.winfo_toplevel())
        self._listbox = tk.Listbox(
            self._popup, height=6, exportselection=False, takefocus=False,
            font=(self.cget("font") or tb.Style().lookup(
                self.cget("style") or "TEntry", "font") or "TkTextFont"),
            activestyle="none",
        )
        scrollbar = tb.Scrollbar(self._popup, orient="vertical", command=self._listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self._listbox.configure(yscrollcommand=scrollbar.set)
        self._listbox.pack(fill="both", expand=True)
        self.bind("<KeyRelease>", self._schedule_search, add="+")
        self.bind("<Down>", lambda event: self._navigate(1), add="+")
        self.bind("<Up>", lambda event: self._navigate(-1), add="+")
        self.bind("<Return>", self._confirm, add="+")
        self.bind("<Tab>", self._tab, add="+")
        self.bind("<Escape>", self._escape, add="+")
        self.bind("<FocusOut>", self._focus_out, add="+")
        self.bind("<Destroy>", self._destroy_popup, add="+")
        # Confirma antes de mudar o foco, evitando que FocusOut esconda o clique.
        self._listbox.bind("<ButtonPress-1>", self._click)

    def build(self, contacts):
        self._contacts = list(contacts)
        self._close()

    def _schedule_search(self, event):
        if event.keysym in {"Up", "Down", "Left", "Right", "Return", "Tab", "Escape"}:
            return
        if self._search_after_id is not None:
            self.after_cancel(self._search_after_id)
        self._search_after_id = self.after(250, self._search)

    def _search(self):
        self._search_after_id = None
        if not self.winfo_exists() or self.focus_get() is not self:
            return
        query = _customer_search_key(self.get())
        if not query:
            self._close()
            return
        self._hits = [contact for contact in self._contacts
                      if query in _customer_search_key(contact[0])]
        self._listbox.delete(0, tk.END)
        if not self._hits:
            self._close()
            return
        for name, phone in self._hits:
            self._listbox.insert(tk.END, f"{name} — {phone}" if phone else f"{name} — sem telefone")
        rows = min(6, len(self._hits))
        self._listbox.configure(height=rows)
        line_height = tkfont.Font(root=self, font=self._listbox.cget("font")).metrics("linespace")
        height = rows * (line_height + 2) + 8
        width = max(self.winfo_width(), 340)
        x = min(self.winfo_rootx(), max(0, self.winfo_screenwidth() - width))
        y = self.winfo_rooty() + self.winfo_height()
        if y + height > self.winfo_screenheight():
            y = max(0, self.winfo_rooty() - height)
        self._popup.geometry(f"{width}x{height}+{max(0, x)}+{y}")
        self._popup.deiconify()
        self._popup.lift()

    def _close(self):
        if self._search_after_id is not None:
            self.after_cancel(self._search_after_id)
            self._search_after_id = None
        self._popup.withdraw()
        self._hits = []

    def _navigate(self, direction):
        if not self._hits:
            self._search()
        if not self._hits:
            return
        selected = self._listbox.curselection()
        index = max(0, min(len(self._hits) - 1, selected[0] + direction)) if selected else (
            0 if direction > 0 else len(self._hits) - 1)
        self._listbox.selection_clear(0, tk.END)
        self._listbox.selection_set(index)
        self._listbox.activate(index)
        self._listbox.see(index)
        return "break"

    def _choose(self, index):
        name, phone = self._hits[index]
        self._close()
        self._on_selected(name, phone)
        self.icursor(tk.END)
        self.event_generate("<<Autocompleted>>")

    def _confirm(self, _event=None):
        if self._hits:
            selected = self._listbox.curselection()
            if selected:
                self._choose(selected[0])
            elif len(self._hits) == 1:
                self._choose(0)
            return "break"

    def _tab(self, _event=None):
        if self._hits:
            selected = self._listbox.curselection()
            if selected:
                self._choose(selected[0])
            elif len(self._hits) == 1:
                self._choose(0)
        self._close()
        # Deixa o Tk avançar normalmente para o campo Telefone.

    def _escape(self, _event=None):
        if self._hits:
            self._close()
            return "break"

    def _click(self, event):
        index = self._listbox.nearest(event.y)
        bounds = self._listbox.bbox(index)
        if bounds and bounds[1] <= event.y < bounds[1] + bounds[3] and index < len(self._hits):
            self._choose(index)
        return "break"

    def _focus_out(self, _event=None):
        self._close()

    def _destroy_popup(self, event):
        if event.widget is self:
            if self._search_after_id is not None:
                self.after_cancel(self._search_after_id)
                self._search_after_id = None
            if self._popup.winfo_exists():
                self._popup.destroy()


class SalesWindow:
    """Janela simples para montar vendas/orçamentos de cartas."""

    def __init__(self, root: tb.Window) -> None:
        self.root = root
        self._startup_started_at = time.perf_counter()
        self._startup_last_mark = self._startup_started_at
        self._startup_timings: list[tuple[str, float, float]] = []
        self._startup_mark("início __init__")

        # V18: evita o "piscar" inicial.
        # A janela fica invisível enquanto tamanho, posição e layout são montados;
        # só depois é exibida já centralizada.
        self.root.withdraw()
        self.root.title("Venda Pokémon Cards")
        self.root.minsize(1280, 550)

        self._sale_items_window = None
        self._sale_items_open_pending = False
        self.items: list[SaleItem] = []
        self.item_by_id: dict[str, SaleItem] = {}
        self.catalog = CardCatalogStore()
        self.card_cache = TcgDexCardCacheStore()
        self.cache_update_in_progress = False
        self.api_client = PokemonTcgApiClient()
        self.sales_store = SalesHistoryStore()
        self.stock_store = StockStore()

        self.current_sale_id: str | None = None
        self.selected_item_id: str | None = None
        self.selected_stock_item_id: int | None = None
        self.item_context_menu: tk.Menu | None = None
        self.sale_title_var = tb.StringVar(value=self.sales_store.generate_sale_code())
        self.customer_var = tb.StringVar()
        self.customer_phone_var = tb.StringVar()
        self.sale_status_var = tb.StringVar(value="Aberta")
        self.sale_status_display_var = tb.StringVar(value="Aberta")
        self.sale_status_var.trace_add(
            "write",
            lambda *_: self.sale_status_display_var.set(
                self._display_sale_status(self.sale_status_var.get())
            ),
        )
        self.history_filter_var = tb.StringVar()
        self.history_status_filter_var = tb.StringVar(value="Aberta")
        self.history_filter_buttons: dict[str, tb.Button] = {}
        self.history_filter_captions: dict[str, str] = {}
        self.sales_by_id: dict[str, SavedSaleSummary] = {}
        self._history_net_totals: dict[str, float] = {}
        self._history_sort_column: int | None = None
        self._history_sort_reverse = False

        self.suggestions: list[CardSuggestion] = []
        self.suggestion_popup: tk.Toplevel | None = None
        self.suggestion_frame: tb.Frame | None = None  # legado; V25 usa popup Toplevel.
        self.suggestion_tree = None  # legado: versões anteriores usavam Treeview.
        self.suggestion_listbox: tk.Listbox | None = None
        self.suggestion_context_index: int | None = None
        self.suggestion_after_id: str | None = None
        self.api_search_token = 0
        self.api_request_in_progress = False
        self.api_query_in_flight = ""
        self.pending_api_query = ""
        self.api_cooldown_until = 0.0
        self.last_api_completed_query = ""
        self.last_rendered_suggestion_texts: tuple[str, ...] = ()
        self.current_suggestions_query = ""
        self.api_failed_until_by_query: dict[str, float] = {}

        self.card_text_var = tb.StringVar()
        self.quantity_var = tb.StringVar(value="1")
        self.unit_price_var = tb.StringVar(value="0,00")
        self.extras_var = tb.StringVar()
        self.language_var = tb.StringVar(value="PT")
        self._configure_window_size()
        self._startup_mark("_configure_window_size")
        self._configure_style()
        self._startup_mark("_configure_style")
        self._build_ui()
        self._startup_mark("_build_ui")
        self._bind_events()
        self._startup_mark("_bind_events")
        self._update_totals()
        self._startup_mark("_update_totals")
        self._show_centered_window()
        self._startup_mark("_show_centered_window")
        self._print_startup_timing_report()

        # A consulta ao SQLite do cache não bloqueia mais a abertura da janela.
        # Ela começa depois que a interface já foi exibida e roda fora da thread do Tkinter.
        self.root.after(50, self._start_deferred_cache_status_refresh)


    def _startup_mark(self, label: str) -> None:
        now = time.perf_counter()
        step = now - getattr(self, "_startup_last_mark", now)
        total = now - getattr(self, "_startup_started_at", now)
        self._startup_last_mark = now
        self._startup_timings.append((label, step, total))
        print(f"[startup] {label:<30} etapa={step:7.3f}s total={total:7.3f}s")

    def _print_startup_timing_report(self) -> None:
        total = time.perf_counter() - self._startup_started_at
        print("\n[startup] ===== RELATÓRIO DE ABERTURA =====")
        for label, step, running_total in self._startup_timings:
            print(f"[startup] {label:<30} etapa={step:7.3f}s acumulado={running_total:7.3f}s")
        print(f"[startup] TOTAL ABERTURA                  {total:7.3f}s")
        print("[startup] ===============================\n")

    def _get_windows_work_area(self) -> tuple[int, int, int, int]:
        """Retorna a área útil do Windows (sem a barra de tarefas)."""
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()

        try:
            import ctypes
            from ctypes import wintypes

            rect = wintypes.RECT()
            SPI_GETWORKAREA = 0x0030
            ok = ctypes.windll.user32.SystemParametersInfoW(
                SPI_GETWORKAREA, 0, ctypes.byref(rect), 0
            )
            if ok:
                return rect.left, rect.top, rect.right, rect.bottom
        except Exception:
            pass

        return 0, 0, screen_width, screen_height

    def _window_geometry(self, requested_width: int | None = None) -> tuple[int, int, int, int]:
        """Calcula a janela usando 90% da altura útil e preservando 100 px abaixo."""
        left, top, right, bottom = self._get_windows_work_area()
        work_width = max(1, right - left)
        work_height = max(1, bottom - top)

        bottom_gap = 100
        available_height = max(1, work_height - bottom_gap)
        height = max(500, int(available_height * 0.95))
        height = min(height, available_height)

        minimum_width = SALE_PANEL_WIDTH + HISTORY_PANEL_MIN_WIDTH + 70
        target_width = requested_width or int(work_width * 0.96)
        width = max(target_width, minimum_width)
        width = min(width, int(work_width * 0.94))

        x = left + max(0, (work_width - width) // 2)
        # Mantém exatamente a folga inferior definida e usa o espaço restante para cima.
        y = top + max(0, work_height - bottom_gap - height)
        return width, height, x, y

    def _configure_window_size(self) -> None:
        """Define e memoriza a geometria antes de montar a interface."""
        width, height, x, y = self._window_geometry()
        self._initial_geometry = f"{width}x{height}+{x}+{y}"
        self.root.geometry(self._initial_geometry)

    def _show_centered_window(self) -> None:
        """Exibe a janela sem forçar um segundo cálculo completo do layout.

        A geometria já foi definida em _configure_window_size(). Evitamos aqui
        update_idletasks(), winfo_reqwidth() e uma segunda chamada de geometry(),
        que atrasavam a primeira exibição da janela.
        """
        self.root.deiconify()
        self.root.lift()
        self.root.after_idle(self.card_entry.focus_set)

    def _configure_style(self) -> None:
        style = tb.Style()
        style.configure("Title.TLabel", font=("Segoe UI", 17, "bold"))
        style.configure("SubTitle.TLabel", font=("Segoe UI", 10))
        style.configure("SectionTitle.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("Value.TLabel", font=("Segoe UI", 12, "bold"))
        style.configure("FooterMetric.TLabel", font=("Segoe UI", 9, "bold"))
        style.configure("FooterCaption.TLabel", font=("Segoe UI", 8))
        style.configure("TLabelframe.Label", font=("Segoe UI", 9, "bold"))
        style.configure("Treeview", rowheight=28, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

    def _build_ui(self) -> None:
        """Monta a interface em três zonas: cabeçalho, venda atual e histórico."""
        self.container = tb.Frame(self.root, padding=14)
        self.container.pack(fill=BOTH, expand=True)

        self._build_header()
        self._startup_mark("_build_header")

        self.workspace = tb.Frame(self.container)
        self.workspace.pack(fill=BOTH, expand=True)

        self.content_frame = tb.Frame(self.workspace, width=SALE_PANEL_WIDTH)
        self.content_frame.pack(side=LEFT, anchor=NW, fill=Y, expand=False)
        self.content_frame.pack_propagate(False)

        self._build_sale_details()
        self._startup_mark("_build_sale_details")
        self._build_form()
        self._startup_mark("_build_form")
        # O resumo é criado antes da tabela e fixado no rodapé. Assim ele sempre
        # reserva sua altura, mesmo em notebooks com pouca resolução vertical.
        self._build_footer()
        self._startup_mark("_build_footer")
        self._build_table()
        self._startup_mark("_build_table")
        self._build_sales_history_panel()
        self._startup_mark("_build_sales_history_panel")
        self._refresh_sales_history()
        self._startup_mark("_refresh_sales_history")

    def _build_sales_history_panel(self) -> None:
        """Painel lateral dedicado somente ao histórico e suas ações."""
        panel = tb.Labelframe(
            self.workspace,
            text=" Histórico de vendas ",
            padding=10,
            bootstyle="secondary",
        )
        panel.pack(side=LEFT, anchor=NW, fill=BOTH, expand=True, padx=(12, 0))

        heading = tb.Frame(panel)
        heading.pack(fill=X, pady=(0, 8))
        heading_text = tb.Frame(heading)
        heading_text.pack(side=LEFT, fill=X, expand=True)
        tb.Label(heading_text, text="Vendas salvas", style="SectionTitle.TLabel").pack(anchor=W)
        tb.Label(
            heading_text,
            text="Pesquise, abra ou exporte vendas anteriores.",
            bootstyle="secondary",
            font=("Segoe UI", 8),
        ).pack(anchor=W, pady=(1, 0))
        tb.Button(
            heading,
            text="Nova venda",
            bootstyle="primary-outline",
            command=self.new_sale_group,
            width=12,
        ).pack(side=RIGHT)

        # Busca + filtros rápidos por status. A visualização principal é sempre
        # "Abertas", para deixar o histórico operacional focado no que ainda
        # precisa de atenção.
        filter_box = tb.Frame(panel)
        filter_box.pack(fill=X, pady=(0, 6))

        search_box = tb.Frame(filter_box)
        search_box.pack(fill=X)
        tb.Label(
            search_box,
            text="Buscar",
            bootstyle="secondary",
            font=("Segoe UI", 8, "bold"),
        ).pack(side=LEFT, padx=(0, 6))
        self.history_filter_entry = tb.Entry(search_box, textvariable=self.history_filter_var)
        self.history_filter_entry.pack(side=LEFT, fill=X, expand=True, padx=(0, 6))
        tb.Button(
            search_box,
            text="Limpar",
            bootstyle="secondary-outline",
            command=self.clear_history_filter,
            width=7,
        ).pack(side=LEFT)

        status_filter_bar = tb.Frame(filter_box)
        status_filter_bar.pack(fill=X, pady=(7, 0))
        tb.Label(
            status_filter_bar,
            text="Status",
            bootstyle="secondary",
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor=W, pady=(0, 3))
        filter_buttons = tb.Frame(status_filter_bar)
        filter_buttons.pack(fill=X)
        for column in range(3):
            filter_buttons.grid_columnconfigure(column, weight=1)

        filter_specs = (
            ("Aberta", "Abertas"),
            ("Liberado", "Separadas"),
            ("PG Recebido", "PG Recebido"),
            ("Finalizada", "Finalizadas"),
            ("Cancelada", "Canceladas"),
            ("Todos", "Todas"),
        )
        for index, (status_value, caption) in enumerate(filter_specs):
            self.history_filter_captions[status_value] = caption
            button = tb.Button(
                filter_buttons,
                text=caption,
                command=lambda value=status_value: self._set_history_status_filter(value),
                padding=(8, 3),
            )
            button.grid(row=index // 3, column=index % 3, sticky="ew", padx=(0, 5), pady=(0, 4))
            self.history_filter_buttons[status_value] = button

        self._update_history_filter_buttons()

        # Barra operacional contextual: fica imediatamente ao lado da tabela para
        # reduzir o deslocamento do mouse. Os botões mudam conforme a venda selecionada.
        self.history_action_box = tb.Labelframe(
            panel,
            text=" Ações da venda selecionada ",
            padding=(8, 6),
            bootstyle="secondary",
        )
        self.history_action_box.pack(fill=X, pady=(2, 7))

        self.history_status_label = tb.Label(
            self.history_action_box,
            text="Selecione uma venda no histórico.",
            bootstyle="secondary",
            wraplength=520,
            justify=LEFT,
            font=("Segoe UI", 8),
        )
        self.history_status_label.pack(anchor=W, fill=X, pady=(0, 6))

        primary_actions = tb.Frame(self.history_action_box)
        primary_actions.pack(fill=X, pady=(0, 5))

        self.open_sale_button = tb.Button(
            primary_actions,
            text="Abrir",
            bootstyle="info",
            command=self.open_selected_sale,
            state=DISABLED,
        )
        self.open_sale_button.pack(side=LEFT, fill=X, expand=True, padx=(0, 5))

        self.payment_received_button = tb.Button(
            primary_actions,
            text="Pagamento pendente",
            bootstyle="warning-outline",
            command=self.mark_selected_payment_received,
            state=DISABLED,
        )
        self.payment_received_button.pack(side=LEFT, fill=X, expand=True, padx=(0, 5))

        self.complete_delivery_button = tb.Button(
            primary_actions,
            text="Concluir entrega",
            bootstyle="success-outline",
            command=self.complete_selected_delivery,
            state=DISABLED,
        )
        self.complete_delivery_button.pack(side=LEFT, fill=X, expand=True)

        secondary_actions = tb.Frame(self.history_action_box)
        secondary_actions.pack(fill=X)

        self.export_sale_button = tb.Button(
            secondary_actions,
            text="Exportar",
            bootstyle="primary-outline",
            command=self.export_selected_sale_excel,
            state=DISABLED,
        )
        self.export_sale_button.pack(side=LEFT, fill=X, expand=True, padx=(0, 5))

        self.duplicate_sale_button = tb.Button(
            secondary_actions,
            text="Duplicar",
            bootstyle="secondary-outline",
            command=self.duplicate_selected_sale,
            state=DISABLED,
        )
        self.duplicate_sale_button.pack(side=LEFT, fill=X, expand=True, padx=(0, 5))

        self.delete_sale_button = tb.Button(
            secondary_actions,
            text="Excluir",
            bootstyle="danger-outline",
            command=self.delete_selected_sale,
            state=DISABLED,
        )
        self.delete_sale_button.pack(side=LEFT, fill=X, expand=True)

        history_columns = [
            {"text": "Venda", "stretch": True, "width": 168},
            {"text": "Cliente", "stretch": True, "width": 118},
            {"text": "Telefone", "stretch": False, "width": 118},
            {"text": "Status", "stretch": False, "width": 82},
            {"text": "Itens", "stretch": False, "width": 52},
            {"text": "Total", "stretch": False, "width": 96},
            {"text": "Entrega", "stretch": False, "width": 128},
            {"text": "Local", "stretch": False, "width": 108},
            {"text": "Pagamento", "stretch": False, "width": 118},
            {"text": "Pago em", "stretch": False, "width": 128},
            {"text": "Ações", "stretch": False, "width": 210},
        ]
        self.history_table = Tableview(
            master=panel,
            coldata=history_columns,
            rowdata=[],
            paginated=False,
            searchable=False,
            bootstyle="secondary",
            stripecolor=("white", "#f8f9fa"),
        )
        self.history_table.pack(fill=BOTH, expand=True)
        self.history_tree = self.history_table.view
        self.history_tree.configure(selectmode="browse")
        self._bind_table_keyboard_navigation(self.history_tree)

        history_tree_columns = list(self.history_tree["columns"])
        self.history_actions_column = history_tree_columns[-1]
        self._history_column_captions = [column["text"] for column in history_columns]
        self.history_tree.bind("<Button-1>", self._on_history_heading_click, add="+")
        for index, column in enumerate(history_tree_columns):
            anchor = E if index == 5 else CENTER if index in {2, 3, 4, 6, 7, 8, 9, 10} else W
            self.history_tree.column(column, anchor=anchor, stretch=(index in {0, 1}))
            self.history_tree.heading(column, anchor="center", command="")

        # Cada status tem uma identidade visual própria no histórico.
        # Usamos fundos suaves para facilitar a leitura sem competir com a tabela
        # principal da venda.
        self.history_tree.tag_configure(
            "Aberta",
            background="#EAF4FF",
            foreground="#174A7E",
        )
        self.history_tree.tag_configure(
            "Liberado",
            background="#FFF6D8",
            foreground="#7A5B00",
        )
        self.history_tree.tag_configure(
            "Finalizada",
            background="#EAF8EE",
            foreground="#25613A",
        )
        self.history_tree.tag_configure(
            "Cancelada",
            background="#FDECEC",
            foreground="#8B2F2F",
        )

        # Ações rápidas embutidas visualmente na linha selecionada.
        # O Treeview não suporta widgets nativamente dentro de células; usamos
        # um pequeno frame sobre a célula reservada "Ações" da linha ativa.
        self.history_inline_actions = tb.Frame(self.history_tree)
        self.history_inline_payment_button = tb.Button(
            self.history_inline_actions,
            text="",
            bootstyle="warning",
            command=self.mark_selected_payment_received,
            padding=(5, 1),
        )
        self.history_inline_payment_button.pack(
            side=LEFT, fill=BOTH, expand=True, padx=(0, 3)
        )

        self.history_inline_delivery_button = tb.Button(
            self.history_inline_actions,
            text="",
            bootstyle="success",
            command=self.complete_selected_delivery,
            padding=(5, 1),
        )
        self.history_inline_delivery_button.pack(
            side=LEFT, fill=BOTH, expand=True
        )
        self.history_inline_actions.place_forget()
        self.history_tree.bind("<<TreeviewSelect>>", self.on_history_selected)
        self.history_tree.bind("<Double-1>", self._on_history_double_click)
        self.history_tree.bind("<Button-3>", self._on_history_right_click)
        self.history_tree.bind("<Configure>", self._schedule_history_inline_actions, add="+")
        self.history_tree.bind("<MouseWheel>", self._schedule_history_inline_actions, add="+")
        self.history_tree.bind("<Button-4>", self._schedule_history_inline_actions, add="+")
        self.history_tree.bind("<Button-5>", self._schedule_history_inline_actions, add="+")
        self.history_tree.bind("<ButtonRelease-1>", self._schedule_history_inline_actions, add="+")
        self.history_tree.bind("<KeyRelease>", self._schedule_history_inline_actions, add="+")
        self.history_tree.bind("<Return>", lambda _event: self.open_selected_sale())
        self.history_tree.bind("<Key-p>", lambda _event: self.mark_selected_payment_received())
        self.history_tree.bind("<Key-P>", lambda _event: self.mark_selected_payment_received())
        self.history_tree.bind("<Key-f>", lambda _event: self.complete_selected_delivery())
        self.history_tree.bind("<Key-F>", lambda _event: self.complete_selected_delivery())

        self.history_context_menu = tk.Menu(self.root, tearoff=False)
        self.history_context_menu.add_command(label="Abrir venda", command=self.open_selected_sale)
        self.history_context_menu.add_command(label="Detalhes", command=self.show_selected_sale_details)
        self.history_context_menu.add_separator()
        self.history_context_menu.add_command(
            label="Marcar pagamento recebido",
            command=self.mark_selected_payment_received,
        )
        self.history_context_menu.add_command(
            label="Concluir entrega",
            command=self.complete_selected_delivery,
        )
        self.history_context_menu.add_separator()
        self.history_context_menu.add_command(label="Exportar", command=self.export_selected_sale_excel)
        self.history_context_menu.add_command(label="Duplicar", command=self.duplicate_selected_sale)
        self.history_context_menu.add_command(label="Excluir", command=self.delete_selected_sale)


    def _on_history_heading_click(self, event):
        if self.history_tree.identify_region(event.x, event.y) != "heading":
            return
        column = self.history_tree.identify_column(event.x)
        if not column or column == "#0":
            return "break"
        index = int(column[1:]) - 1
        if index >= len(self._history_column_captions) - 1:
            return "break"
        if self._history_sort_column == index:
            self._history_sort_reverse = not self._history_sort_reverse
        else:
            self._history_sort_column = index
            self._history_sort_reverse = False
        self._apply_history_sort()
        return "break"

    def _history_sort_key(self, sale_id: str, column: int):
        summary = self.sales_by_id[sale_id]
        if column == 4:
            return summary.item_count
        if column == 5:
            return self._history_net_totals.get(sale_id, summary.net_total)
        if column in {6, 9}:
            raw = summary.completed_at if column == 6 else summary.paid_at
            return (raw or "").replace("T", " ")
        values = self.history_tree.item(sale_id, "values")
        return _customer_search_key(str(values[column]))

    def _apply_history_sort(self) -> None:
        column = self._history_sort_column
        if column is None:
            return
        rows = list(self.history_tree.get_children())
        rows.sort(key=lambda sale_id: self._history_sort_key(sale_id, column),
                  reverse=self._history_sort_reverse)
        for position, sale_id in enumerate(rows):
            self.history_tree.move(sale_id, "", position)
        for index, tree_column in enumerate(self.history_tree["columns"]):
            caption = self._history_column_captions[index]
            if index == column:
                caption += " ▼" if self._history_sort_reverse else " ▲"
            self.history_tree.heading(tree_column, text=caption, command="")
        self._schedule_history_inline_actions()

    def _build_header(self) -> None:
        """Cabeçalho global da aplicação, fora dos painéis de trabalho."""
        header = tb.Frame(self.container)
        header.pack(fill=X, pady=(0, 6))

        title_box = tb.Frame(header)
        title_box.pack(side=LEFT, fill=X, expand=True)
        tb.Label(title_box, text="Vendas Pokémon Cards", style="Title.TLabel").pack(anchor=W)

        right = tb.Frame(header)
        right.pack(side=RIGHT, anchor=NE)
        tb.Label(
            right,
            text="Ctrl+N  Nova   •   Ctrl+S  Salvar   •   Ctrl+E  Exportar",
            bootstyle="secondary",
            font=("Segoe UI", 8),
        ).pack(anchor=E)
        self.status_label = tb.Label(
            right,
            text="Venda em aberto",
            bootstyle="info",
            font=("Segoe UI", 9, "bold"),
        )
        self.status_label.pack(anchor=E, pady=(4, 0))

        quick_actions = tb.Frame(self.container)
        quick_actions.pack(fill=X, pady=(0, 8))
        tb.Button(
            quick_actions,
            text="Catálogo",
            bootstyle="secondary-outline",
            command=self.open_catalog_window,
            width=12,
        ).pack(side=LEFT, padx=(0, 6))
        tb.Button(
            quick_actions,
            text="Estoque",
            bootstyle="primary-outline",
            command=self.open_stock_window,
            width=12,
        ).pack(side=LEFT)

        tb.Separator(self.container).pack(fill=X, pady=(0, 6))


    def open_stock_window(self) -> None:
        """Abre a janela de estoque e evita múltiplas instâncias."""
        if getattr(self, "stock_window_toplevel", None) is not None:
            try:
                if self.stock_window_toplevel.winfo_exists():
                    self.stock_window_toplevel.deiconify()
                    self.stock_window_toplevel.lift()
                    self.stock_window_toplevel.focus_force()
                    return
            except tk.TclError:
                self.stock_window_toplevel = None
                self.stock_window_instance = None

        win = tk.Toplevel(self.root)
        self.stock_window_toplevel = win

        # Mantém o Estoque associado à janela de Vendas e garante que ele
        # apareça na frente já no primeiro clique. O StockWindow monta a UI
        # inicialmente oculto para evitar piscar, por isso reforçamos o foco
        # somente depois que a construção terminou.
        try:
            win.transient(self.root)
        except tk.TclError:
            pass

        self.stock_window_instance = StockWindow(win)

        def _bring_stock_to_front() -> None:
            try:
                if not win.winfo_exists():
                    return
                win.deiconify()
                win.lift()
                # Topmost temporário evita a janela nascer atrás da tela de
                # Vendas no Windows, sem mantê-la permanentemente acima.
                try:
                    win.attributes("-topmost", True)
                    win.after(120, lambda: win.attributes("-topmost", False) if win.winfo_exists() else None)
                except tk.TclError:
                    pass
                win.focus_force()
            except tk.TclError:
                pass

        self.root.after_idle(_bring_stock_to_front)

        def _close_stock() -> None:
            try:
                if win.winfo_exists():
                    win.destroy()
            except tk.TclError:
                pass
            self.stock_window_toplevel = None
            self.stock_window_instance = None

        win.protocol("WM_DELETE_WINDOW", _close_stock)

    def open_catalog_window(self) -> None:
        """Abre uma janela simples de catálogo usando o cache local TCGdex."""
        if getattr(self, "catalog_window", None) is not None:
            try:
                if self.catalog_window.winfo_exists():
                    self.catalog_window.deiconify()
                    self.catalog_window.lift()
                    self.catalog_window.focus_force()
                    return
            except tk.TclError:
                pass

        win = tk.Toplevel(self.root)
        self.catalog_window = win
        win.title("Catálogo de cartas")
        win.geometry("900x620")
        win.transient(self.root)

        container = tb.Frame(win, padding=12)
        container.pack(fill=BOTH, expand=True)

        header = tb.Frame(container)
        header.pack(fill=X, pady=(0, 6))
        tb.Label(header, text="Catálogo de cartas", style="SectionTitle.TLabel").pack(side=LEFT)
        tb.Button(
            header,
            text="Atualizar catálogo",
            bootstyle="info-outline",
            command=self.update_all_cards_cache,
        ).pack(side=RIGHT)

        search_var = tb.StringVar()
        search_row = tb.Frame(container)
        search_row.pack(fill=X, pady=(0, 8))
        tb.Label(search_row, text="Buscar", font=("Segoe UI", 8, "bold")).pack(side=LEFT, padx=(0, 6))
        search_entry = tb.Entry(search_row, textvariable=search_var)
        search_entry.pack(side=LEFT, fill=X, expand=True)

        columns = [
            {"text": "Carta", "stretch": True, "width": 260},
            {"text": "Nº", "stretch": False, "width": 90},
            {"text": "Coleção", "stretch": True, "width": 220},
            {"text": "Origem", "stretch": False, "width": 110},
        ]
        table = Tableview(
            master=container,
            coldata=columns,
            rowdata=[],
            paginated=False,
            searchable=False,
            bootstyle="secondary",
            stripecolor=("white", "#f8f9fa"),
        )
        table.pack(fill=BOTH, expand=True)
        tree = table.view
        self._bind_table_keyboard_navigation(tree)

        def refresh_catalog(_event=None):
            query = search_var.get().strip()
            for iid in tree.get_children(""):
                tree.delete(iid)
            if len(query) < 2:
                return
            suggestions = self.card_cache.search(query, limit=150)
            if not suggestions:
                suggestions = self.catalog.search(query, limit=150)
            for idx, suggestion in enumerate(suggestions):
                name, number, collection, source = self._suggestion_row_values(suggestion)
                tree.insert("", END, iid=f"catalog_{idx}", values=(name, number, collection, source))

        search_entry.bind("<KeyRelease>", refresh_catalog)
        search_entry.focus_set()

        def on_close():
            try:
                win.destroy()
            finally:
                self.catalog_window = None

        win.protocol("WM_DELETE_WINDOW", on_close)

    def _build_sale_details(self) -> None:
        """Dados da venda atual separados do histórico para reduzir ambiguidade."""
        details = tb.Labelframe(
            self.content_frame,
            text=" Venda atual ",
            padding=10,
            bootstyle="primary",
        )
        details.pack(fill=X, pady=(0, 10))

        fields = tb.Frame(details)
        fields.pack(fill=X)
        fields.grid_columnconfigure(0, weight=3)
        fields.grid_columnconfigure(1, weight=2)
        fields.grid_columnconfigure(2, weight=2)
        fields.grid_columnconfigure(3, weight=0)

        sale_box = tb.Frame(fields)
        sale_box.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        tb.Label(sale_box, text="Código da venda", font=("Segoe UI", 8, "bold")).pack(anchor=W)
        self.sale_title_entry = tb.Entry(sale_box, textvariable=self.sale_title_var, state="readonly")
        self.sale_title_entry.pack(fill=X, pady=(2, 0))

        customer_box = tb.Frame(fields)
        customer_box.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        tb.Label(customer_box, text="Cliente", font=("Segoe UI", 8, "bold")).pack(anchor=W)
        self.customer_entry = CustomerAutocompleteEntry(
            customer_box, textvariable=self.customer_var,
            on_selected=self._select_customer_contact,
        )
        self.customer_entry.pack(fill=X, pady=(2, 0))
        self._customer_name_after_id = None
        self.customer_var.trace_add("write", self._schedule_customer_name_format)

        phone_box = tb.Frame(fields)
        phone_box.grid(row=0, column=2, sticky="ew", padx=(0, 8))
        tb.Label(phone_box, text="Telefone", font=("Segoe UI", 8, "bold")).pack(anchor=W)
        self.customer_phone_entry = tb.Entry(
            phone_box,
            textvariable=self.customer_phone_var,
            validate="key",
            validatecommand=(self.root.register(self._validate_customer_phone_input), "%P"),
        )
        self.customer_phone_entry.pack(fill=X, pady=(2, 0))
        self._phone_mask_after_id = None
        self.customer_phone_var.trace_add("write", self._schedule_phone_mask)
        self.customer_phone_entry.bind("<BackSpace>", self._delete_phone_digit)
        self.customer_phone_entry.bind("<Delete>", self._delete_phone_digit)

        status_box = tb.Frame(fields)
        status_box.grid(row=0, column=3, sticky="ew")
        tb.Label(status_box, text="Status", font=("Segoe UI", 8, "bold")).pack(anchor=W)
        self.sale_status_combo = tb.Combobox(
            status_box,
            textvariable=self.sale_status_display_var,
            values=tuple(self._display_sale_status(value) for value in SALE_STATUS_OPTIONS),
            state="readonly",
            width=13,
        )
        self.sale_status_combo.pack(fill=X, pady=(2, 0))
        self.sale_status_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self.sale_status_var.set(
                "Liberado" if self.sale_status_display_var.get() == "Separado"
                else self.sale_status_display_var.get()
            ),
        )

        lower = tb.Frame(details)
        lower.pack(fill=X, pady=(8, 0))

        notes_box = tb.Frame(lower)
        notes_box.pack(side=LEFT, fill=BOTH, expand=True, padx=(0, 8))
        tb.Label(notes_box, text="Observações", font=("Segoe UI", 8, "bold")).pack(anchor=W)
        self.sale_notes_text = tk.Text(
            notes_box,
            height=1,
            wrap="word",
            font=("Segoe UI", 9),
            relief="solid",
            borderwidth=1,
        )
        self.sale_notes_text.pack(fill=X, pady=(2, 0))

        action_box = tb.Frame(lower)
        action_box.pack(side=RIGHT, anchor=SE)
        tb.Button(action_box, text="Nova", bootstyle="secondary-outline", command=self.new_sale_group, width=10).pack(side=LEFT, padx=(0, 6))
        self.save_action_box = tb.Frame(action_box)
        self.save_action_box.pack(side=LEFT, padx=(0, 6))
        self.save_new_sale_button = tb.Button(
            self.save_action_box, text="Salvar venda", bootstyle="success",
            command=self.save_new_sale, width=17,
        )
        self.save_changes_button = tb.Button(
            self.save_action_box, text="Salvar alterações", bootstyle="success",
            command=self.save_sale_changes, width=17,
        )
        self._sync_sale_save_buttons()
        self.finalize_sale_button = tb.Button(
            action_box, text="Finalizar", bootstyle="success-outline",
            command=lambda: self.set_current_sale_status("Finalizada"), width=10,
        )
        self.finalize_sale_button.pack(side=LEFT)


    def _build_form(self) -> None:
        """Formulário compacto para inclusão e edição de itens."""
        form = tb.Labelframe(
            self.content_frame,
            text=" Adicionar carta à venda ",
            padding=(10, 6),
            bootstyle="primary",
        )
        form.pack(fill=X, pady=(0, 6))
        self.item_form = form
        self.form_mode_label = tb.Label(form, text="Busque a carta, informe quantidade e preço unitário.",
                                        bootstyle="secondary", wraplength=900, font=("Segoe UI", 9))
        self.form_mode_label.pack(anchor=W, fill=X, pady=(0, 4))

        fields = tb.Frame(form)
        fields.pack(fill=X)
        fields.grid_columnconfigure(0, weight=1)

        card_box = tb.Frame(fields)
        card_box.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        tb.Label(card_box, text="Carta", font=("Segoe UI", 8, "bold")).pack(anchor=W)
        self.card_entry = tb.Entry(card_box, textvariable=self.card_text_var)
        self.card_entry.pack(fill=X, pady=(2, 0))

        qty_box = tb.Frame(fields)
        qty_box.grid(row=0, column=1, sticky="n", padx=(0, 8))
        tb.Label(qty_box, text="Quantidade", font=("Segoe UI", 8, "bold")).pack(anchor=W)
        self.quantity_entry = tb.Entry(qty_box, textvariable=self.quantity_var, width=7)
        self.quantity_entry.pack(pady=(2, 0))

        price_box = tb.Frame(fields)
        price_box.grid(row=0, column=2, sticky="n", padx=(0, 8))
        tb.Label(price_box, text="Preço unitário (R$)", font=("Segoe UI", 8, "bold")).pack(anchor=W)
        self.unit_price_entry = tb.Entry(price_box, textvariable=self.unit_price_var, width=11)
        self.unit_price_entry.pack(pady=(2, 0))

        extras_box = tb.Frame(fields)
        extras_box.grid(row=0, column=3, sticky="n", padx=(0, 8))
        tb.Label(extras_box, text="Extras", font=("Segoe UI", 8, "bold")).pack(anchor=W)
        self.extras_combo = tb.Combobox(
            extras_box,
            textvariable=self.extras_var,
            values=(
                "", "Normal", "Foil", "Reverse Foil", "Promo", "Full Art", "Full Art Especial", "Ace Spec", "Shattered Holo",
                "Altered Art", "Pokeball Foil", "Masterball Foil",
                "Secret Rare", "Alt Art", "1ª Edição", "Stamped", "Jumbo",
            ),
            width=16,
        )
        self.extras_combo.pack(pady=(2, 0))

        language_box = tb.Frame(fields)
        language_box.grid(row=0, column=4, sticky="n", padx=(0, 8))
        tb.Label(language_box, text="Idioma", font=("Segoe UI", 8, "bold")).pack(anchor=W)
        self.language_combo = tb.Combobox(
            language_box,
            textvariable=self.language_var,
            values=("PT", "EN", "JP", "ES", "FR", "DE", "IT", "KR", "ZH"),
            width=7,
            state="readonly",
        )
        self.language_combo.pack(pady=(2, 0))

        action_box = tb.Frame(form)
        action_box.pack(fill=X, pady=(6, 0))
        self.item_preview_label = tb.Label(action_box, text="Total do item: —", font=("Segoe UI", 11, "bold"))
        self.item_preview_label.pack(side=LEFT)
        buttons = tb.Frame(action_box)
        buttons.pack(side=RIGHT)
        self.add_button = tb.Button(buttons, text="Adicionar à venda", bootstyle="success",
                                    command=self._submit_item_form, width=19)
        self.add_button.pack(side=LEFT)
        self.update_item_button = tb.Button(buttons, text="Aplicar alterações", bootstyle="primary",
                                            command=self._submit_item_form, width=19, state=DISABLED)
        self.cancel_item_button = tb.Button(buttons, text="Cancelar", bootstyle="secondary-outline",
                                            command=self._cancel_item_edit, width=10)
        self._form_snapshot = self._item_form_values()
        for variable in (self.card_text_var, self.quantity_var, self.unit_price_var,
                         self.extras_var, self.language_var):
            variable.trace_add("write", self._on_item_form_change)

        self.suggestion_popup = tk.Toplevel(self.card_entry)
        self.suggestion_popup.wm_overrideredirect(True)
        self.suggestion_popup.withdraw()
        try:
            self.suggestion_popup.transient(self.root)
        except tk.TclError:
            pass

        self.suggestion_listbox = tk.Listbox(
            self.suggestion_popup,
            height=5,
            activestyle="dotbox",
            exportselection=False,
            font=("Segoe UI", 9),
            borderwidth=1,
            relief="solid",
        )
        self.suggestion_listbox.pack(fill=BOTH, expand=True)
        self.suggestion_listbox.bind("<ButtonRelease-1>", self._on_suggestion_click, add="+")
        self.suggestion_listbox.bind("<Return>", lambda _event: self._select_current_suggestion(), add="+")
        self.suggestion_listbox.bind("<Tab>", lambda _event: self._select_current_suggestion(), add="+")
        self.suggestion_listbox.bind("<Escape>", lambda _event: self._hide_suggestions(), add="+")
        self.suggestion_listbox.bind("<FocusOut>", lambda _event: self.root.after(180, self._hide_suggestions_if_focus_left), add="+")
        self.suggestion_listbox.bind("<Button-3>", self._on_suggestion_right_click, add="+")

        # Estado interno do autocomplete, sem ocupar uma linha no formulário.
        self.suggestion_status_label = tb.Label(
            form,
            text="",
            bootstyle="secondary",
            font=("Segoe UI", 8),
        )

    def _build_table(self) -> None:
        """Tabela principal com ações rápidas e estados operacionais visíveis."""
        table_frame = tb.Labelframe(
            self.content_frame,
            text=" Itens da venda ",
            padding=10,
            bootstyle="primary",
        )
        table_frame.pack(fill=BOTH, expand=True, pady=(0, 0))

        toolbar = tb.Frame(table_frame)
        toolbar.pack(fill=X, pady=(0, 7))

        left_actions = tb.Frame(toolbar)
        left_actions.pack(side=LEFT)
        tb.Label(left_actions, text="Item selecionado:", bootstyle="secondary", font=("Segoe UI", 8)).pack(side=LEFT, padx=(0, 6))
        self.edit_item_button = tb.Button(left_actions, text="Editar item", bootstyle="info-outline",
                                          command=self._edit_context_selected_item, width=12, state=DISABLED)
        self.edit_item_button.pack(side=LEFT, padx=(0, 5))
        tb.Button(left_actions, text="Ampliar tabela", bootstyle="primary-outline",
                  command=self._open_sale_items_window, width=14).pack(side=LEFT, padx=(0, 5))
        tb.Button(left_actions, text="Remover", bootstyle="danger-outline", command=self.remove_selected, width=9).pack(side=LEFT)

        status_actions = tb.Frame(toolbar)
        status_actions.pack(side=RIGHT)
        tb.Label(status_actions, text="Situação:", bootstyle="secondary", font=("Segoe UI", 8)).pack(side=LEFT, padx=(0, 6))
        tb.Button(status_actions, text="Separado", bootstyle="success-outline", command=lambda: self._set_selected_items_separation_status(ITEM_STATUS_SEPARATED), width=9).pack(side=LEFT, padx=(0, 5))
        tb.Button(status_actions, text="Não encontrado", bootstyle="danger-outline", command=lambda: self._set_selected_items_separation_status(ITEM_STATUS_NOT_FOUND), width=14).pack(side=LEFT, padx=(0, 5))
        tb.Button(status_actions, text="Pendente", bootstyle="secondary-outline", command=lambda: self._set_selected_items_separation_status(""), width=9).pack(side=LEFT)

        hint = tb.Label(
            table_frame,
            text="Duplo clique amplia a tabela · F2 ou Editar item abre a edição · Ctrl/Shift seleciona vários itens.",
            bootstyle="secondary",
            font=("Segoe UI", 8),
        )
        hint.pack(anchor=W, pady=(0, 6))

        self.table_surface = tb.Frame(table_frame)
        self.table_surface.pack(fill=BOTH, expand=True)

        self.coldata = [
            {"text": "Carta", "stretch": False, "width": TABLE_BASE_COLUMN_WIDTHS[0]},
            {"text": "Nº Carta", "stretch": False, "width": TABLE_BASE_COLUMN_WIDTHS[1]},
            {"text": "Coleção", "stretch": False, "width": TABLE_BASE_COLUMN_WIDTHS[2]},
            {"text": "Extras", "stretch": False, "width": TABLE_BASE_COLUMN_WIDTHS[3]},
            {"text": "Idioma", "stretch": False, "width": TABLE_BASE_COLUMN_WIDTHS[4]},
            {"text": "Situação", "stretch": False, "width": TABLE_BASE_COLUMN_WIDTHS[5]},
            {"text": "Qtd", "stretch": False, "width": TABLE_BASE_COLUMN_WIDTHS[6]},
            {"text": "Preço Unit.", "stretch": False, "width": TABLE_BASE_COLUMN_WIDTHS[7]},
            {"text": "Total", "stretch": False, "width": TABLE_BASE_COLUMN_WIDTHS[8]},
        ]

        self.sale_table = Tableview(
            master=self.table_surface,
            coldata=self.coldata,
            rowdata=[],
            paginated=False,
            searchable=False,
            bootstyle="primary",
            stripecolor=("white", "#f8f9fa"),
        )
        self.sale_table.pack(fill=BOTH, expand=True)
        self.tree = self.sale_table.view
        self.tree.configure(selectmode="extended")
        self._bind_table_keyboard_navigation(self.tree)

        self.tree.tag_configure("item_separated", background="#E6F4EA", foreground="#1E4620")
        self.tree.tag_configure("item_not_found", background="#FDECEC", foreground="#8A1C1C")

        self.item_context_menu = tk.Menu(self.root, tearoff=False)
        self.item_context_menu.add_command(label="Marcar como Separado", command=lambda: self._set_selected_items_separation_status(ITEM_STATUS_SEPARATED))
        self.item_context_menu.add_command(label="Marcar como Não encontrado", command=lambda: self._set_selected_items_separation_status(ITEM_STATUS_NOT_FOUND))
        self.item_context_menu.add_command(label="Marcar como Pendente", command=lambda: self._set_selected_items_separation_status(""))
        self.item_context_menu.add_separator()
        self.item_context_menu.add_command(label="Editar item", command=self._edit_context_selected_item)
        self.item_context_menu.add_command(label="Remover item", command=self.remove_selected)

        columns = list(self.tree["columns"])
        numeric_columns = {7, 8}
        center_columns = {1, 2, 4, 5, 6}
        for index, column in enumerate(columns):
            if index in numeric_columns:
                anchor = E
            elif index in center_columns:
                anchor = CENTER
            else:
                anchor = W
            min_width = TABLE_MIN_COLUMN_WIDTHS[index] if index < len(TABLE_MIN_COLUMN_WIDTHS) else 80
            base_width = TABLE_BASE_COLUMN_WIDTHS[index] if index < len(TABLE_BASE_COLUMN_WIDTHS) else min_width
            self.tree.column(column, anchor=anchor, width=base_width, minwidth=min_width, stretch=False)
            self.tree.heading(column, anchor="center")

        self._table_sort_state: dict[int, bool] = {}
        self.tree.bind("<Button-1>", self._on_table_click, add="+")
        try:
            self.tree.unbind("<Button-3>")
        except Exception:
            pass
        try:
            self.tree.unbind("<ButtonRelease-3>")
        except Exception:
            pass
        self.tree.bind("<Button-3>", self._on_sale_table_right_click)
        self.tree.bind("<ButtonRelease-3>", self._block_native_sale_table_context_menu)
        self.tree.tag_configure("item_trade", background="#EDE7F6", foreground="#512DA8")
        self.tree.bind("<<TreeviewSelect>>", self.on_sale_item_selected)
        self.table_surface.bind("<Configure>", self._fit_table_columns_to_width, add="+")
        self.root.after(80, self._fit_table_columns_to_width)

    def _bind_table_keyboard_navigation(self, tree) -> None:
        """Navega pelas linhas reais, inclusive as inseridas fora do Tableview."""
        tree.configure(takefocus=True)
        tag = f"TableKeyboard:{tree}"
        tree.bindtags((tag,) + tuple(t for t in tree.bindtags() if t != tag))
        for key in ("Up", "Down", "Home", "End", "Prior", "Next"):
            for modifier in ("", "Shift-", "Control-"):
                tree.bind_class(tag, f"<{modifier}{key}>",
                                lambda event, view=tree: self._navigate_table_rows(view, event))
        tree.bind_class(tag, "<Left>", lambda event: self._scroll_table_horizontal(tree, -1))
        tree.bind_class(tag, "<Right>", lambda event: self._scroll_table_horizontal(tree, 1))
        tree.bind_class(tag, "<Control-a>", lambda event: self._select_all_table_rows(tree))
        tree.bind_class(tag, "<Control-A>", lambda event: self._select_all_table_rows(tree))
        tree.bind("<FocusIn>", lambda event: self._focus_table_rows(tree), add="+")
        tree.bind("<Button-1>", lambda event: (
            setattr(tree, "_keyboard_selection_anchor", None), tree.focus_set()), add="+")

    def _focus_table_rows(self, tree) -> str:
        tree.focus_set()
        rows = list(tree.get_children(""))
        if rows:
            selected = tree.selection()
            current = tree.focus()
            if current not in rows:
                current = next((row for row in selected if row in rows), rows[0])
                tree.focus(current)
            if not selected:
                tree.selection_set(current)
            tree.see(current)
        return "break"

    def _navigate_table_rows(self, tree, event) -> str:
        rows = list(tree.get_children(""))
        if not rows:
            return "break"
        current = tree.focus()
        if current not in rows:
            current = next((row for row in tree.selection() if row in rows), None)
        index = rows.index(current) if current in rows else None
        key = event.keysym
        if key == "Home":
            target = 0
        elif key == "End":
            target = len(rows) - 1
        elif index is None:
            target = len(rows) - 1 if key in ("Up", "Prior") else 0
        else:
            step = 1
            if key in ("Prior", "Next"):
                # A altura visível determina o passo, sem depender do rowdata interno.
                visible = sum(bool(tree.bbox(row)) for row in rows)
                step = max(1, visible - 1)
            target = max(0, min(len(rows) - 1,
                               index + (-step if key in ("Up", "Prior") else step)))
        row = rows[target]
        extended = str(tree.cget("selectmode")) == "extended"
        if event.state & 0x0001 and extended:
            anchor = getattr(tree, "_keyboard_selection_anchor", None)
            if anchor not in rows:
                anchor = current if current in rows else row
            tree._keyboard_selection_anchor = anchor
            start, end = sorted((rows.index(anchor), target))
            tree.selection_set(rows[start:end + 1])
        elif not (event.state & 0x0004 and extended):
            tree.selection_set(row)
            tree._keyboard_selection_anchor = row
        tree.focus(row)
        tree.see(row)
        return "break"

    @staticmethod
    def _scroll_table_horizontal(tree, direction: int) -> str:
        tree.xview_scroll(direction, "units")
        return "break"

    @staticmethod
    def _select_all_table_rows(tree) -> str:
        if str(tree.cget("selectmode")) == "extended":
            tree.selection_set(tree.get_children(""))
        return "break"

    def _block_native_sale_table_context_menu(self, event: tk.Event) -> str:
        """Bloqueia qualquer menu nativo remanescente do Tableview no botão direito."""
        return "break"

    def _on_sale_table_right_click(self, event: tk.Event) -> str:
        """Seleciona a linha sob o mouse e abre o menu operacional do item."""
        row_id = self.tree.identify_row(event.y)
        if not row_id:
            return "break"

        # Clique direito trabalha sobre a linha apontada. Se ela já fizer parte
        # de uma seleção múltipla, preserva a seleção para permitir marcação em lote.
        current_selection = set(self.tree.selection())
        if row_id not in current_selection:
            self.tree.selection_set(row_id)
        self.tree.focus(row_id)
        self.tree.see(row_id)
        self.on_sale_item_selected()

        if self.item_context_menu is not None:
            try:
                self.item_context_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.item_context_menu.grab_release()
        return "break"

    def _edit_context_selected_item(self, _event=None) -> str:
        selected = self.tree.selection()
        if len(selected) != 1:
            return "break"
        self._begin_item_edit(str(selected[0]))
        return "break"

    def _bind_sale_table_expansion(self) -> None:
        # Um bindtag próprio roda antes dos eventos do Tableview/Treeview.
        tag = f"SaleItemsExpansion:{self.tree}"
        self.tree.bindtags((tag,) + tuple(t for t in self.tree.bindtags() if t != tag))
        self.tree.bind_class(tag, "<Double-Button-1>", self._on_item_double_click)

    def _on_item_double_click(self, event) -> str:
        if self.tree.identify_region(event.x, event.y) in {"heading", "separator"}:
            return "break"
        if not self._sale_items_open_pending:
            self._sale_items_open_pending = True
            item_id = self.tree.identify_row(event.y) or None
            # Abre depois que o clique termina, evitando interferência de foco.
            self.root.after_idle(lambda: self._open_sale_items_window(item_id))
        return "break"

    def _open_sale_items_window(self, item_id: str | None = None) -> None:
        self._sale_items_open_pending = False
        window = self._sale_items_window
        try:
            from app.ui.sale_items_window import SaleItemsWindow
            if window is None or not window.window.winfo_exists():
                # Conserva a referência para limpar uma janela parcialmente criada.
                window = SaleItemsWindow.__new__(SaleItemsWindow)
                self._sale_items_window = window
                window.__init__(self)
            window.show(item_id)
            window.window.update_idletasks()
            window.window.deiconify()
            window.window.lift()
            window.window.focus_force()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            self._sale_items_window = None
            if window is not None:
                try:
                    window.window.destroy()
                except (AttributeError, tk.TclError):
                    pass
            messagebox.showerror(
                "Não foi possível ampliar a tabela",
                "Verifique se sales_window.py e sale_items_window.py estão na pasta app/ui.\n"
                "Depois de substituir os arquivos, feche e abra o aplicativo.\n\n"
                f"Detalhe do erro: {type(exc).__name__}: {exc}",
                parent=self.root,
            )

    def _begin_item_edit(self, item_id: str, field: str = "quantity") -> None:
        if item_id not in self.item_by_id:
            return
        if self.selected_item_id != item_id:
            if not self._finish_pending_item("editar outra carta"):
                return
            item = self.item_by_id[item_id]
            self._hide_suggestions()
            self.selected_stock_item_id = None
            self.selected_item_id = item_id
            self.card_text_var.set(item.card.display_name)
            self.quantity_var.set(str(item.quantity))
            self.unit_price_var.set(format_brl(item.unit_price).replace("R$", "").strip())
            self.extras_var.set(item.extras or "")
            self.language_var.set(item.language or "PT")
            self._linked_card_text = self.card_text_var.get()
            self.selected_stock_item_id = item.stock_item_id
            self._form_snapshot = self._item_form_values()
            self._set_item_form_mode(item)
        self.tree.selection_set(item_id)
        self.tree.focus(item_id)
        self.tree.see(item_id)
        widget = {"quantity": self.quantity_entry, "price": self.unit_price_entry,
                  "card": self.card_entry}[field]
        widget.focus_set()
        widget.selection_range(0, END)

    def _item_form_values(self) -> tuple:
        return (self.card_text_var.get(), self.quantity_var.get(), self.unit_price_var.get(),
                self.extras_var.get(), self.language_var.get(), self.selected_stock_item_id)

    def _item_form_is_dirty(self) -> bool:
        if self.selected_item_id is None:
            return bool(self.card_text_var.get().strip())
        return self._item_form_values() != getattr(self, "_form_snapshot", ())

    def _set_item_form_mode(self, item=None) -> None:
        editing = item is not None
        self.item_form.configure(text=" Editar item da venda " if editing else " Adicionar carta à venda ",
                                 bootstyle="info" if editing else "primary")
        self.form_mode_label.configure(
            text=(f"Editando: {item.card.display_name} · Aplique para atualizar esta linha."
                  if editing else "Busque a carta, informe quantidade e preço unitário."),
            bootstyle="info" if editing else "secondary")
        for button in (self.add_button, self.update_item_button, self.cancel_item_button):
            button.pack_forget()
        if editing:
            self.update_item_button.configure(state=NORMAL)
            self.update_item_button.pack(side=LEFT, padx=(0, 6))
            self.cancel_item_button.pack(side=LEFT)
        else:
            self.update_item_button.configure(state=DISABLED)
            self.add_button.pack(side=LEFT)
        self._on_item_form_change()

    def _on_item_form_change(self, *_args) -> None:
        # Mudar apenas o foco ou selecionar o texto não desfaz o vínculo de estoque.
        if (self.selected_stock_item_id is not None
                and self.card_text_var.get() != getattr(self, "_linked_card_text", self.card_text_var.get())):
            self.selected_stock_item_id = None
        if not hasattr(self, "item_preview_label"):
            return
        try:
            quantity = self._parse_quantity(self.quantity_var.get())
            price = self._parse_unit_price(self.unit_price_var.get())
            item = self.item_by_id.get(self.selected_item_id)
            discount = item.discount_percent if item else 0.0
            total = quantity * price * (1 - discount / 100)
            text = f"Total do item: {format_brl(total)}"
            if discount:
                text += f" · desconto de {discount:g}% mantido"
        except (ValueError, OverflowError):
            text = "Total do item: — · informe quantidade e preço válidos"
        self.item_preview_label.configure(text=text)

    def _submit_item_form(self, _event=None) -> str:
        if self.selected_item_id is not None:
            self.update_selected_item()
        else:
            self.add_item()
        return "break"

    def _cancel_item_edit(self) -> None:
        self._reset_form_after_add()
        self.status_label.configure(text="Edição cancelada · o item permaneceu como estava.")

    def _on_item_escape(self, _event=None) -> str:
        if self._is_suggestion_popup_visible():
            self._hide_suggestions()
        elif self.selected_item_id is not None:
            self._cancel_item_edit()
        return "break"

    def _finish_pending_item(self, action: str) -> bool:
        if not self._item_form_is_dirty():
            return True
        answer = messagebox.askyesnocancel(
            "Alterações no item", f"Há um item no formulário. Deseja aplicá-lo antes de {action}?\n\n"
            "Sim: aplicar e continuar.\nNão: descartar o formulário.\nCancelar: continuar editando.",
            parent=self.root)
        if answer is None:
            return False
        if answer:
            self._submit_item_form()
            return not self._item_form_is_dirty()
        self._reset_form_after_add()
        return True

    def _item_row_tags(self, item) -> tuple[str, ...]:
        return ("item_trade",) if item_is_trade(item) else self._item_status_tag(item.separation_status)

    @staticmethod
    def _item_status_tag(status: str) -> tuple[str, ...]:
        if status == ITEM_STATUS_SEPARATED:
            return ("item_separated",)
        if status == ITEM_STATUS_NOT_FOUND:
            return ("item_not_found",)
        return ()

    @staticmethod
    def _item_status_display(status: str) -> str:
        return status or "Pendente"

    @staticmethod
    def _display_sale_status(status: str) -> str:
        return "Separado" if status == "Liberado" else status

    def _sync_current_sale_release_status(self) -> None:
        """Mantém Aberta/Liberado sincronizado com a separação dos itens.

        - Todos os itens separados: Aberta -> Liberado.
        - Qualquer item volta a pendente/não encontrado: Liberado -> Aberta.
        - Finalizada e Cancelada nunca são alteradas automaticamente.
        """
        current_status = self.sale_status_var.get().strip() or "Aberta"

        if current_status in {"Finalizada", "Cancelada"}:
            return

        if not self.items:
            if current_status == "Liberado":
                self.sale_status_var.set("Aberta")
            return

        all_separated = all(
            (getattr(item, "separation_status", "") or "") == ITEM_STATUS_SEPARATED
            for item in self.items
        )

        if all_separated and current_status == "Aberta":
            self.sale_status_var.set("Liberado")
        elif not all_separated and current_status == "Liberado":
            self.sale_status_var.set("Aberta")

    def _set_selected_items_separation_status(self, status: str) -> None:
        """Marca um ou vários itens selecionados e persiste a alteração."""
        selected = [str(iid) for iid in self.tree.selection()]
        if not selected:
            self.status_label.configure(text="Selecione um ou mais itens para alterar a situação")
            return

        valid_status = status if status in (ITEM_STATUS_SEPARATED, ITEM_STATUS_NOT_FOUND) else ""
        changed = 0
        for item_id in selected:
            item = self.item_by_id.get(item_id)
            if item is None:
                continue
            item.separation_status = valid_status
            self._replace_item_row(item)
            changed += 1

        if not changed:
            return

        # Atualiza imediatamente os indicadores do Resumo da venda
        # (separados / não encontrados) sem precisar reabrir a janela.
        self._update_totals()
        previous_sale_status = self.sale_status_var.get().strip() or "Aberta"
        self._sync_current_sale_release_status()
        current_sale_status = self.sale_status_var.get().strip() or "Aberta"

        # Não salva automaticamente. A alteração fica pendente e será confirmada
        # quando o usuário trocar de venda, iniciar uma nova venda ou clicar em Salvar.
        if previous_sale_status != current_sale_status:
            self._update_current_history_row_status(current_sale_status)

        if self.sale_status_var.get() == "Liberado":
            self.status_label.configure(text="Todos os itens separados · venda separada")
        elif valid_status:
            self.status_label.configure(
                text=f"{changed} item(ns) marcado(s) como {valid_status}"
            )
        else:
            self.status_label.configure(text=f"Marcação removida de {changed} item(ns)")

    def _fit_table_columns_to_width(self, event: tk.Event | None = None) -> None:
        """Distribui as colunas para ocupar a largura visível da tabela.

        Mantém a tabela com largura controlada, mas evita uma faixa vazia à direita
        dentro do próprio Tableview. A coluna Carta cresce pouco; o excedente é
        distribuído entre Carta, Coleção, Preço Unit. e Total.
        """
        if not hasattr(self, "tree"):
            return

        columns = list(self.tree["columns"])
        if not columns:
            return

        if event is not None and getattr(event, "width", 0):
            available_width = int(event.width) - 2
        else:
            self.table_surface.update_idletasks()
            available_width = int(self.table_surface.winfo_width()) - 2

        if available_width <= 0:
            return

        base_widths = TABLE_BASE_COLUMN_WIDTHS[: len(columns)]
        min_widths = TABLE_MIN_COLUMN_WIDTHS[: len(columns)]
        ratios = TABLE_EXTRA_WIDTH_RATIOS[: len(columns)]

        base_total = sum(base_widths)
        min_total = sum(min_widths)

        # Em telas menores, usa a largura mínima e deixa o scrollbar horizontal trabalhar.
        if available_width <= min_total:
            widths = min_widths
        elif available_width <= base_total:
            # Reduz proporcionalmente do base até o mínimo.
            reducible = base_total - min_total
            target_reduction = base_total - available_width
            widths = []
            for base, minimum in zip(base_widths, min_widths):
                capacity = max(0, base - minimum)
                reduction = round(target_reduction * (capacity / reducible)) if reducible else 0
                widths.append(max(minimum, base - reduction))
        else:
            extra = available_width - base_total
            widths = [base + round(extra * ratio) for base, ratio in zip(base_widths, ratios)]

        # Ajusta arredondamento no último campo visível para fechar exatamente a largura.
        diff = available_width - sum(widths)
        if widths:
            widths[-1] = max(min_widths[-1], widths[-1] + diff)

        for index, column in enumerate(columns):
            width = int(widths[index]) if index < len(widths) else 100
            self.tree.column(column, width=width, minwidth=min_widths[index], stretch=False)

    def _on_table_click(self, event: tk.Event) -> str | None:
        """Ordena a Tableview ao clicar no cabeçalho."""
        region = self.tree.identify_region(event.x, event.y)
        if region != "heading":
            return None

        column_id = self.tree.identify_column(event.x)
        try:
            column_index = int(column_id.replace("#", "")) - 1
        except ValueError:
            return "break"

        self._sort_table_by_column(column_index)
        return "break"

    def _sort_table_by_column(self, column_index: int) -> None:
        """Ordenação visual simples, preservando os objetos SaleItem no item_by_id."""
        rows: list[tuple[object, str]] = []
        for iid in self.tree.get_children(""):
            values = self.tree.item(iid, "values") or ()
            key = values[column_index] if column_index < len(values) else ""
            rows.append((self._normalize_sort_value(key), iid))

        reverse = not self._table_sort_state.get(column_index, False)
        self._table_sort_state[column_index] = reverse
        rows.sort(key=lambda item: item[0], reverse=reverse)

        for position, (_key, iid) in enumerate(rows):
            self.tree.move(iid, "", position)

    @staticmethod
    def _normalize_sort_value(value: object) -> object:
        text = str(value or "").strip()
        numeric = text.replace("R$", "").replace("%", "").replace(" ", "")
        numeric = numeric.replace(".", "").replace(",", ".")
        try:
            return float(numeric)
        except ValueError:
            return text.casefold()

    def _build_footer(self) -> None:
        """Rodapé compacto e fixo, sempre visível em telas de pouca altura."""
        footer = tb.Labelframe(
            self.content_frame,
            text=" Resumo da venda ",
            padding=(9, 6),
            bootstyle="secondary",
        )
        footer.pack(side=BOTTOM, fill=X, pady=(8, 0))

        row = tb.Frame(footer)
        row.pack(fill=X)

        # Ações essenciais ficam no próprio resumo para economizar uma linha inteira.
        actions = tb.Frame(row)
        actions.pack(side=LEFT, padx=(0, 14))
        tb.Button(
            actions,
            text="Exportar Excel",
            bootstyle="primary",
            command=self.export_excel,
            width=13,
        ).pack(side=LEFT, padx=(0, 6))
        tb.Button(
            actions,
            text="Limpar itens",
            bootstyle="secondary-outline",
            command=self.clear_sale,
            width=10,
        ).pack(side=LEFT)

        metrics = tb.Frame(row)
        metrics.pack(side=LEFT, fill=X, expand=True)

        self.items_label = tb.Label(
            metrics,
            text="0 itens · 0 unidades",
            style="FooterMetric.TLabel",
            bootstyle="secondary",
        )
        self.items_label.pack(side=LEFT, padx=(0, 14))

        self.separated_label = tb.Label(
            metrics,
            text="0 separados",
            style="FooterMetric.TLabel",
            bootstyle="success",
        )
        self.separated_label.pack(side=LEFT, padx=(0, 14))

        self.not_found_label = tb.Label(
            metrics,
            text="0 não encontrados",
            style="FooterMetric.TLabel",
            bootstyle="danger",
        )
        self.not_found_label.pack(side=LEFT, padx=(0, 14))

        self.total_label = tb.Label(
            row,
            text="Total: R$ 0,00",
            style="Value.TLabel",
            bootstyle="success",
            anchor=E,
        )
        self.total_label.pack(side=RIGHT, padx=(18, 2))
        self.trade_summary_label = tb.Label(footer, style="FooterMetric.TLabel", bootstyle="secondary")
        self.trade_summary_label.pack(fill=X, pady=(5, 0))

    def _select_customer_contact(self, name: str, phone: str) -> None:
        self.customer_var.set(name)
        self.customer_phone_var.set(phone)

    def _refresh_customer_contacts(self) -> None:
        if not hasattr(self, "customer_entry"):
            return
        # Usa todas as vendas salvas, independentemente dos filtros do histórico.
        summaries = self.sales_store.list_sales(search="", status="Todos")
        contacts = _customer_contacts_from_sales(
            summaries, self._format_customer_name, self._format_customer_phone,
        )
        self.customer_entry.build(contacts)

    @staticmethod
    def _format_customer_name(value: str) -> str:
        """Capitaliza nomes, preservando espaços durante a digitação."""
        particles = {"da", "das", "de", "do", "dos", "e"}
        first_word = True

        def format_word(match: re.Match) -> str:
            nonlocal first_word
            word = match.group().lower()
            result = word.title() if first_word or word not in particles else word
            first_word = False
            return result

        return re.sub(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", format_word, str(value or ""))

    def _schedule_customer_name_format(self, *_args) -> None:
        if self._customer_name_after_id is None:
            self._customer_name_after_id = self.root.after_idle(self._apply_customer_name_format)

    def _apply_customer_name_format(self) -> None:
        self._customer_name_after_id = None
        raw = self.customer_var.get()
        formatted = self._format_customer_name(raw)
        if raw != formatted:
            cursor = self.customer_entry.index(tk.INSERT)
            self.customer_var.set(formatted)
            self.customer_entry.icursor(min(cursor, len(formatted)))

    @staticmethod
    def _customer_phone_digits(value: str) -> str:
        digits = re.sub(r"[^0-9]", "", str(value or ""))
        if len(digits) in (14, 15) and digits.startswith("0055"):
            digits = digits[4:]
        elif len(digits) in (12, 13) and digits.startswith("55"):
            digits = digits[2:]
        return digits

    @staticmethod
    def _validate_customer_phone_input(proposed: str) -> bool:
        """Aceita números brasileiros colados do WhatsApp, inclusive +55."""
        clean = re.sub(r"[\s\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]", "", proposed)
        return (
            re.fullmatch(r"[+0-9()\-]*", clean) is not None
            and len(SalesWindow._customer_phone_digits(clean)) <= 11
        )

    @staticmethod
    def _format_customer_phone(value: str) -> str:
        digits = SalesWindow._customer_phone_digits(value)[:11]
        if not digits:
            return ""
        if len(digits) < 3:
            return f"({digits}"
        split = 6 if len(digits) == 10 else 7
        result = f"({digits[:2]}){digits[2:split]}"
        if len(digits) > split:
            result += f"-{digits[split:]}"
        return result

    def _schedule_phone_mask(self, *_args) -> None:
        # Espera o Entry concluir a inserção para preservar o cursor ao editar.
        if self._phone_mask_after_id is None:
            self._phone_mask_after_id = self.root.after_idle(self._apply_phone_mask)

    def _apply_phone_mask(self) -> None:
        self._phone_mask_after_id = None
        raw = self.customer_phone_var.get()
        formatted = self._format_customer_phone(raw)
        if raw == formatted:
            return
        entry = self.customer_phone_entry
        cursor = entry.index(tk.INSERT)
        at_end = cursor >= len(raw)
        before = len(re.sub(r"[^0-9]", "", raw[:cursor]))
        self.customer_phone_var.set(formatted)
        if at_end:
            entry.icursor(END)
        else:
            positions = [i + 1 for i, char in enumerate(formatted) if char.isdigit()]
            entry.icursor(positions[min(before, len(positions)) - 1] if before and positions else 0)

    def _delete_phone_digit(self, event) -> str:
        entry = self.customer_phone_entry
        if entry.selection_present():
            entry.delete(tk.SEL_FIRST, tk.SEL_LAST)
            return "break"
        raw = self.customer_phone_var.get()
        cursor = entry.index(tk.INSERT)
        indices = (range(cursor - 1, -1, -1) if event.keysym == "BackSpace"
                   else range(cursor, len(raw)))
        for index in indices:
            if raw[index].isdigit():
                entry.delete(index, index + 1)
                entry.icursor(index if event.keysym == "BackSpace" else cursor)
                break
        return "break"

    def _bind_events(self) -> None:
        self.card_entry.bind("<KeyRelease>", self._on_card_entry_keyrelease)
        self.card_entry.bind("<Return>", self._on_card_entry_return)
        self.card_entry.bind("<Down>", self._focus_suggestion_list)
        self.card_entry.bind("<Up>", self._focus_suggestion_list)
        self.card_entry.bind("<Escape>", self._on_item_escape)
        self.card_entry.bind("<FocusOut>", lambda _event: self.root.after(180, self._hide_suggestions_if_focus_left))
        self.root.bind("<Button-1>", self._on_global_click_for_suggestion, add="+")

        self.quantity_entry.bind("<Return>", self._submit_item_form)
        self.quantity_entry.bind("<Escape>", self._on_item_escape)
        self.unit_price_entry.bind("<Return>", self._submit_item_form)
        self.unit_price_entry.bind("<Escape>", self._on_item_escape)
        self.extras_combo.bind("<Return>", self._submit_item_form)
        self.extras_combo.bind("<Escape>", self._on_item_escape)
        self.language_combo.bind("<Return>", self._submit_item_form)
        self.language_combo.bind("<Escape>", self._on_item_escape)
        self.root.bind("<Control-e>", lambda _event: self.export_excel())
        self.root.bind("<Control-s>", lambda _event: self.save_current_sale())
        self.root.bind("<Control-n>", lambda _event: self.new_sale_group())
        self.root.bind("<F6>", lambda _event: self._focus_table_rows(self.tree))
        self.root.bind("<Shift-F6>", lambda _event: self._focus_table_rows(self.history_tree))
        self._bind_sale_table_expansion()
        self.tree.bind("<F2>", self._edit_context_selected_item)
        self.tree.bind("<Return>", self._edit_context_selected_item)
        self.tree.bind("<Delete>", lambda _event: self.remove_selected(), add="+")
        self.sale_title_entry.bind("<Return>", lambda _event: self.save_current_sale())
        self.customer_entry.bind("<Return>", lambda _event: self.save_current_sale(), add="+")
        self.customer_phone_entry.bind("<Return>", lambda _event: self.save_current_sale())
        self.history_filter_entry.bind("<KeyRelease>", self._on_history_filter_keyrelease)

    def _on_card_entry_keyrelease(self, event: tk.Event) -> None:
        ignored_keys = {"Return", "Escape", "Up", "Down", "Left", "Right", "Tab", "Shift_L", "Shift_R", "Control_L", "Control_R"}
        if str(event.keysym) in ignored_keys:
            return

        # Texto editado deixa de representar a variante de estoque selecionada.
        self._on_item_form_change()

        if self.suggestion_after_id is not None:
            self.root.after_cancel(self.suggestion_after_id)
        self.suggestion_after_id = self.root.after(650, self._refresh_suggestions)

    def _on_card_entry_return(self, _event: tk.Event) -> str | None:
        if self._is_suggestion_popup_visible() and self.suggestions:
            self._select_suggestion_by_index(0)
            return "break"

        return self._submit_item_form()

    def _refresh_suggestions(self) -> None:
        """Sugere estoque e catálogo, mantendo a entrada manual disponível."""
        self.suggestion_after_id = None
        query = self.card_text_var.get().strip()

        if len(query) < 2:
            self.suggestions = []
            self.current_suggestions_query = query
            self.last_rendered_suggestion_texts = ()
            self._hide_suggestions()
            self.suggestion_status_label.configure(text="Digite pelo menos 2 caracteres ou adicione a carta manualmente")
            return

        stock_items = self.stock_store.search_available(query, limit=12)
        stock_suggestions = [self._stock_to_card_suggestion(item) for item in stock_items]
        catalog_suggestions = self.card_cache.search(query, limit=12)
        history_suggestions = self.catalog.search(query, limit=6)
        self.suggestions = self._merge_suggestions(
            stock_suggestions + catalog_suggestions + history_suggestions, limit=12
        )
        self.current_suggestions_query = query

        if self.suggestions:
            self._show_suggestions()
            self.suggestion_status_label.configure(
                text=f"{len(self.suggestions)} sugestão(ões) · também é possível digitar livremente"
            )
        else:
            self.last_rendered_suggestion_texts = ()
            self._hide_suggestions()
            self.suggestion_status_label.configure(text="Nenhuma sugestão · adicione a carta digitada")

    def _stock_to_card_suggestion(self, item: StockItem) -> CardSuggestion:
        number = item.card_number.strip()
        collection = (
            self.card_cache.resolve_collection_number(item.edition_code, item.edition_name)
            or item.edition_code.strip()
        )
        name = item.display_name.strip()
        input_text = f"{name} ({number}/{collection})" if number and collection else (f"{name} {number}" if number else name)

        detail_parts = [item.edition_name.strip()]
        if item.extras.strip():
            detail_parts.append(item.extras.strip())
        if item.language.strip():
            detail_parts.append(item.language.strip())
        detail_parts.append(f"Qtd: {item.quantity}")
        if item.price_display:
            detail_parts.append(item.price_display)

        return CardSuggestion(
            name=name,
            card_number=number,
            collection_number=collection,
            display_text=input_text,
            input_text=input_text,
            usage_count=0,
            source=f"Estoque:{item.id}",
            detail=" · ".join(part for part in detail_parts if part),
        )

    @staticmethod
    def _stock_item_id_from_suggestion(suggestion: CardSuggestion) -> int | None:
        source = str(suggestion.source or "")
        if not source.casefold().startswith("estoque:"):
            return None
        try:
            return int(source.split(":", 1)[1])
        except (IndexError, ValueError):
            return None

    def _start_api_suggestions(self, query: str) -> None:
        self.api_search_token += 1
        token = self.api_search_token
        self.api_request_in_progress = True
        self.api_query_in_flight = query
        self.pending_api_query = ""
        self.suggestion_status_label.configure(text="Autocomplete: consultando Pokémon TCG API...")
        thread = threading.Thread(target=self._fetch_api_suggestions_worker, args=(query, token), daemon=True)
        thread.start()

    def _fetch_api_suggestions_worker(self, query: str, token: int) -> None:
        try:
            result = self.api_client.search_suggestions(query, limit=8)
            self.root.after(0, lambda: self._on_api_suggestions_loaded(query, token, result.suggestions, result.from_cache))
        except PokemonTcgApiError as exc:
            # Python limpa a variável `exc` ao sair do bloco except.
            # Por isso a mensagem precisa ser capturada antes do callback do Tkinter.
            error_message = str(exc)
            self.root.after(
                0,
                lambda q=query, t=token, msg=error_message: self._on_api_suggestions_error(q, t, msg),
            )
        except Exception as exc:  # noqa: BLE001
            # Mesmo motivo acima: não capturar `exc` diretamente dentro do lambda.
            error_message = f"Erro inesperado no autocomplete: {exc}"
            self.root.after(
                0,
                lambda q=query, t=token, msg=error_message: self._on_api_suggestions_error(q, t, msg),
            )

    def _on_api_suggestions_loaded(
        self,
        query: str,
        token: int,
        api_suggestions: list[ApiCardSuggestion],
        from_cache: bool,
    ) -> None:
        if token != self.api_search_token:
            return

        self.api_request_in_progress = False
        self.api_query_in_flight = ""
        self.last_api_completed_query = query.strip()

        current_query = self.card_text_var.get().strip()
        if query.strip() != current_query:
            self._schedule_pending_api_if_needed(current_query)
            return

        local_suggestions = self.catalog.search(query, limit=3)
        converted = [self._api_to_card_suggestion(item) for item in api_suggestions]
        self.suggestions = self._merge_suggestions(local_suggestions + converted, limit=8)
        self.current_suggestions_query = query

        if self.suggestions:
            self._show_suggestions()
            source = "cache/API" if from_cache else "API"
            self.suggestion_status_label.configure(text=f"Autocomplete: {len(self.suggestions)} sugestão(ões) via {source}")
        else:
            self._hide_suggestions()
            self.suggestion_status_label.configure(text="Autocomplete: nenhuma carta encontrada na API")

        self._schedule_pending_api_if_needed(current_query)

    def _on_api_suggestions_error(self, query: str, token: int, message: str) -> None:
        if token != self.api_search_token:
            return

        self.api_request_in_progress = False
        self.api_query_in_flight = ""

        current_query = self.card_text_var.get().strip()
        if query.strip() != current_query:
            self._schedule_pending_api_if_needed(current_query)
            return

        lower_message = message.lower()
        now = time.monotonic()
        if "429" in lower_message or "too many" in lower_message or "rate" in lower_message:
            self.api_cooldown_until = now + 12
            self.suggestion_status_label.configure(text="Autocomplete: limite da API; usando histórico local por alguns segundos")
        else:
            # Erros 500/instabilidade não devem provocar retry em loop nem apagar a lista.
            self.api_failed_until_by_query[query.strip()] = now + 20
            if self.suggestions:
                self.suggestion_status_label.configure(text="Autocomplete: API instável; mantendo sugestões locais")
            else:
                self.suggestion_status_label.configure(text="Autocomplete: API instável; tente digitar o nome completo ou código")

        print(f"[autocomplete] {message}")

    def _schedule_pending_api_if_needed(self, current_query: str) -> None:
        current_query = current_query.strip()
        if len(current_query) < 3:
            return
        if self.api_request_in_progress:
            self.pending_api_query = current_query
            return
        if current_query == self.last_api_completed_query:
            return
        if current_query == self.api_query_in_flight:
            return

        # Se o texto mudou enquanto a API respondia, aguarda uma pausa curta e consulta o texto atual.
        if self.suggestion_after_id is not None:
            self.root.after_cancel(self.suggestion_after_id)
        self.suggestion_after_id = self.root.after(550, self._refresh_suggestions)

    @staticmethod
    def _api_to_card_suggestion(item: ApiCardSuggestion) -> CardSuggestion:
        detail_parts = [part for part in [item.set_name, item.series, item.rarity] if part]
        detail = " · ".join(detail_parts) if detail_parts else "Pokémon TCG API"
        return CardSuggestion(
            name=item.name,
            card_number=item.card_number,
            collection_number=item.collection_number,
            display_text=f"{item.display_text} · {detail}",
            input_text=item.display_text,
            usage_count=0,
            source="API",
            detail=detail,
        )

    @staticmethod
    def _merge_suggestions(suggestions: list[CardSuggestion], limit: int = 12) -> list[CardSuggestion]:
        seen: set[tuple[str, str, str]] = set()
        merged: list[CardSuggestion] = []
        for suggestion in suggestions:
            key = (
                suggestion.name.strip().lower(),
                suggestion.card_number.strip().lower(),
                suggestion.collection_number.strip().lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(suggestion)
            if len(merged) >= limit:
                break
        return merged

    def _show_suggestions(self) -> None:
        """Exibe sugestões em um popup/Listbox logo abaixo do campo Carta."""
        if self.suggestion_listbox is None or self.suggestion_popup is None:
            return

        if not self.suggestions:
            self._hide_suggestions()
            return

        rendered_texts = tuple(self._suggestion_render_key(item) for item in self.suggestions)
        if rendered_texts != self.last_rendered_suggestion_texts:
            self.suggestion_listbox.delete(0, END)
            for index, suggestion in enumerate(self.suggestions):
                self.suggestion_listbox.insert(END, self._suggestion_display_text(suggestion))
                color = self._suggestion_color(suggestion)
                if color:
                    try:
                        self.suggestion_listbox.itemconfigure(index, foreground=color)
                    except tk.TclError:
                        pass
            self.last_rendered_suggestion_texts = rendered_texts

        visible_count = min(len(self.suggestions), 6)
        self.suggestion_listbox.configure(height=max(1, visible_count))

        try:
            self.card_entry.update_idletasks()
            self.suggestion_popup.update_idletasks()
            x = self.card_entry.winfo_rootx()
            y = self.card_entry.winfo_rooty() + self.card_entry.winfo_height()
            width = max(self.card_entry.winfo_width(), 260)
            height = max(self.suggestion_listbox.winfo_reqheight() + 2, 24)
            self.suggestion_popup.geometry(f"{width}x{height}+{x}+{y}")
            self.suggestion_popup.deiconify()
            self.suggestion_popup.lift(self.root)
            self.suggestion_listbox.selection_clear(0, END)
            self.suggestion_listbox.activate(0)
        except tk.TclError:
            return

    def _suggestion_render_key(self, suggestion: CardSuggestion) -> str:
        return "|".join(
            [
                suggestion.name,
                suggestion.card_number,
                suggestion.collection_number,
                suggestion.detail,
                suggestion.source,
            ]
        )

    @staticmethod
    def _suggestion_tag(suggestion: CardSuggestion) -> str:
        source = (suggestion.source or "").casefold()
        if "estoque:" in source:
            return "stock"
        if "tcgdex" in source:
            return "tcgdex"
        if "api" in source:
            return "api"
        return "local"

    @staticmethod
    def _suggestion_color(suggestion: CardSuggestion) -> str:
        source = (suggestion.source or "").casefold()
        if "estoque:" in source:
            return "#1E6A3A"
        if "tcgdex" in source:
            return "#1F4E79"
        if "api" in source:
            return "#7C3AED"
        return "#1E6A3A"

    @staticmethod
    def _suggestion_display_text(suggestion: CardSuggestion) -> str:
        name = suggestion.name.strip() or suggestion.input_text.strip()
        number = suggestion.card_number.strip()
        collection = suggestion.collection_number.strip()
        source_cf = (suggestion.source or "").casefold()

        if source_cf.startswith("estoque:"):
            code = f"{number}/{collection}" if number and collection else number
            title = f"{name} ({code})" if code and code not in name else name
            detail = (suggestion.detail or "").strip()
            return " · ".join(part for part in (title, detail, "Estoque") if part)

        detail_parts = [part.strip() for part in (suggestion.detail or "").split(" · ") if part.strip()]
        source_label = "Histórico"
        display_collection = ""

        if "tcgdex" in source_cf:
            display_collection = detail_parts[0] if detail_parts else ""
            lang = ""
            if len(detail_parts) >= 2 and detail_parts[1].upper() in {"PT", "EN", "ES", "FR", "DE", "IT"}:
                lang = detail_parts[1].upper()
            source_label = f"TCGdex {lang}".strip()
        elif "api" in source_cf:
            display_collection = detail_parts[0] if detail_parts else ""
            source_label = "API"
        else:
            display_collection = detail_parts[0] if detail_parts and "histórico" not in detail_parts[0].casefold() else ""

        code = f"{number}/{collection}" if number and collection else number or collection
        title = f"{name} ({code})" if code and code not in name else name
        parts = [title]
        if display_collection:
            parts.append(display_collection)
        parts.append(source_label)
        return " · ".join(part for part in parts if part)

    @staticmethod
    def _suggestion_row_values(suggestion: CardSuggestion) -> tuple[str, str, str, str]:
        name = suggestion.name.strip() or suggestion.input_text.strip()
        number = ""
        if suggestion.card_number and suggestion.collection_number:
            number = f"{suggestion.card_number}/{suggestion.collection_number}"
        elif suggestion.card_number:
            number = suggestion.card_number
        elif suggestion.collection_number:
            number = suggestion.collection_number

        detail_parts = [part.strip() for part in (suggestion.detail or "").split(" · ") if part.strip()]
        collection = ""
        source_label = suggestion.source or "Local"
        source_casefold = source_label.casefold()

        if "tcgdex" in source_casefold:
            collection = detail_parts[0] if detail_parts else ""
            lang = ""
            if len(detail_parts) >= 2 and detail_parts[1].upper() in {"PT", "EN", "ES", "FR", "DE", "IT"}:
                lang = detail_parts[1].upper()
            source_label = f"TCGdex {lang}".strip()
        elif "api" in source_casefold:
            collection = detail_parts[0] if detail_parts else ""
            source_label = "API"
        else:
            collection = detail_parts[0] if detail_parts and "histórico" not in detail_parts[0].casefold() else ""
            source_label = "Histórico"

        return (name, number, collection, source_label)

    def _on_suggestion_list_key(self, _event: tk.Event) -> None:
        # Mantido por compatibilidade com atalhos antigos.
        return None

    def _focus_suggestion_list(self, _event: tk.Event) -> str | None:
        if not self._is_suggestion_popup_visible() or self.suggestion_listbox is None:
            self._refresh_suggestions()
            return "break"

        if self.suggestion_listbox.size() > 0:
            self.suggestion_listbox.focus_set()
            self.suggestion_listbox.selection_clear(0, END)
            self.suggestion_listbox.selection_set(0)
            self.suggestion_listbox.activate(0)
            self.suggestion_listbox.see(0)
        return "break"

    def _select_current_suggestion(self) -> str:
        if self.suggestion_listbox is None:
            return "break"

        if self.suggestion_listbox.size() <= 0:
            return "break"

        selected = self.suggestion_listbox.curselection()
        index = int(selected[0]) if selected else int(self.suggestion_listbox.index(ACTIVE) or 0)
        self._select_suggestion_by_index(index)
        return "break"

    def _on_suggestion_click(self, event: tk.Event) -> str:
        """Seleciona sugestão com clique simples, como no AutocompleteEntry enviado."""
        if self.suggestion_listbox is None or not self.suggestions:
            return "break"

        index = int(self.suggestion_listbox.nearest(event.y))
        if index < 0 or index >= len(self.suggestions):
            return "break"

        self.suggestion_listbox.selection_clear(0, END)
        self.suggestion_listbox.selection_set(index)
        self.suggestion_listbox.activate(index)
        self._select_suggestion_by_index(index)
        return "break"

    def _on_global_click_for_suggestion(self, event: tk.Event) -> None:
        """Clique fora fecha o popup de autocomplete."""
        if not self._is_suggestion_popup_visible():
            return
        if event.widget is self.card_entry or event.widget is self.suggestion_listbox:
            return
        self._hide_suggestions()

    def _on_suggestion_right_click(self, event: tk.Event) -> str:
        """Menu contextual do autocomplete para limpar itens do histórico local."""
        if self.suggestion_listbox is None or not self.suggestions:
            return "break"

        index = int(self.suggestion_listbox.nearest(event.y))
        if index < 0 or index >= len(self.suggestions):
            return "break"

        self.suggestion_context_index = index
        self.suggestion_listbox.selection_clear(0, END)
        self.suggestion_listbox.selection_set(index)
        self.suggestion_listbox.activate(index)

        suggestion = self.suggestions[index]
        menu = tk.Menu(self.root, tearoff=0)
        if self._is_local_history_suggestion(suggestion):
            menu.add_command(
                label="Remover do histórico",
                command=lambda idx=index: self._remove_history_suggestion(idx),
            )
        else:
            menu.add_command(
                label="Só itens do histórico local podem ser removidos",
                state=DISABLED,
            )

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    @staticmethod
    def _is_local_history_suggestion(suggestion: CardSuggestion) -> bool:
        source = (suggestion.source or "").casefold()
        detail = (suggestion.detail or "").casefold()
        return "tcgdex" not in source and "api" not in source and "histórico" in detail

    def _remove_history_suggestion(self, index: int) -> None:
        if index < 0 or index >= len(self.suggestions):
            return

        suggestion = self.suggestions[index]
        if not self._is_local_history_suggestion(suggestion):
            return

        removed = self.catalog.delete_card(
            name=suggestion.name,
            card_number=suggestion.card_number,
            collection_number=suggestion.collection_number,
            display_text=suggestion.input_text or suggestion.display_text,
        )
        if removed <= 0:
            self.suggestion_status_label.configure(text="Autocomplete: item do histórico não encontrado")
            return

        self.suggestions.pop(index)
        self.last_rendered_suggestion_texts = ()
        if self.suggestions:
            self._show_suggestions()
        else:
            self._hide_suggestions()
        self.suggestion_status_label.configure(text="Autocomplete: item removido do histórico local")

    def _select_suggestion_by_index(self, index: int) -> None:
        if not self.suggestions:
            return

        index = max(0, min(index, len(self.suggestions) - 1))
        suggestion = self.suggestions[index]
        self.card_text_var.set(suggestion.input_text)

        stock_item_id = self._stock_item_id_from_suggestion(suggestion)
        self._linked_card_text = self.card_text_var.get()
        self.selected_stock_item_id = stock_item_id
        if stock_item_id is not None:
            stock_item = self.stock_store.get_item(stock_item_id)
            if stock_item is not None:
                self.extras_var.set(stock_item.extras or "")
                self.language_var.set(stock_item.language or "PT")
                if stock_item.unit_price_decimal is not None:
                    self.unit_price_var.set(f"{stock_item.unit_price_decimal:.2f}".replace(".", ","))
                else:
                    self.unit_price_var.set("0,00")
                self.suggestion_status_label.configure(
                    text=f"Estoque selecionado · disponível: {stock_item.quantity} unidade(s)"
                )

        self._hide_suggestions()
        self.quantity_entry.focus_set()
        self.quantity_entry.selection_range(0, END)

    def _hide_suggestions_if_focus_left(self) -> None:
        try:
            focused = self.root.focus_get()
        except (KeyError, tk.TclError):
            self._hide_suggestions()
            return

        if focused is self.card_entry or focused is self.suggestion_listbox:
            return
        self._hide_suggestions()

    def _hide_suggestions(self) -> None:
        try:
            if self.suggestion_popup is not None and self.suggestion_popup.winfo_exists():
                self.suggestion_popup.withdraw()
        except tk.TclError:
            pass
        self.last_rendered_suggestion_texts = ()

    def _is_suggestion_popup_visible(self) -> bool:
        try:
            return (
                self.suggestion_popup is not None
                and self.suggestion_popup.winfo_exists()
                and self.suggestion_popup.state() == "normal"
            )
        except tk.TclError:
            return False

    # Cache local TCGdex
    def update_all_cards_cache(self) -> None:
        if self.cache_update_in_progress:
            return

        confirm = messagebox.askyesno(
            "Atualizar cartas",
            "Baixar/atualizar o cache local de cartas do TCGdex?\n\n"
            "Isso pode demorar alguns minutos na primeira vez, mas depois o autocomplete fica local e rápido.",
        )
        if not confirm:
            return

        self.cache_update_in_progress = True
        if hasattr(self, "update_cache_button"):
            self.update_cache_button.configure(state=DISABLED)
        if hasattr(self, "cache_status_label"):
            self.cache_status_label.configure(text="Atualizado em: atualizando...")
        self.suggestion_status_label.configure(text="Autocomplete: cache em atualização")

        thread = threading.Thread(target=self._update_all_cards_cache_worker, daemon=True)
        thread.start()

    def _update_all_cards_cache_worker(self) -> None:
        try:
            result = self.card_cache.update_all_cards(
                progress_callback=lambda message: self.root.after(
                    0,
                    lambda _msg=message: self._set_cache_status_message("Atualizado em: atualizando..."),
                )
            )
        except CardCacheUpdateError as exc:
            self.root.after(0, lambda msg=str(exc): self._on_cache_update_finished(None, msg))
        except Exception as exc:  # noqa: BLE001
            self.root.after(0, lambda msg=f"Erro inesperado ao atualizar cache: {exc}": self._on_cache_update_finished(None, msg))
        else:
            self.root.after(0, lambda res=result: self._on_cache_update_finished(res, None))

    def _on_cache_update_finished(
        self,
        result: CardCacheUpdateResult | None,
        error_message: str | None,
    ) -> None:
        self.cache_update_in_progress = False
        if hasattr(self, "update_cache_button"):
            self.update_cache_button.configure(state=NORMAL)

        if error_message:
            self._set_cache_status_message("Atualizado em: erro na atualização")
            self.suggestion_status_label.configure(text="Autocomplete: cache não atualizado")
            messagebox.showerror("Atualizar cartas", error_message)
            return

        self._refresh_cache_status()
        if result is not None:
            self.suggestion_status_label.configure(text="Autocomplete: cache local atualizado")
            messagebox.showinfo(
                "Atualizar cartas",
                f"Cache atualizado com sucesso.\n\n"
                f"Cartas: {result.card_count}\n"
                f"Coleções: {result.set_count}\n"
                f"Idiomas: {result.languages or '-'}",
            )

    @staticmethod
    def _format_cache_updated_at_brasilia(value: str) -> str:
        """Formata o horário salvo no cache para uma linha curta de exibição.

        A partir da V34 o cache grava diretamente em horário de Brasília (UTC-3).
        Mantemos compatibilidade com caches antigos que armazenavam o mesmo
        formato sem informação explícita de fuso.
        """
        raw = (value or "").strip()
        if not raw:
            return "—"
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(raw[:19], fmt)
                return dt.strftime("%d/%m/%Y %H:%M")
            except ValueError:
                continue
        return raw

    def _start_deferred_cache_status_refresh(self) -> None:
        """Consulta o status do cache em background sem atrasar a abertura."""
        if not hasattr(self, "cache_status_label"):
            return

        started_at = time.perf_counter()

        def worker() -> None:
            try:
                stats = self.card_cache.get_stats()
                error = None
            except Exception as exc:  # noqa: BLE001
                stats = None
                error = exc

            elapsed = time.perf_counter() - started_at

            def apply_result() -> None:
                try:
                    if not self.root.winfo_exists():
                        return
                except tk.TclError:
                    return

                if error is not None:
                    self.cache_status_label.configure(text="Atualizado em: indisponível")
                    print(f"[startup] cache status diferido          {elapsed:7.3f}s (erro: {error})")
                    return

                if stats is None or stats.card_count <= 0:
                    self.cache_status_label.configure(text="Atualizado em: —")
                else:
                    updated = self._format_cache_updated_at_brasilia(stats.updated_at)
                    self.cache_status_label.configure(text=f"Atualizado em: {updated} (Brasília)")

                print(f"[startup] cache status diferido          {elapsed:7.3f}s (não bloqueou abertura)")

            try:
                self.root.after(0, apply_result)
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _refresh_cache_status(self) -> None:
        """Atualização síncrona usada após operações explícitas no catálogo."""
        if not hasattr(self, "cache_status_label"):
            return
        try:
            stats = self.card_cache.get_stats()
        except Exception:  # noqa: BLE001
            self.cache_status_label.configure(text="Atualizado em: indisponível")
            return

        if stats.card_count <= 0:
            self.cache_status_label.configure(text="Atualizado em: —")
            return

        updated = self._format_cache_updated_at_brasilia(stats.updated_at)
        self.cache_status_label.configure(text=f"Atualizado em: {updated} (Brasília)")

    def _set_cache_status_message(self, message: str) -> None:
        if hasattr(self, "cache_status_label"):
            self.cache_status_label.configure(text=message)

    # Sales history / groups
    def new_sale_group(self) -> None:
        if not self._finish_pending_item("iniciar uma nova venda"):
            return
        if self._current_sale_has_unsaved_changes():
            answer = messagebox.askyesnocancel(
                "Nova venda",
                "Existem alterações não salvas. Deseja salvar antes de iniciar uma nova venda?",
            )
            if answer is None:
                return
            if answer:
                if not self.save_current_sale(show_message=False):
                    return
            else:
                if not self._restore_stock_to_saved_sale_state():
                    return
                self._refresh_sales_history()

        self.current_sale_id = None
        self._sync_sale_save_buttons()
        self.sale_title_var.set(self.sales_store.generate_sale_code())
        self.customer_var.set("")
        self.customer_phone_var.set("")
        self.sale_status_var.set("Aberta")
        self._set_sale_notes("")
        self._clear_current_items()
        self._reset_form_after_add()
        self.status_label.configure(text="Nova venda em aberto")
        self.history_status_label.configure(text="Nova venda. Preencha os dados e clique em Salvar.")
        self.card_entry.focus_set()

    def _sync_sale_save_buttons(self) -> None:
        """Mostra a ação correspondente ao primeiro cadastro ou à edição."""
        if not hasattr(self, "save_new_sale_button"):
            return
        is_new = self.current_sale_id is None
        self.save_new_sale_button.configure(state=NORMAL if is_new else DISABLED)
        self.save_changes_button.configure(state=DISABLED if is_new else NORMAL)
        self.save_new_sale_button.pack_forget()
        self.save_changes_button.pack_forget()
        button = self.save_new_sale_button if is_new else self.save_changes_button
        button.pack(side=LEFT)

    def save_new_sale(self) -> bool:
        if self.current_sale_id is not None:
            return False
        return self.save_current_sale()

    def save_sale_changes(self) -> bool:
        if self.current_sale_id is None:
            return False
        return self.save_current_sale()

    def save_current_sale(self, show_message: bool = True) -> bool:
        is_new = self.current_sale_id is None
        action = "salvar a venda" if is_new else "salvar as alterações da venda"
        if not self._finish_pending_item(action):
            return False
        if not is_new and self.sales_store.get_sale(self.current_sale_id) is None:
            messagebox.showerror("Salvar alterações", "Esta venda não está mais no histórico. Abra outra venda ou inicie uma nova.")
            return False

        title = self.sale_title_var.get().strip() or self._default_sale_title()
        status = self.sale_status_var.get().strip() or "Aberta"
        try:
            sale_id = self.sales_store.save_sale(
                sale_id=self.current_sale_id,
                title=title,
                customer_name=self._format_customer_name(self.customer_var.get()).strip(),
                customer_phone=self._format_customer_phone(self.customer_phone_var.get()),
                items=self.items,
                status=status,
                notes=self._get_sale_notes(),
            )
        except Exception as exc:
            messagebox.showerror(
                "Salvar venda" if is_new else "Salvar alterações",
                f"Não foi possível gravar a venda: {exc}",
            )
            return False
        self.current_sale_id = sale_id
        self.sale_title_var.set(title)
        saved_sale = self.sales_store.get_sale(sale_id)
        status = saved_sale.status if saved_sale else status
        self.sale_status_var.set(status)
        self.status_label.configure(text=f"{self._display_sale_status(status)}: {title}")
        self._sync_sale_save_buttons()
        self.history_status_label.configure(
            text="Venda salva no histórico local." if is_new else "Alterações salvas na venda existente."
        )
        self._refresh_sales_history(select_id=sale_id)
        if show_message:
            messagebox.showinfo(
                "Salvar venda" if is_new else "Salvar alterações",
                "Venda salva no histórico." if is_new else "Alterações salvas com sucesso na mesma venda.",
            )
        return True

    def _return_selected_sale_to_separated(self) -> bool:
        """Retorna a venda finalizada clicada no histórico, sem trocar a venda em edição."""
        sale_id = self._get_selected_history_sale_id()
        if not sale_id:
            return False
        sale = self.sales_store.get_sale(sale_id)
        if sale is None or sale.status != "Finalizada":
            return False
        try:
            self.sales_store.reopen_delivery(sale.id)
        except Exception as exc:
            messagebox.showerror("Retornar para Separada", f"Não foi possível atualizar a venda: {exc}")
            return False
        if self.current_sale_id == sale.id:
            self.sale_status_var.set("Liberado")
            self.status_label.configure(text="Venda retornou como Separada.")
        self._refresh_sales_history(select_id=sale.id)
        self.history_status_label.configure(text=f"{sale.title} retornou para Separadas · aguardando entrega.")
        return True

    def set_current_sale_status(self, status: str) -> None:
        if status not in SALE_STATUS_OPTIONS:
            return
        if status == "Finalizada":
            if not self.current_sale_id:
                messagebox.showwarning("Finalizar venda", "Salve e separe a venda antes de concluir a entrega.")
                return
            if self.save_current_sale(show_message=False):
                self.complete_selected_delivery(self.current_sale_id)
            return
        previous_status = self.sale_status_var.get()
        self.sale_status_var.set(status)
        if self.save_current_sale(show_message=False):
            self.status_label.configure(text=f"Venda {self._display_sale_status(self.sale_status_var.get()).lower()}.")
        else:
            self.sale_status_var.set(previous_status)

    @staticmethod
    def _sale_item_state(item: SaleItem) -> tuple:
        """Representação estável do item usada para detectar alterações não salvas."""
        card = item.card
        return (
            str(getattr(card, "name", "") or "").strip(),
            str(getattr(card, "card_number", "") or "").strip(),
            str(getattr(card, "collection_number", "") or "").strip(),
            str(getattr(card, "raw_text", "") or "").strip(),
            int(getattr(item, "quantity", 0) or 0),
            round(float(getattr(item, "unit_price", 0.0) or 0.0), 6),
            round(float(getattr(item, "discount_percent", 0.0) or 0.0), 6),
            str(getattr(item, "extras", "") or "").strip(),
            str(getattr(item, "language", "") or "").strip(),
            str(getattr(item, "separation_status", "") or "").strip(),
            getattr(item, "stock_item_id", None),
        )

    def _update_current_history_row_status(self, status: str) -> None:
        """Atualiza apenas a linha visível do histórico, sem persistir no banco."""
        sale_id = self.current_sale_id
        if not sale_id or not hasattr(self, "history_tree"):
            return
        try:
            if not self.history_tree.exists(sale_id):
                return
            values = list(self.history_tree.item(sale_id, "values") or ())
            if len(values) >= 4:
                values[3] = self._display_sale_status(status)
                self.history_tree.item(sale_id, values=tuple(values), tags=(status,))
        except tk.TclError:
            pass

    def _restore_stock_to_saved_sale_state(self) -> bool:
        """Desfaz reservas de alterações não salvas e restaura o estoque da versão salva."""
        current_reservations = [
            (int(item.stock_item_id), int(item.quantity))
            for item in self.items
            if getattr(item, "stock_item_id", None) is not None
        ]

        saved_reservations: list[tuple[int, int]] = []
        if self.current_sale_id is not None:
            saved = self.sales_store.get_sale(self.current_sale_id)
            if saved is not None:
                saved_reservations = [
                    (int(item.stock_item_id), int(item.quantity))
                    for item in saved.items
                    if getattr(item, "stock_item_id", None) is not None
                ]

        try:
            if current_reservations:
                self.stock_store.restore_many(current_reservations)
            if saved_reservations:
                self.stock_store.reserve_many(saved_reservations)
            return True
        except StockQuantityError as exc:
            # Tenta retornar ao estado corrente se a recomposição da versão salva falhar.
            try:
                if saved_reservations:
                    self.stock_store.restore_many(saved_reservations)
                if current_reservations:
                    self.stock_store.reserve_many(current_reservations)
            except Exception:
                pass
            messagebox.showerror(
                "Estoque",
                f"Não foi possível descartar as alterações com segurança:\n{exc}",
            )
            return False

    def _current_sale_has_unsaved_changes(self) -> bool:
        """Retorna True somente quando a venda atual difere do que está salvo."""
        # Dados do cliente e observações também são conteúdo, mesmo sem cartas.
        if self.current_sale_id is None:
            return bool(self.items or self.customer_var.get().strip()
                        or self.customer_phone_var.get().strip() or self._get_sale_notes()
                        or self.sale_status_var.get().strip() not in ("", "Aberta"))

        saved = self.sales_store.get_sale(self.current_sale_id)
        if saved is None:
            return bool(self.items)

        current_title = self.sale_title_var.get().strip() or self._default_sale_title()
        current_customer = self.customer_var.get().strip()
        current_phone = self._format_customer_phone(self.customer_phone_var.get())
        current_status = self.sale_status_var.get().strip() or "Aberta"
        current_notes = self._get_sale_notes()

        if current_title != str(saved.title or "").strip():
            return True
        if current_customer != str(saved.customer_name or "").strip():
            return True
        if current_phone != self._format_customer_phone(saved.customer_phone):
            return True
        if current_status != str(saved.status or "Aberta").strip():
            return True
        if current_notes != str(saved.notes or "").strip():
            return True

        current_items = [self._sale_item_state(item) for item in self.items]
        saved_items = [self._sale_item_state(item) for item in saved.items]
        return current_items != saved_items

    def show_selected_sale_details(self) -> None:
        """Abre uma janela somente de leitura com todos os dados da venda."""
        sale_id = self._get_selected_history_sale_id()
        if not sale_id:
            messagebox.showwarning(
                "Detalhes da venda",
                "Selecione uma venda no histórico.",
                parent=self.root,
            )
            return

        sale = self.sales_store.get_sale(sale_id)
        if sale is None:
            messagebox.showerror(
                "Detalhes da venda",
                "Venda não encontrada no histórico.",
                parent=self.root,
            )
            self._refresh_sales_history()
            return

        SaleDetailsWindow(self.root, sale)

    def open_selected_sale(self) -> None:
        if not self._finish_pending_item("abrir uma venda"):
            return
        sale_id = self._get_selected_history_sale_id()
        if not sale_id:
            messagebox.showwarning("Abrir venda", "Selecione uma venda no histórico.")
            return

        if self.current_sale_id != sale_id and self._current_sale_has_unsaved_changes():
            answer = messagebox.askyesnocancel(
                "Abrir venda",
                "Deseja salvar as alterações da venda atual antes de abrir outra?",
            )
            if answer is None:
                return
            if answer:
                if not self.save_current_sale(show_message=False):
                    return
            else:
                if not self._restore_stock_to_saved_sale_state():
                    return
                self._refresh_sales_history()

        sale = self.sales_store.get_sale(sale_id)
        if not sale:
            messagebox.showerror("Abrir venda", "Venda não encontrada no histórico.")
            self._refresh_sales_history()
            return

        self.current_sale_id = sale.id
        self._sync_sale_save_buttons()
        self.sale_title_var.set(sale.title)
        self.customer_var.set(sale.customer_name)
        self.customer_phone_var.set(self._format_customer_phone(sale.customer_phone))
        self.sale_status_var.set(sale.status)
        self._set_sale_notes(sale.notes)
        self._load_items_into_current_sale(sale.items)
        self.status_label.configure(text=f"Editando: {sale.title}")
        self.history_status_label.configure(text="Venda carregada. Altere os dados ou itens e clique em Salvar alterações.")
        self._refresh_sales_history(select_id=sale.id)

    def duplicate_selected_sale(self) -> None:
        sale_id = self._get_selected_history_sale_id()
        if not sale_id:
            messagebox.showwarning("Duplicar venda", "Selecione uma venda no histórico.")
            return
        sale = self.sales_store.get_sale(sale_id)
        if sale is None:
            messagebox.showerror("Duplicar venda", "Venda não encontrada no histórico.")
            return

        reservations = [
            (int(item.stock_item_id), int(item.quantity))
            for item in sale.items
            if getattr(item, "stock_item_id", None) is not None
        ]
        reserved = False
        try:
            self.stock_store.reserve_many(reservations)
            reserved = True
            duplicated_id = self.sales_store.duplicate_sale(sale_id)
            if not duplicated_id:
                raise RuntimeError("Não foi possível duplicar a venda selecionada.")
        except (StockQuantityError, RuntimeError) as exc:
            if reserved:
                try:
                    self.stock_store.restore_many(reservations)
                except Exception:
                    pass
            messagebox.showwarning("Duplicar venda", str(exc))
            return

        self._refresh_sales_history(select_id=duplicated_id)
        self.history_status_label.configure(text="Venda duplicada · estoque reservado para a nova venda.")

    def export_selected_sale_excel(self) -> None:
        sale_id = self._get_selected_history_sale_id()
        if not sale_id:
            messagebox.showwarning("Exportar venda", "Selecione uma venda no histórico.")
            return
        sale = self.sales_store.get_sale(sale_id)
        if not sale:
            messagebox.showerror("Exportar venda", "Venda não encontrada no histórico.")
            return
        if not sale.items:
            messagebox.showwarning("Exportar venda", "A venda selecionada não possui itens.")
            return

        path = filedialog.asksaveasfilename(
            title="Exportar venda",
            defaultextension=".xlsx",
            filetypes=[("Excel", "*.xlsx")],
            initialfile=f"{self._safe_filename(sale.title)}.xlsx",
        )
        if not path:
            return

        try:
            output_path = export_sale_to_xlsx(
                sale.items,
                Path(path),
                sale_title=sale.title,
                customer_name=sale.customer_name,
                customer_phone=sale.customer_phone,
                status=sale.status,
                notes=sale.notes,
            )
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Exportar venda", f"Erro ao exportar Excel:\n{exc}")
            return
        self.history_status_label.configure(text=f"Venda exportada: {output_path.name}")
        messagebox.showinfo("Exportar venda", f"Arquivo exportado com sucesso:\n{output_path}")

    def delete_selected_sale(self) -> None:
        sale_id = self._get_selected_history_sale_id()
        if not sale_id:
            messagebox.showwarning("Excluir venda", "Selecione uma venda no histórico.")
            return

        summary = self.sales_by_id.get(sale_id)
        title = summary.title if summary else "venda selecionada"
        confirm = messagebox.askyesno("Excluir venda", f"Deseja excluir '{title}' do histórico?")
        if not confirm:
            return

        sale = self.sales_store.get_sale(sale_id)
        if sale is not None:
            reservations = [
                (int(item.stock_item_id), int(item.quantity))
                for item in sale.items
                if getattr(item, "stock_item_id", None) is not None
            ]
            if reservations:
                self.stock_store.restore_many(reservations)

        self.sales_store.delete_sale(sale_id)
        if self.current_sale_id == sale_id:
            self.current_sale_id = None
            self.sale_title_var.set(self.sales_store.generate_sale_code())
            self.customer_var.set("")
            self.sale_status_var.set("Aberta")
            self._set_sale_notes("")
            self._clear_current_items()
            self.status_label.configure(text="Venda em aberto")
        self._refresh_sales_history()
        self.history_status_label.configure(text="Venda excluída do histórico.")

    def _ask_delivery_location(self) -> str | None:
        """Solicita o local da entrega usando apenas os locais cadastrados."""
        dialog = tb.Toplevel(self.root)
        dialog.title("Local da entrega")
        dialog.resizable(False, False)
        dialog.transient(self.root)

        result: dict[str, str | None] = {"value": None}
        location_var = tb.StringVar(value="")

        frame = tb.Frame(dialog, padding=18)
        frame.pack(fill=BOTH, expand=True)

        tb.Label(
            frame,
            text="Onde a entrega foi concluída?",
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor=W, pady=(0, 10))

        combo = tb.Combobox(
            frame,
            textvariable=location_var,
            values=DELIVERY_LOCATIONS,
            state="readonly",
            width=24,
        )
        combo.pack(fill=X, pady=(0, 14))

        buttons = tb.Frame(frame)
        buttons.pack(fill=X)

        def confirm() -> None:
            value = location_var.get().strip()
            if value not in DELIVERY_LOCATIONS:
                messagebox.showwarning(
                    "Local da entrega",
                    "Selecione Domínio, Multiverso ou Countergames.",
                    parent=dialog,
                )
                return
            result["value"] = value
            dialog.destroy()

        def cancel() -> None:
            dialog.destroy()

        tb.Button(
            buttons,
            text="Cancelar",
            bootstyle="secondary-outline",
            command=cancel,
        ).pack(side=LEFT)

        tb.Button(
            buttons,
            text="Confirmar",
            bootstyle="success",
            command=confirm,
        ).pack(side=RIGHT)

        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.update_idletasks()

        width = max(dialog.winfo_reqwidth(), 330)
        height = max(dialog.winfo_reqheight(), 150)
        x = self.root.winfo_rootx() + max((self.root.winfo_width() - width) // 2, 0)
        y = self.root.winfo_rooty() + max((self.root.winfo_height() - height) // 2, 0)
        dialog.geometry(f"{width}x{height}+{x}+{y}")

        dialog.grab_set()
        combo.focus_set()
        self.root.wait_window(dialog)
        return result["value"]

    def mark_selected_payment_received(self) -> None:
        """Marca explicitamente o pagamento da venda selecionada como recebido."""
        sale_id = self._get_selected_history_sale_id()
        if not sale_id:
            messagebox.showwarning("Pagamento", "Selecione uma venda no histórico.", parent=self.root)
            return

        summary = self.sales_by_id.get(str(sale_id))
        if summary is None:
            self._refresh_sales_history()
            return

        if summary.payment_status == "Pgto recebido":
            messagebox.showinfo("Pagamento", "O pagamento desta venda já está marcado como recebido.", parent=self.root)
            return

        confirmed = messagebox.askyesno(
            "Confirmar pagamento",
            f"O pagamento da venda {summary.title} foi recebido?",
            parent=self.root,
        )
        if not confirmed:
            return

        try:
            paid_at = self.sales_store.set_payment_received(str(sale_id), True)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Pagamento", f"Não foi possível registrar o pagamento:\n{exc}", parent=self.root)
            return

        updated_sale = self.sales_store.get_sale(str(sale_id))
        if updated_sale and self.current_sale_id == updated_sale.id:
            self.sale_status_var.set(updated_sale.status)
        self._refresh_sales_history(select_id=str(sale_id))
        if hasattr(self, "payment_received_button"):
            self.payment_received_button.configure(
                text="✓ Pagamento recebido",
                bootstyle="success",
                state=DISABLED,
            )

        paid_display = self._format_history_datetime(paid_at)
        self.history_status_label.configure(
            text=f"✓ Pagamento recebido em {paid_display} · {summary.title}" + (
                " · Venda Finalizada: entrega e pagamento confirmados."
                if updated_sale and updated_sale.status == "Finalizada" else " · Aguardando entrega."
            )
        )

    def cancel_selected_payment_received(self) -> bool:
        """Corrige a marcação de pagamento sem cancelar a venda ou a entrega."""
        sale_id = self._get_selected_history_sale_id()
        if not sale_id:
            return False
        sale = self.sales_store.get_sale(str(sale_id))
        if sale is None or sale.payment_status != "Pgto recebido":
            return False
        if not messagebox.askyesno(
            "Cancelar pagamento recebido",
            f"Voltar o pagamento da venda {sale.title} para Pendente?",
            parent=self.root,
        ):
            return False
        try:
            self.sales_store.set_payment_received(str(sale_id), False)
        except Exception as exc:
            messagebox.showerror(
                "Cancelar pagamento recebido", f"Não foi possível corrigir o pagamento:\n{exc}",
                parent=self.root,
            )
            return False
        updated_sale = self.sales_store.get_sale(str(sale_id))
        if updated_sale and self.current_sale_id == updated_sale.id:
            self.sale_status_var.set(updated_sale.status)
        self._refresh_sales_history(select_id=str(sale_id))
        self.history_status_label.configure(text=f"{sale.title} · Pagamento Pendente.")
        return True

    def cancel_selected_delivery(self) -> bool:
        """Desfaz a entrega da venda clicada sem alterar o pagamento."""
        sale_id = self._get_selected_history_sale_id()
        if not sale_id:
            return False
        sale = self.sales_store.get_sale(str(sale_id))
        if sale is None or not sale.completed_at:
            return False
        if not messagebox.askyesno(
            "Cancelar entrega",
            f"Cancelar a confirmação de entrega da venda {sale.title}?\n\nO pagamento será mantido.",
            parent=self.root,
        ):
            return False
        try:
            self.sales_store.cancel_delivery(str(sale_id))
        except Exception as exc:
            messagebox.showerror("Cancelar entrega", f"Não foi possível cancelar a entrega:\n{exc}", parent=self.root)
            return False
        updated_sale = self.sales_store.get_sale(str(sale_id))
        if updated_sale and self.current_sale_id == updated_sale.id:
            self.sale_status_var.set(updated_sale.status)
        self._refresh_sales_history(select_id=str(sale_id))
        self.history_status_label.configure(text=f"{sale.title} · Entrega pendente · pagamento mantido.")
        return True

    def complete_selected_delivery(self, sale_id: str | None = None) -> None:
        """Registra a entrega independentemente da ordem do pagamento."""
        sale_id = sale_id or self._get_selected_history_sale_id()
        if not sale_id:
            messagebox.showwarning(
                "Concluir entrega",
                "Selecione uma venda separada no histórico.",
                parent=self.root,
            )
            return

        summary = self.sales_store.get_sale(str(sale_id))
        if summary is None:
            messagebox.showerror(
                "Concluir entrega",
                "Venda não encontrada no histórico.",
                parent=self.root,
            )
            self._refresh_sales_history()
            return

        if summary.completed_at:
            messagebox.showinfo("Concluir entrega", "A entrega desta venda já foi concluída.", parent=self.root)
            return
        if summary.status != "Liberado":
            messagebox.showwarning(
                "Concluir entrega",
                "Apenas vendas com status Separado podem ter a entrega concluída.",
                parent=self.root,
            )
            return

        confirmed = messagebox.askyesno(
            "Concluir entrega",
            (
                f"A entrega da venda {summary.title} foi concluída?\n\n"
                "A venda será Finalizada somente quando o pagamento também estiver recebido."
            ),
            parent=self.root,
        )
        if not confirmed:
            return

        delivery_location = self._ask_delivery_location()
        if delivery_location is None:
            return

        sale = self.sales_store.get_sale(str(sale_id))
        if sale is None:
            messagebox.showerror(
                "Concluir entrega",
                "Venda não encontrada no histórico.",
                parent=self.root,
            )
            self._refresh_sales_history()
            return

        try:
            completed_at = self.sales_store.complete_sale(sale.id, delivery_location)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(
                "Concluir entrega",
                f"Não foi possível finalizar a venda:\n{exc}",
                parent=self.root,
            )
            return

        updated_sale = self.sales_store.get_sale(sale.id)
        if self.current_sale_id == sale.id and updated_sale:
            self.sale_status_var.set(updated_sale.status)
            self.status_label.configure(text=(
                f"Finalizada: {sale.title}" if updated_sale.status == "Finalizada"
                else f"Entrega concluída · aguardando pagamento: {sale.title}"
            ))

        self._refresh_sales_history()
        completed_display = self._format_history_datetime(completed_at)
        self.history_status_label.configure(
            text=(
                f"Entrega concluída em {completed_display} · {delivery_location} · "
                f"{sale.title} · " + ("Finalizada: entrega e pagamento confirmados."
                if updated_sale and updated_sale.status == "Finalizada" else "Aguardando pagamento.")
            )
        )

    def _schedule_history_inline_actions(self, _event: object | None = None) -> None:
        """Reposiciona as ações depois que o Treeview terminar de mover/redesenhar."""
        try:
            self.root.after_idle(self._position_history_inline_actions)
        except tk.TclError:
            pass

    def _hide_history_inline_actions(self) -> None:
        if hasattr(self, "history_inline_actions"):
            self.history_inline_actions.place_forget()

    def _position_history_inline_actions(self) -> None:
        """Mostra os botões rápidos dentro da célula Ações da linha selecionada."""
        if not hasattr(self, "history_tree") or not hasattr(self, "history_inline_actions"):
            return

        sale_id = self._get_selected_history_sale_id()
        if not sale_id or not self.history_tree.exists(sale_id):
            self._hide_history_inline_actions()
            return

        summary = self.sales_by_id.get(str(sale_id))
        if summary is None:
            self._hide_history_inline_actions()
            return

        try:
            bbox = self.history_tree.bbox(sale_id, self.history_actions_column)
        except tk.TclError:
            bbox = ()

        if not bbox:
            self._hide_history_inline_actions()
            return

        x, y, width, height = bbox
        if width <= 0 or height <= 0:
            self._hide_history_inline_actions()
            return

        # Pagamento
        if summary.payment_status == "Pgto recebido":
            self.history_inline_payment_button.configure(
                text="",
                bootstyle="success-outline",
                state=DISABLED,
            )
        else:
            self.history_inline_payment_button.configure(
                text="",
                bootstyle="warning",
                state=NORMAL,
            )

        # Entrega
        if summary.status == "Liberado" and not summary.completed_at:
            self.history_inline_delivery_button.configure(
                text="",
                bootstyle="success",
                state=NORMAL,
            )
        elif summary.completed_at:
            self.history_inline_delivery_button.configure(
                text="",
                bootstyle="success-outline",
                state=DISABLED,
            )
        else:
            self.history_inline_delivery_button.configure(
                text="",
                bootstyle="secondary-outline",
                state=DISABLED,
            )

        self.history_inline_actions.place(
            x=x + 1,
            y=y + 1,
            width=max(width - 2, 1),
            height=max(height - 2, 1),
        )
        self.history_inline_actions.lift()

    def _on_history_right_click(self, event: tk.Event) -> str:
        """Abre as ações da venda exatamente onde o mouse está."""
        if not hasattr(self, "history_tree"):
            return "break"

        row_id = self.history_tree.identify_row(event.y)
        if not row_id:
            return "break"

        self.history_tree.selection_set(row_id)
        self.history_tree.focus(row_id)
        self.on_history_selected()

        summary = self.sales_by_id.get(str(row_id))
        if summary is None:
            return "break"

        # Índices: 0 Abrir, 1 Detalhes, 3 Pagamento, 4 Entrega,
        # 6 Exportar, 7 Duplicar, 8 Excluir.
        try:
            self.history_context_menu.entryconfigure(0, state=NORMAL)
            self.history_context_menu.entryconfigure(1, state=NORMAL)
            payment_received = summary.payment_status == "Pgto recebido"
            self.history_context_menu.entryconfigure(
                3, state=NORMAL,
                label="Cancelar pagamento recebido" if payment_received else "Marcar pagamento recebido",
                command=self.cancel_selected_payment_received if payment_received else self.mark_selected_payment_received,
            )
            delivery_completed = bool(summary.completed_at)
            self.history_context_menu.entryconfigure(
                4, state=NORMAL if delivery_completed or summary.status == "Liberado" else DISABLED,
                label="Cancelar entrega" if delivery_completed else "Concluir entrega",
                command=self.cancel_selected_delivery if delivery_completed else self.complete_selected_delivery,
            )
            self.history_context_menu.entryconfigure(6, state=NORMAL)
            self.history_context_menu.entryconfigure(7, state=NORMAL)
            self.history_context_menu.entryconfigure(8, state=NORMAL)
            # A opção adicional aparece somente para a venda Finalizada clicada.
            if self.history_context_menu.index("end") > 8:
                self.history_context_menu.delete(9, "end")
            if summary.status == "Finalizada":
                self.history_context_menu.add_separator()
                self.history_context_menu.add_command(
                    label="Retornar para Separada",
                    command=self._return_selected_sale_to_separated,
                )
            self.history_context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                self.history_context_menu.grab_release()
            except Exception:
                pass

        return "break"

    def _on_history_double_click(self, event: tk.Event) -> None:
        """Dois cliques sempre abrem a venda, independentemente do status."""
        if not hasattr(self, "history_tree"):
            return

        row_id = self.history_tree.identify_row(event.y)
        if not row_id:
            return

        self.history_tree.selection_set(row_id)
        self.history_tree.focus(row_id)
        self.open_selected_sale()

    def on_history_selected(self, _event: object | None = None) -> None:
        sale_id = self._get_selected_history_sale_id()
        action_buttons = (
            "open_sale_button",
            "export_sale_button",
            "duplicate_sale_button",
            "delete_sale_button",
        )

        if not sale_id:
            self._hide_history_inline_actions()
            for name in action_buttons:
                button = getattr(self, name, None)
                if button is not None:
                    button.configure(state=DISABLED)
            if hasattr(self, "complete_delivery_button"):
                self.complete_delivery_button.configure(
                    text="Concluir entrega",
                    bootstyle="success-outline",
                    state=DISABLED,
                )
            if hasattr(self, "payment_received_button"):
                self.payment_received_button.configure(
                    text="Pagamento pendente",
                    bootstyle="warning-outline",
                    state=DISABLED,
                )
            return

        summary = self.sales_by_id.get(sale_id)
        if not summary:
            return

        for name in action_buttons:
            button = getattr(self, name, None)
            if button is not None:
                button.configure(state=NORMAL)

        if hasattr(self, "complete_delivery_button"):
            if summary.status == "Liberado" and not summary.completed_at:
                self.complete_delivery_button.configure(
                    text="Concluir entrega",
                    bootstyle="success",
                    state=NORMAL,
                )
            elif summary.completed_at:
                self.complete_delivery_button.configure(
                    text="✓ Entrega concluída",
                    bootstyle="success-outline",
                    state=DISABLED,
                )
            else:
                self.complete_delivery_button.configure(
                    text="Aguardando liberação",
                    bootstyle="secondary-outline",
                    state=DISABLED,
                )

        if hasattr(self, "payment_received_button"):
            if summary.payment_status == "Pgto recebido":
                self.payment_received_button.configure(
                    text="✓ Pagamento recebido",
                    bootstyle="success",
                    state=DISABLED,
                )
            else:
                self.payment_received_button.configure(
                    text="Marcar pgto recebido",
                    bootstyle="warning",
                    state=NORMAL,
                )

        customer = f" · {summary.customer_name}" if summary.customer_name else ""
        phone = f" · {self._format_customer_phone(summary.customer_phone)}" if summary.customer_phone else ""
        payment_text = (
            "Pago"
            if summary.payment_status == "Pgto recebido"
            else "Pagamento pendente"
        )
        self.history_status_label.configure(
            text=(
                f"{summary.title}{customer}{phone}  •  {self._display_sale_status(summary.status)}  •  {payment_text}  •  "
                f"{summary.item_count} item(ns) / {summary.unit_count} un.  •  "
                f"{format_brl(self._history_net_totals.get(summary.id, summary.net_total))}"
            ),
            wraplength=0,
        )
        self._schedule_history_inline_actions()

    @staticmethod
    def _format_history_datetime(value: str) -> str:
        """Formata timestamps do histórico como DD/MM/YYYY HH:MM."""
        raw = (value or "").strip()
        if not raw:
            return "-"
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(raw[:19], fmt).strftime("%d/%m/%Y %H:%M")
            except ValueError:
                continue
        return raw

    def _refresh_sales_history(self, select_id: str | None = None) -> None:
        if not hasattr(self, "history_tree"):
            return

        self._sync_sale_save_buttons()
        self._refresh_customer_contacts()
        self._update_history_status_counts()
        self._hide_history_inline_actions()
        self.sales_by_id = {}
        for name in (
            "open_sale_button",
            "export_sale_button",
            "duplicate_sale_button",
            "delete_sale_button",
        ):
            button = getattr(self, name, None)
            if button is not None:
                button.configure(state=DISABLED)

        if hasattr(self, "complete_delivery_button"):
            self.complete_delivery_button.configure(
                text="Concluir entrega",
                bootstyle="success-outline",
                state=DISABLED,
            )
        if hasattr(self, "payment_received_button"):
            self.payment_received_button.configure(
                text="Pagamento pendente",
                bootstyle="warning-outline",
                state=DISABLED,
            )

        for item_id in self.history_tree.get_children():
            self.history_tree.delete(item_id)

        active_filter = self.history_status_filter_var.get().strip() or "Todos"
        summaries = self.sales_store.list_sales(
            search=self.history_filter_var.get().strip(),
            status="Liberado" if active_filter == "PG Recebido" else active_filter,
        )
        if active_filter == "PG Recebido":
            summaries = [summary for summary in summaries if self._awaiting_delivery_paid(summary)]
        self._history_net_totals = {}
        for summary in summaries:
            saved_sale = self.sales_store.get_sale(summary.id)
            net_total = (self._calculate_chargeable_totals(saved_sale.items).net_total
                         if saved_sale is not None else summary.net_total)
            self._history_net_totals[summary.id] = net_total
            self.sales_by_id[summary.id] = summary
            self.history_tree.insert(
                "",
                END,
                iid=summary.id,
                values=(
                    summary.title,
                    summary.customer_name or "-",
                    self._format_customer_phone(summary.customer_phone) or "-",
                    ("Entregue · PG pendente" if summary.completed_at and summary.status == "Liberado"
                     else self._display_sale_status(summary.status)),
                    summary.item_count,
                    format_brl(net_total),
                    self._format_history_datetime(summary.completed_at),
                    summary.completed_location or "-",
                    summary.payment_status or "Pendente",
                    self._format_history_datetime(summary.paid_at),
                    "",
                ),
                tags=(summary.status,),
            )

        self._apply_history_sort()
        if select_id and self.history_tree.exists(select_id):
            self.history_tree.selection_set(select_id)
            self.history_tree.see(select_id)
            self._schedule_history_inline_actions()

        active_status = self.history_status_filter_var.get().strip() or "Aberta"
        status_caption = {
            "Aberta": "abertas",
            "Liberado": "separadas",
            "PG Recebido": "pagas e separadas, aguardando entrega",
            "Finalizada": "finalizadas",
            "Cancelada": "canceladas",
            "Todos": "no total",
        }.get(active_status, active_status.lower())
        if summaries:
            self.history_status_label.configure(
                text=f"{len(summaries)} venda(s) {status_caption} exibida(s)."
            )
        else:
            self.history_status_label.configure(
                text=f"Nenhuma venda {status_caption} encontrada para o filtro atual."
            )

    @staticmethod
    def _awaiting_delivery_paid(summary) -> bool:
        """Pagamento recebido e cartas separadas: falta concluir a entrega."""
        return (summary.status == "Liberado" and summary.payment_status == "Pgto recebido"
                and not summary.completed_at)

    def _update_history_status_counts(self) -> None:
        """Mostra a quantidade de vendas diretamente nos botões de status."""
        if not getattr(self, "history_filter_buttons", None):
            return

        counts = self.sales_store.count_sales_by_status(
            search=self.history_filter_var.get().strip()
        )

        counts["PG Recebido"] = sum(
            self._awaiting_delivery_paid(summary)
            for summary in self.sales_store.list_sales(
                search=self.history_filter_var.get().strip(), status="Liberado",
            )
        )
        for status, button in self.history_filter_buttons.items():
            caption = self.history_filter_captions.get(status, status)
            total = int(counts.get(status, 0))
            button.configure(text=f"{caption} ({total})")

    def _set_history_status_filter(self, status: str) -> None:
        """Aplica o filtro rápido do histórico e atualiza o destaque dos botões."""
        if status not in {"Todos", "PG Recebido", *SALE_STATUS_OPTIONS}:
            status = "Aberta"
        self.history_status_filter_var.set(status)
        self._update_history_filter_buttons()
        self._refresh_sales_history()

    def _update_history_filter_buttons(self) -> None:
        """Mantém o filtro ativo visualmente evidente, sem poluir o painel."""
        if not getattr(self, "history_filter_buttons", None):
            return

        active = self.history_status_filter_var.get().strip() or "Aberta"
        style_map = {
            "Aberta": ("primary", "primary-outline"),
            "Liberado": ("warning", "warning-outline"),
            "PG Recebido": ("info", "info-outline"),
            "Finalizada": ("success", "success-outline"),
            "Cancelada": ("danger", "danger-outline"),
            "Todos": ("secondary", "secondary-outline"),
        }
        for status, button in self.history_filter_buttons.items():
            active_style, inactive_style = style_map.get(status, ("secondary", "secondary-outline"))
            button.configure(bootstyle=active_style if status == active else inactive_style)

    def _on_history_filter_keyrelease(self, _event: tk.Event | None = None) -> None:
        if hasattr(self, "_history_filter_after_id") and self._history_filter_after_id:
            self.root.after_cancel(self._history_filter_after_id)
        self._history_filter_after_id = self.root.after(220, self._refresh_sales_history)

    def clear_history_filter(self) -> None:
        self.history_filter_var.set("")
        # Limpar volta para o fluxo principal do histórico: somente vendas abertas.
        self.history_status_filter_var.set("Aberta")
        self._update_history_filter_buttons()
        self._refresh_sales_history()

    def _get_selected_history_sale_id(self) -> str | None:
        if not hasattr(self, "history_tree"):
            return None
        selected = self.history_tree.selection()
        return str(selected[0]) if selected else None

    def _load_items_into_current_sale(self, items: list[SaleItem]) -> None:
        self._clear_current_items()
        for item in items:
            self.items.append(item)
            self.item_by_id[item.id] = item
            self._insert_item_row(item)
        self._update_totals()

    def _clear_current_items(self) -> None:
        self.items.clear()
        self.item_by_id.clear()
        self.selected_item_id = None
        if hasattr(self, "update_item_button"):
            self.update_item_button.configure(state=DISABLED)
        if hasattr(self, "tree"):
            for item_id in self.tree.get_children():
                self.tree.delete(item_id)
        self._reset_form_after_add()
        self._update_totals()

    def _autosave_current_sale_if_loaded(self) -> None:
        if not self.current_sale_id:
            return
        title = self.sale_title_var.get().strip() or self._default_sale_title()
        status = self.sale_status_var.get().strip() or "Aberta"
        self.sales_store.save_sale(
            sale_id=self.current_sale_id,
            title=title,
            customer_name=self._format_customer_name(self.customer_var.get()).strip(),
            customer_phone=self._format_customer_phone(self.customer_phone_var.get()),
            items=self.items,
            status=status,
            notes=self._get_sale_notes(),
        )
        self.sale_title_var.set(title)
        self.sale_status_var.set(status)
        self.status_label.configure(text=f"Editando: {title}")
        self._refresh_sales_history(select_id=self.current_sale_id)

    def _get_sale_notes(self) -> str:
        if not hasattr(self, "sale_notes_text"):
            return ""
        return self.sale_notes_text.get("1.0", END).strip()

    def _set_sale_notes(self, value: str) -> None:
        if not hasattr(self, "sale_notes_text"):
            return
        self.sale_notes_text.delete("1.0", END)
        if value:
            self.sale_notes_text.insert("1.0", value)

    def _default_sale_title(self) -> str:
        return self.sales_store.generate_sale_code()

    @staticmethod
    def _safe_filename(value: str) -> str:
        cleaned = re.sub(r"[^\w\-. ]+", "", str(value or "").strip(), flags=re.UNICODE)
        cleaned = cleaned.replace(" ", "_").strip("._")
        return cleaned or "venda_pokemon_cards"

    def add_item(self) -> None:
        # Proteção também para comandos legados: edição nunca duplica a linha.
        if self.selected_item_id is not None:
            self.update_selected_item()
            return
        try:
            card = parse_card_text(self.card_text_var.get())
            quantity = self._parse_quantity(self.quantity_var.get())
            unit_price = self._parse_unit_price(self.unit_price_var.get())
        except (CardParseError, ValueError) as exc:
            messagebox.showwarning("Validação", str(exc))
            return

        stock_item_id = self.selected_stock_item_id
        if stock_item_id is not None:
            try:
                self.stock_store.reserve_quantity(stock_item_id, quantity)
            except StockQuantityError as exc:
                messagebox.showwarning("Estoque insuficiente", str(exc))
                self._refresh_suggestions()
                return

        try:
            item = SaleItem(
                card=card,
                quantity=quantity,
                unit_price=unit_price,
                extras=self._clean_extras(self.extras_var.get()),
                language=self._clean_language(self.language_var.get()),
                stock_item_id=stock_item_id,
            )
            self.items.append(item)
            self.item_by_id[item.id] = item
            self._normalize_sale_card_number(item)
            self.catalog.save_card(item.card)
            self._hide_suggestions()
            self._insert_item_row(item)
            self._update_totals()
            self._reset_form_after_add()
            status = (f"{quantity} unidade(s) reservada(s) do estoque"
                      if stock_item_id is not None else "carta sem vínculo com estoque")
            self.status_label.configure(text=f"Alteração não salva · item adicionado · {status}")
        except Exception:
            # Se qualquer etapa posterior falhar, não deixa o estoque baixado sem a venda.
            if stock_item_id is not None:
                self.stock_store.restore_quantity(stock_item_id, quantity)
            raise

    def _normalize_sale_card_number(self, item: SaleItem) -> None:
        """Atualiza a identificação em memória, sem alterar reservas do estoque.

        A carta normalizada será usada na edição, no salvamento e na exportação.
        Vendas antigas no disco só são modificadas quando o usuário as salva.
        Denominadores numéricos já informados são preservados.
        """
        card = item.card
        code = card.collection_number.strip()
        if not card.card_number or not code or code.isdecimal():
            return
        edition_name = ""
        stock_id = getattr(item, "stock_item_id", None)
        if stock_id is not None:
            stock = self.stock_store.get_item(stock_id)
            if stock is not None and stock.edition_code.strip().casefold() == code.casefold():
                edition_name = stock.edition_name
        number = self.card_cache.resolve_collection_number(code, edition_name)
        if not number:
            return
        normalized = replace(card, collection_number=number)
        item.card = replace(normalized, raw_text=normalized.display_name)

    def _insert_item_row(self, item: SaleItem) -> None:
        self._normalize_sale_card_number(item)
        self.tree.insert(
            "",
            END,
            iid=item.id,
            values=(
                item.card.display_name,
                item.card.card_number,
                item.card.collection_number,
                display_item_extras(item),
                self._language_display(getattr(item, "language", "") or ""),
                self._item_status_display(getattr(item, "separation_status", "") or ""),
                item.quantity,
                format_brl(item.unit_price),
                format_brl(item.net_total),
            ),
            tags=self._item_row_tags(item),
        )


    def on_sale_item_selected(self, _event: object | None = None) -> None:
        """Selecionar destaca a linha; editar é uma ação explícita."""
        selected = self.tree.selection()
        self.edit_item_button.configure(state=NORMAL if len(selected) == 1 else DISABLED)
        if self.item_context_menu is not None:
            self.item_context_menu.entryconfigure("Editar item", state=NORMAL if len(selected) == 1 else DISABLED)

    def update_selected_item(self) -> None:
        if not self.selected_item_id:
            messagebox.showwarning("Atualizar item", "Selecione um item da tabela para atualizar.")
            return

        old_item = self.item_by_id.get(self.selected_item_id)
        if old_item is None:
            messagebox.showwarning("Atualizar item", "Item selecionado não encontrado.")
            return

        try:
            card = parse_card_text(self.card_text_var.get())
            quantity = self._parse_quantity(self.quantity_var.get())
            unit_price = self._parse_unit_price(self.unit_price_var.get())
        except (CardParseError, ValueError) as exc:
            messagebox.showwarning("Validação", str(exc))
            return

        new_stock_item_id = self.selected_stock_item_id
        old_stock_item_id = getattr(old_item, "stock_item_id", None)
        try:
            if old_stock_item_id is None:
                if new_stock_item_id is not None:
                    self.stock_store.reserve_quantity(new_stock_item_id, quantity)
            elif new_stock_item_id is None:
                self.stock_store.restore_quantity(old_stock_item_id, old_item.quantity)
            else:
                self.stock_store.replace_reservation(
                    old_stock_item_id,
                    old_item.quantity,
                    new_stock_item_id,
                    quantity,
                )
        except StockQuantityError as exc:
            messagebox.showwarning("Estoque insuficiente", str(exc))
            return

        try:
            updated_item = SaleItem(
                card=card,
                quantity=quantity,
                unit_price=unit_price,
                extras=self._clean_extras(self.extras_var.get()),
                language=self._clean_language(self.language_var.get()),
                separation_status=getattr(old_item, "separation_status", "") or "",
                stock_item_id=new_stock_item_id,
                id=old_item.id,
                discount_percent=old_item.discount_percent,
                created_at=old_item.created_at,
            )
            self.item_by_id[updated_item.id] = updated_item
            self.items = [updated_item if item.id == updated_item.id else item for item in self.items]
            self._normalize_sale_card_number(updated_item)
            self.catalog.save_card(updated_item.card)
            self._replace_item_row(updated_item)
            self._update_totals()
            self._sync_current_sale_release_status()
            self._update_current_history_row_status(self.sale_status_var.get().strip() or "Aberta")
            self._reset_form_after_add()
            save_action = "Salvar alterações" if self.current_sale_id is not None else "Salvar venda"
            self.status_label.configure(text=f"Item atualizado na venda · clique em {save_action} para gravar.")
        except Exception:
            # Reverte a reserva em memória/banco se a atualização da venda falhar.
            try:
                if old_stock_item_id is None and new_stock_item_id is not None:
                    self.stock_store.restore_quantity(new_stock_item_id, quantity)
                elif old_stock_item_id is not None and new_stock_item_id is None:
                    self.stock_store.reserve_quantity(old_stock_item_id, old_item.quantity)
                elif old_stock_item_id is None:
                    pass
                else:
                    self.stock_store.replace_reservation(
                        new_stock_item_id, quantity, old_stock_item_id, old_item.quantity
                    )
            finally:
                raise

    def _replace_item_row(self, item: SaleItem) -> None:
        self._normalize_sale_card_number(item)
        if self.tree.exists(item.id):
            self.tree.item(
                item.id,
                values=(
                    item.card.display_name,
                    item.card.card_number,
                    item.card.collection_number,
                    display_item_extras(item),
                    self._language_display(getattr(item, "language", "") or "PT"),
                    self._item_status_display(getattr(item, "separation_status", "") or ""),
                    item.quantity,
                    format_brl(item.unit_price),
                    format_brl(item.net_total),
                ),
                tags=self._item_row_tags(item),
            )
        else:
            self._insert_item_row(item)

    def remove_selected(self) -> None:
        selected = self.tree.selection()
        if not selected:
            return

        for item_id in selected:
            item = self.item_by_id.get(str(item_id))
            if item is not None:
                stock_item_id = getattr(item, "stock_item_id", None)
                if stock_item_id is not None:
                    try:
                        self.stock_store.restore_quantity(stock_item_id, item.quantity)
                    except StockQuantityError as exc:
                        messagebox.showerror("Estoque", str(exc))
                        return

            self.item_by_id.pop(str(item_id), None)
            self.items = [current for current in self.items if current.id != str(item_id)]
            self.tree.delete(item_id)

        if self.selected_item_id in {str(item_id) for item_id in selected}:
            self._reset_form_after_add()

        self._update_totals()
        self._sync_current_sale_release_status()
        self._update_current_history_row_status(self.sale_status_var.get().strip() or "Aberta")
        self.status_label.configure(text="Alteração não salva · item(ns) removido(s) · estoque ajustado")

    def clear_sale(self) -> None:
        if self.items:
            confirm = messagebox.askyesno("Limpar venda", "Deseja remover todos os itens da venda atual?")
            if not confirm:
                return

        requests = [
            (int(item.stock_item_id), int(item.quantity))
            for item in self.items
            if getattr(item, "stock_item_id", None) is not None
        ]
        if requests:
            self.stock_store.restore_many(requests)

        self._clear_current_items()
        self._sync_current_sale_release_status()
        self._update_current_history_row_status(self.sale_status_var.get().strip() or "Aberta")
        self._reset_form_after_add()
        self.status_label.configure(
            text="Venda em aberto" if not self.current_sale_id else "Alteração não salva · venda limpa · estoque devolvido"
        )

    def export_excel(self) -> None:
        if not self._finish_pending_item("exportar a venda"):
            return
        if not self.items:
            messagebox.showwarning("Exportar Excel", "Adicione pelo menos uma carta antes de exportar.")
            return

        path = filedialog.asksaveasfilename(
            title="Exportar venda",
            defaultextension=".xlsx",
            filetypes=[("Excel", "*.xlsx")],
            initialfile=f"{self._safe_filename(self.sale_title_var.get() or 'venda_pokemon_cards')}.xlsx",
        )
        if not path:
            return

        try:
            output_path = export_sale_to_xlsx(
                self.items,
                Path(path),
                sale_title=self.sale_title_var.get().strip() or self._default_sale_title(),
                customer_name=self._format_customer_name(self.customer_var.get()).strip(),
                status=self.sale_status_var.get().strip() or "Aberta",
                notes=self._get_sale_notes(),
            )
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Exportar Excel", f"Erro ao exportar Excel:\n{exc}")
            return

        self.status_label.configure(text=f"Exportado: {output_path.name}")
        messagebox.showinfo("Exportar Excel", f"Arquivo exportado com sucesso:\n{output_path}")

    @staticmethod
    def _calculate_chargeable_totals(items):
        """Itens não encontrados continuam na venda, mas não entram na cobrança."""
        return calculate_totals([
            item for item in items
            if getattr(item, "separation_status", "") != ITEM_STATUS_NOT_FOUND
        ])

    def _update_totals(self) -> None:
        totals = calculate_totals(self.items)
        chargeable_totals = self._calculate_chargeable_totals(self.items)
        separated = sum(1 for item in self.items if getattr(item, "separation_status", "") == ITEM_STATUS_SEPARATED)
        not_found = sum(1 for item in self.items if getattr(item, "separation_status", "") == ITEM_STATUS_NOT_FOUND)

        self.items_label.configure(
            text=f"{totals.item_count} item(ns) · {totals.unit_count} unidade(s)"
        )
        if hasattr(self, "separated_label"):
            self.separated_label.configure(text=f"{separated} separado(s)")
        if hasattr(self, "not_found_label"):
            self.not_found_label.configure(text=f"{not_found} não encontrado(s)")
        cards, trades, receivable = trade_summary(self.items)
        self.total_label.configure(text=f"A receber: {format_brl(receivable)}")
        self.trade_summary_label.configure(
            text=f"Valor das cartas: {format_brl(cards)} · Abatimento por trocas: {format_brl(trades)}"
        )
        expanded = getattr(self, "_sale_items_window", None)
        if expanded is not None:
            expanded.refresh()

    def _reset_form_after_add(self) -> None:
        if self.suggestion_after_id is not None:
            self.root.after_cancel(self.suggestion_after_id)
            self.suggestion_after_id = None
        self._hide_suggestions()
        self.selected_item_id = None
        self.selected_stock_item_id = None
        if hasattr(self, "tree"):
            self.tree.selection_remove(self.tree.selection())
        if hasattr(self, "update_item_button"):
            self.update_item_button.configure(state=DISABLED)
        self.card_text_var.set("")
        self.quantity_var.set("1")
        self.unit_price_var.set("0,00")
        self.extras_var.set("")
        self.language_var.set("PT")
        self._linked_card_text = ""
        self._form_snapshot = self._item_form_values()
        self._set_item_form_mode()
        self.card_entry.focus_set()
        self.suggestion_status_label.configure(text="Digite uma carta ou selecione uma sugestão")


    @staticmethod
    def _clean_extras(value: str) -> str:
        return " ".join(str(value or "").strip().split())

    @staticmethod
    def _language_display(value: str) -> str:
        code = "".join(str(value or "").strip().upper().split())
        return {"PT": "🇧🇷 PT", "EN": "🇺🇸 EN"}.get(code, code)

    @staticmethod
    def _clean_language(value: str) -> str:
        clean = "".join(str(value or "").strip().upper().split())
        return clean or "PT"


    @staticmethod
    def _parse_unit_price(value: str):
        unit_price = parse_money_brl(value)
        if not math.isfinite(unit_price) or unit_price == 0:
            raise ValueError("Preço unitário deve ser diferente de zero. Use um valor negativo para abater uma troca.")
        return unit_price

    @staticmethod
    def _parse_quantity(value: str) -> int:
        try:
            quantity = int(str(value).strip())
        except ValueError as exc:
            raise ValueError("Quantidade deve ser um número inteiro.") from exc

        if quantity <= 0:
            raise ValueError("Quantidade deve ser maior que zero.")
        return quantity
