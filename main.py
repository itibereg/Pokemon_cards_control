from __future__ import annotations

import ttkbootstrap as tb

from app.ui.main_window import MainWindow


def main() -> None:
    root = tb.Window(themename="flatly")
    MainWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
