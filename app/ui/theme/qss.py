"""Stylesheet: the whole look, built from the palette and the tokens.

One stylesheet, generated at start-up, applied to the application. Building it
from :mod:`app.ui.theme.palette` and :mod:`app.ui.theme.tokens` is what keeps
the colours in one place: change the accent there and every control follows,
including the ones painted by hand, because they read the same palette.

The selectors use two custom properties so a widget can opt into a look
without a subclass:

* ``[kind="card"]`` — a raised panel with a hairline border.
* ``[role="muted"]``, ``[role="faint"]``, ``[role="accent"]``,
  ``[role="success"]``, ``[role="warning"]``, ``[role="danger"]`` — text colour
  roles.
* ``[kind="primary"]``, ``[kind="ghost"]``, ``[kind="danger"]`` — button kinds.
"""

from __future__ import annotations

from app.ui.theme.palette import Palette
from app.ui.theme.tokens import TOKENS, Tokens


def build_stylesheet(palette: Palette, tokens: Tokens = TOKENS) -> str:
    """Render the application stylesheet.

    Args:
        palette: Colours to paint with.
        tokens: Spacing, type and geometry.

    Returns:
        A Qt stylesheet string, ready for ``QApplication.setStyleSheet``.
    """
    spacing = tokens.spacing
    radii = tokens.radii
    typography = tokens.type
    geometry = tokens.geometry

    return f"""/* ------------------------------------------------------------------ base */
QMainWindow, QWidget#central, QDialog, QStackedWidget {{
    background-color: {palette.background};
    color: {palette.text};
}}

QWidget {{
    color: {palette.text};
    font-family: {typography.family};
    font-size: {typography.body}pt;
    selection-background-color: {palette.accent};
    selection-color: #FFFFFF;
}}

QLabel {{
    background: transparent;
    color: {palette.text};
}}

QWidget[kind="card"], QFrame[kind="card"] {{
    background-color: {palette.surface};
    border: 1px solid {palette.border};
    border-radius: {radii.lg}px;
}}

QWidget[kind="panel"], QFrame[kind="panel"] {{
    background-color: {palette.surface};
    border: 1px solid {palette.border};
    border-radius: {radii.lg}px;
}}

QWidget[kind="inset"], QFrame[kind="inset"] {{
    background-color: {palette.surface_alt};
    border: 1px solid {palette.border};
    border-radius: {radii.md}px;
}}

/* ------------------------------------------------------------------ shell */
/* The window's furniture: a sidebar with one hairline, a header with one
   hairline, rows that light up under the pointer, and pills for states. */
QFrame#sidebar {{
    background-color: {palette.surface};
    border: none;
    border-right: 1px solid {palette.border};
    min-width: {geometry.sidebar_width}px;
    max-width: {geometry.sidebar_width}px;
}}

QFrame#sidebar QPushButton {{
    background: transparent;
    border: none;
    border-radius: {radii.md}px;
    padding: {spacing.sm}px {spacing.md}px;
    text-align: left;
    color: {palette.text_muted};
    font-weight: 500;
}}

QFrame#sidebar QPushButton:hover {{
    background-color: {palette.surface_hover};
    color: {palette.text};
}}

QFrame#sidebar QPushButton:checked {{
    background-color: {palette.accent_soft};
    color: {palette.accent};
    font-weight: 600;
    border-left: 4px solid {palette.accent};
    border-radius: 0px;
    border-top-right-radius: {radii.md}px;
    border-bottom-right-radius: {radii.md}px;
}}

QFrame#sidebar QPushButton:focus {{
    border: none;
    outline: none;
}}

QFrame#topbar {{
    background-color: {palette.surface};
    border: none;
    border-bottom: 1px solid {palette.border};
    min-height: {geometry.topbar_height}px;
}}

QFrame#row {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: {radii.md}px;
}}

QFrame#row:hover {{
    background-color: {palette.surface_hover};
    border: 1px solid {palette.border_strong};
}}

QLabel[kind="pill"] {{
    background-color: {palette.surface_alt};
    border: 1px solid {palette.border};
    border-radius: {radii.pill}px;
    padding: 2px {spacing.sm}px;
    font-size: {typography.small}pt;
    font-weight: 500;
    color: {palette.text_muted};
}}

QLabel[kind="pill"][role="accent"] {{
    color: {palette.accent};
    border-color: {palette.accent_soft};
    background-color: {palette.accent_soft};
}}

QLabel[kind="pill"][role="success"] {{
    color: {palette.success};
    border-color: {palette.success};
}}

QLabel[kind="pill"][role="warning"] {{
    color: {palette.warning};
    border-color: {palette.warning};
}}

QLabel[kind="pill"][role="danger"] {{
    color: {palette.danger};
    border-color: {palette.danger};
}}

QLabel[kind="pill"][role="info"] {{
    color: {palette.info};
    border-color: {palette.info};
}}

QScrollArea {{
    background: transparent;
    border: none;
}}

QScrollArea > QWidget > QWidget {{
    background: transparent;
}}

QCheckBox {{
    spacing: 8px;
    color: {palette.text};
    background: transparent;
}}

QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {palette.border_strong};
    border-radius: {radii.sm}px;
    background-color: {palette.surface_alt};
}}

QCheckBox::indicator:hover {{
    border-color: {palette.accent};
}}

QCheckBox::indicator:checked {{
    background-color: {palette.accent};
    border: 1px solid {palette.accent};
}}

QCheckBox::indicator:disabled {{
    border-color: {palette.border};
    background-color: {palette.surface};
}}

/* ------------------------------------------------------------------ text */
QLabel[role="muted"] {{ color: {palette.text_muted}; }}
QLabel[role="faint"] {{ color: {palette.text_faint}; }}
QLabel[role="accent"] {{ color: {palette.accent}; }}
QLabel[role="success"] {{ color: {palette.success}; }}
QLabel[role="warning"] {{ color: {palette.warning}; }}
QLabel[role="danger"] {{ color: {palette.danger}; }}
QLabel[role="display"] {{ font-size: {typography.display}pt; font-weight: 700; }}
QLabel[role="title"] {{ font-size: {typography.title}pt; font-weight: 700; }}
QLabel[role="heading"] {{ font-size: {typography.heading}pt; font-weight: 600; }}
QLabel[role="small"] {{ font-size: {typography.small}pt; color: {palette.text_muted}; font-weight: 500; }}
QLabel[role="data"] {{
    font-family: {typography.mono};
    font-size: {typography.data}pt;
}}

/* --------------------------------------------------------------- buttons */
QPushButton {{
    background-color: {palette.surface_alt};
    border: 1px solid {palette.border};
    border-radius: {radii.md}px;
    padding: {spacing.xs + 2}px {spacing.lg}px;
    color: {palette.text};
    font-weight: 500;
}}

QPushButton:hover {{
    background-color: {palette.surface_hover};
    border-color: {palette.border_strong};
}}

QPushButton:pressed {{
    background-color: {palette.surface};
}}

QPushButton:disabled {{
    color: {palette.text_faint};
    background-color: {palette.surface};
    border-color: {palette.border};
}}

QPushButton:focus {{
    border: 1px solid {palette.accent};
}}

QPushButton[kind="primary"], QPushButton#primary {{
    background-color: {palette.accent};
    border: none;
    border-radius: {radii.md}px;
    color: #FFFFFF;
    font-weight: 600;
}}

QPushButton[kind="primary"]:hover, QPushButton#primary:hover {{
    background-color: {palette.with_alpha(palette.accent, 220)};
}}

QPushButton[kind="primary"]:pressed, QPushButton#primary:pressed {{
    background-color: {palette.with_alpha(palette.accent, 180)};
}}

QPushButton[kind="primary"]:disabled, QPushButton#primary:disabled {{
    background-color: {palette.border};
    color: {palette.text_faint};
}}

QPushButton[kind="ghost"] {{
    background: transparent;
    border: 1px solid transparent;
}}

QPushButton[kind="ghost"]:hover {{
    background-color: {palette.surface_hover};
    border-color: {palette.border};
}}

QPushButton[kind="danger"] {{
    color: {palette.danger};
    border: 1px solid {palette.border};
}}

QPushButton[kind="danger"]:hover {{
    border-color: {palette.danger};
    background-color: {palette.with_alpha(palette.danger, 30)};
}}

/* ---------------------------------------------------------------- inputs */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background-color: {palette.surface_alt};
    border: 1px solid {palette.border};
    border-radius: {radii.md}px;
    padding: {spacing.xs + 2}px {spacing.md}px;
    color: {palette.text};
    min-height: 22px;
}}

QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 1px solid {palette.accent};
    background-color: {palette.surface};
}}

QComboBox::drop-down {{
    border: none;
    width: {spacing.xl}px;
}}

QComboBox QAbstractItemView {{
    background-color: {palette.surface};
    border: 1px solid {palette.border};
    border-radius: {radii.md}px;
    selection-background-color: {palette.accent_soft};
    selection-color: {palette.text};
    outline: none;
    padding: 4px;
}}

/* ------------------------------------------------------------ containers */
QTabWidget::pane {{
    border: none;
    background-color: {palette.background};
    top: -1px;
}}

QTabBar {{
    background: transparent;
    border: none;
}}

QTabBar::tab {{
    background: transparent;
    color: {palette.text_muted};
    padding: {spacing.sm}px {spacing.lg}px;
    min-height: {geometry.tab_height - 12}px;
    border: none;
    border-bottom: 2px solid transparent;
    font-weight: 500;
}}

QTabBar::tab:hover {{
    color: {palette.text};
}}

QTabBar::tab:selected {{
    color: {palette.text};
    border-bottom: 2px solid {palette.accent};
    font-weight: 600;
}}

QHeaderView::section {{
    background-color: {palette.surface};
    color: {palette.text_muted};
    border: none;
    border-bottom: 1px solid {palette.border};
    padding: {spacing.sm}px {spacing.md}px;
    font-size: {typography.small}pt;
    font-weight: 600;
}}

QTreeView, QTableView, QListView {{
    background-color: {palette.surface};
    border: 1px solid {palette.border};
    border-radius: {radii.lg}px;
    alternate-background-color: {palette.surface_alt};
    gridline-color: transparent;
    selection-background-color: {palette.accent_soft};
    selection-color: {palette.text};
    outline: none;
}}

QTreeView::item, QTableView::item, QListView::item {{
    padding: {spacing.xs}px {spacing.sm}px;
    border: none;
    border-radius: {radii.sm}px;
}}

QTreeView::item:hover, QTableView::item:hover, QListView::item:hover {{
    background-color: {palette.surface_hover};
}}

QScrollBar:vertical {{
    background: transparent;
    width: {geometry.scrollbar}px;
    margin: 0;
}}

QScrollBar:horizontal {{
    background: transparent;
    height: {geometry.scrollbar}px;
    margin: 0;
}}

QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background-color: {palette.border_strong};
    border-radius: {geometry.scrollbar // 2}px;
    min-height: {spacing.xl}px;
    min-width: {spacing.xl}px;
}}

QScrollBar::handle:hover {{
    background-color: {palette.text_faint};
}}

QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QProgressBar {{
    background-color: {palette.surface_alt};
    border: 1px solid {palette.border};
    border-radius: 4px;
    height: 8px;
    text-align: center;
}}

QProgressBar::chunk {{
    background-color: {palette.accent};
    border-radius: 3px;
}}

QProgressBar[role="success"]::chunk {{ background-color: {palette.success}; }}
QProgressBar[role="warning"]::chunk {{ background-color: {palette.warning}; }}
QProgressBar[role="danger"]::chunk {{ background-color: {palette.danger}; }}

QSplitter::handle {{ background-color: {palette.border}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}

QToolTip {{
    background-color: {palette.surface_alt};
    color: {palette.text};
    border: 1px solid {palette.border_strong};
    border-radius: {radii.sm}px;
    padding: {spacing.xs}px {spacing.sm}px;
}}

QMenu {{
    background-color: {palette.surface};
    border: 1px solid {palette.border};
    border-radius: {radii.md}px;
    padding: {spacing.xs}px;
}}

QMenu::item {{
    padding: {spacing.xs}px {spacing.lg}px;
    border-radius: {radii.sm}px;
}}

QMenu::item:selected {{
    background-color: {palette.accent_soft};
    color: {palette.text};
}}

QStatusBar {{
    background-color: {palette.background};
    color: {palette.text_muted};
    border-top: 1px solid {palette.border};
}}
"""
