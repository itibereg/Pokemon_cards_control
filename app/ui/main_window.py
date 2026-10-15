from __future__ import annotations
# VERSAO_ATUALIZADA: HIDE_MAIN_WHILE_MODULE_OPEN_V52_2026_09_27

import tkinter as tk
import ttkbootstrap as tb
from ttkbootstrap.constants import BOTH, LEFT, RIGHT, TOP, X, Y, W, NW

from app.ui.sales_window import SalesWindow
from app.ui.stock_window import StockWindow
from app.ui.purchases_window import PurchasesWindow


class MainWindow:
    """Janela principal do sistema com menu de acesso aos módulos."""

    def __init__(self, root: tb.Window) -> None:
        self.root = root
        self.sales_toplevel: tk.Toplevel | None = None
        self.sales_window: SalesWindow | None = None
        self.stock_toplevel: tk.Toplevel | None = None
        self.stock_window: StockWindow | None = None
        self.purchases_toplevel: tk.Toplevel | None = None
        self.purchases_window: PurchasesWindow | None = None

        self.root.withdraw()
        self.root.title("Pokémon Cards")
        self.root.minsize(900, 680)

        self._configure_style()
        self._configure_geometry()
        self._build_ui()
        self._show_window()

    def _configure_style(self) -> None:
        style = tb.Style()
        style.configure("MainTitle.TLabel", font=("Segoe UI", 24, "bold"))
        style.configure("MainSubtitle.TLabel", font=("Segoe UI", 10))
        style.configure("MenuTitle.TLabel", font=("Segoe UI", 9, "bold"))
        style.configure("ModuleTitle.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("ModuleText.TLabel", font=("Segoe UI", 10))

    def _get_work_area(self) -> tuple[int, int, int, int]:
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

    def _configure_geometry(self) -> None:
        left, top, right, bottom = self._get_work_area()
        work_width = max(1, right - left)
        work_height = max(1, bottom - top)

        width = min(max(980, int(work_width * 0.70)), work_width - 40)
        height = min(max(740, int(work_height * 0.78)), work_height - 40)
        x = left + max(0, (work_width - width) // 2)
        y = top + max(0, (work_height - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _show_window(self) -> None:
        self.root.update_idletasks()
        self.root.deiconify()
        self.root.lift()

    def _build_ui(self) -> None:
        container = tb.Frame(self.root, padding=16)
        container.pack(fill=BOTH, expand=True)

        header = tb.Frame(container)
        header.pack(fill=X, pady=(0, 14))

        title_box = tb.Frame(header)
        title_box.pack(side=LEFT, fill=X, expand=True)
        tb.Label(title_box, text="Pokémon Cards", style="MainTitle.TLabel").pack(anchor=W)
        tb.Label(
            title_box,
            text="Controle de vendas, compras e estoque.",
            style="MainSubtitle.TLabel",
            bootstyle="secondary",
        ).pack(anchor=W, pady=(2, 0))

        tb.Label(
            header,
            text="Menu principal",
            bootstyle="secondary",
            font=("Segoe UI", 9, "bold"),
        ).pack(side=RIGHT, anchor=NW, pady=(8, 0))

        tb.Separator(container).pack(fill=X, pady=(0, 14))

        workspace = tb.Frame(container)
        workspace.pack(fill=BOTH, expand=True)

        sidebar = tb.Frame(workspace, width=220, padding=(0, 0, 14, 0))
        sidebar.pack(side=LEFT, fill=Y)
        sidebar.pack_propagate(False)

        tb.Label(sidebar, text="ACESSOS", style="MenuTitle.TLabel", bootstyle="secondary").pack(
            anchor=W, pady=(2, 8)
        )

        self.sales_button = tb.Button(
            sidebar,
            text="Vendas",
            bootstyle="primary",
            command=self.open_sales,
            padding=(12, 10),
        )
        self.sales_button.pack(fill=X, pady=(0, 8))

        self.stock_button = tb.Button(
            sidebar,
            text="Estoque",
            bootstyle="primary-outline",
            command=self.open_stock,
            padding=(12, 10),
        )
        self.stock_button.pack(fill=X, pady=(0, 8))

        self.purchases_button = tb.Button(
            sidebar, text="Compras", bootstyle="success-outline",
            command=self.open_purchases, padding=(12, 10),
        )
        self.purchases_button.pack(fill=X, pady=(0, 8))

        tb.Separator(sidebar).pack(fill=X, pady=10)
        tb.Label(
            sidebar,
            text="Novos módulos poderão ser adicionados aqui.",
            bootstyle="secondary",
            wraplength=190,
            justify="left",
            font=("Segoe UI", 8),
        ).pack(anchor=W)

        content = tb.Labelframe(
            workspace,
            text=" Início ",
            padding=22,
            bootstyle="secondary",
        )
        content.pack(side=LEFT, fill=BOTH, expand=True)

        tb.Label(content, text="Bem-vindo", style="ModuleTitle.TLabel").pack(anchor=W, pady=(6, 4))
        tb.Label(
            content,
            text="Selecione um módulo no menu à esquerda para começar.",
            style="ModuleText.TLabel",
            bootstyle="secondary",
        ).pack(anchor=W)

        card = tb.Labelframe(content, text=" Vendas ", padding=16, bootstyle="primary")
        card.pack(fill=X, pady=(24, 0))

        card_text = tb.Frame(card)
        card_text.pack(side=LEFT, fill=X, expand=True)
        tb.Label(card_text, text="Vendas Pokémon Cards", font=("Segoe UI", 13, "bold")).pack(anchor=W)
        tb.Label(
            card_text,
            text="Monte vendas, organize a separação dos itens e consulte o histórico.",
            bootstyle="secondary",
            font=("Segoe UI", 9),
        ).pack(anchor=W, pady=(3, 0))

        tb.Button(
            card,
            text="Abrir Vendas",
            bootstyle="primary",
            command=self.open_sales,
            width=16,
        ).pack(side=RIGHT, padx=(12, 0))

        stock_card = tb.Labelframe(content, text=" Estoque ", padding=16, bootstyle="secondary")
        stock_card.pack(fill=X, pady=(12, 0))

        stock_text = tb.Frame(stock_card)
        stock_text.pack(side=LEFT, fill=X, expand=True)
        tb.Label(stock_text, text="Estoque de cartas", font=("Segoe UI", 13, "bold")).pack(anchor=W)
        tb.Label(
            stock_text,
            text="Consulte e organize o estoque em uma janela dedicada.",
            bootstyle="secondary",
            font=("Segoe UI", 9),
        ).pack(anchor=W, pady=(3, 0))

        tb.Button(
            stock_card,
            text="Abrir Estoque",
            bootstyle="primary-outline",
            command=self.open_stock,
            width=16,
        ).pack(side=RIGHT, padx=(12, 0))

        purchases_card = tb.Labelframe(content, text=" Compras ", padding=16, bootstyle="success")
        purchases_card.pack(fill=X, pady=(12, 0))
        purchases_text = tb.Frame(purchases_card)
        purchases_text.pack(side=LEFT, fill=X, expand=True)
        tb.Label(purchases_text, text="Compras realizadas", font=("Segoe UI", 13, "bold")).pack(anchor=W)
        tb.Label(purchases_text, text="Registre aquisições, consulte fornecedores e acompanhe pagamentos.",
                 bootstyle="secondary", font=("Segoe UI", 9), wraplength=420).pack(anchor=W, pady=(3, 0))
        tb.Button(purchases_card, text="Abrir Compras", bootstyle="success-outline",
                  command=self.open_purchases, width=16).pack(side=RIGHT, padx=(12, 0))

        footer = tb.Frame(container)
        footer.pack(fill=X, pady=(14, 0))
        tb.Separator(footer).pack(fill=X, pady=(0, 8))
        tb.Label(
            footer,
            text="Pokémon Cards",
            bootstyle="secondary",
            font=("Segoe UI", 8),
        ).pack(side=LEFT)
        tb.Label(
            footer,
            text="Pronto",
            bootstyle="success",
            font=("Segoe UI", 8, "bold"),
        ).pack(side=RIGHT)

    def open_sales(self) -> None:
        """Abre Vendas mantendo o menu principal oculto enquanto o módulo estiver ativo."""
        self.root.withdraw()

        if self.sales_toplevel is not None:
            try:
                if self.sales_toplevel.winfo_exists():
                    self.sales_toplevel.deiconify()
                    self.sales_toplevel.lift()
                    self.sales_toplevel.focus_force()
                    return
            except tk.TclError:
                self.sales_toplevel = None
                self.sales_window = None

        top = tk.Toplevel(self.root)
        self.sales_toplevel = top

        try:
            self.sales_window = SalesWindow(top)
        except Exception:
            self.sales_toplevel = None
            self.sales_window = None
            try:
                top.destroy()
            except tk.TclError:
                pass
            self._show_main_window()
            raise

        top.protocol("WM_DELETE_WINDOW", self._close_sales)

    def open_stock(self) -> None:
        """Abre Estoque mantendo o menu principal oculto enquanto o módulo estiver ativo."""
        self.root.withdraw()

        if self.stock_toplevel is not None:
            try:
                if self.stock_toplevel.winfo_exists():
                    self.stock_toplevel.deiconify()
                    self.stock_toplevel.lift()
                    self.stock_toplevel.focus_force()
                    return
            except tk.TclError:
                self.stock_toplevel = None
                self.stock_window = None

        top = tk.Toplevel(self.root)
        self.stock_toplevel = top

        try:
            self.stock_window = StockWindow(top)
        except Exception:
            self.stock_toplevel = None
            self.stock_window = None
            try:
                top.destroy()
            except tk.TclError:
                pass
            self._show_main_window()
            raise

        top.protocol("WM_DELETE_WINDOW", self._close_stock)

    def open_purchases(self) -> None:
        """Abre Compras mantendo o menu principal oculto enquanto o módulo estiver ativo."""
        self.root.withdraw()

        if self.purchases_toplevel is not None:
            try:
                if self.purchases_toplevel.winfo_exists():
                    self.purchases_toplevel.deiconify()
                    self.purchases_toplevel.lift()
                    self.purchases_toplevel.focus_force()
                    return
            except tk.TclError:
                self.purchases_toplevel = None
                self.purchases_window = None

        top = tk.Toplevel(self.root)
        self.purchases_toplevel = top

        try:
            self.purchases_window = PurchasesWindow(top)
        except Exception:
            self.purchases_toplevel = None
            self.purchases_window = None
            try:
                top.destroy()
            except tk.TclError:
                pass
            self._show_main_window()
            raise

        top.protocol("WM_DELETE_WINDOW", self._close_purchases)

    def _show_main_window(self) -> None:
        """Restaura o menu principal somente quando o usuário sair de um módulo."""
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:
            pass

    def _close_stock(self) -> None:
        if self.stock_toplevel is not None:
            try:
                if self.stock_toplevel.winfo_exists():
                    self.stock_toplevel.destroy()
            except tk.TclError:
                pass
        self.stock_toplevel = None
        self.stock_window = None
        self._show_main_window()

    def _close_purchases(self) -> None:
        if self.purchases_toplevel is not None:
            try:
                if self.purchases_toplevel.winfo_exists():
                    self.purchases_toplevel.destroy()
            except tk.TclError:
                pass
        self.purchases_toplevel = None
        self.purchases_window = None
        self._show_main_window()

    def _close_sales(self) -> None:
        if self.sales_toplevel is not None:
            try:
                if self.sales_toplevel.winfo_exists():
                    self.sales_toplevel.destroy()
            except tk.TclError:
                pass
        self.sales_toplevel = None
        self.sales_window = None
        self._show_main_window()
