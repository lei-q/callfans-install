"""UI 主题：品牌红配色、全局 QSS、SVG 线性图标（2026-10-15 改版）。

图标取自 Lucide（24x24 线性风格），内嵌免资源打包；颜色在渲染时注入，
同一图标可按状态（导航选中/未选中）渲染两套像素。
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

# ---------- 配色 ----------

PRIMARY = "#CE4141"        # 品牌红（logo 同色）
PRIMARY_HOVER = "#B83838"
PRIMARY_PRESSED = "#9F2F2F"
PRIMARY_SOFT = "#FBEAEA"   # 浅红底（选中/提示）
SIDEBAR = "#0F172A"        # 侧栏深色
SIDEBAR_HOVER = "#1E293B"
BG = "#F1F5F9"             # 内容区底色
CARD = "#FFFFFF"
BORDER = "#E2E8F0"
TEXT = "#0F172A"
MUTED = "#475569"
SUBTLE = "#64748B"
SUCCESS = "#16A34A"
WARNING = "#D97706"
DANGER = "#DC2626"

FONT_STACK = '"Microsoft YaHei UI", "PingFang SC", "Segoe UI", "Helvetica Neue", sans-serif'

# ---------- 图标（Lucide 路径） ----------

ICONS = {
    "home": '<path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'
            '<polyline points="9 22 9 12 15 12 15 22"/>',
    "layers": '<path d="M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91'
              'a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z"/>'
              '<path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65"/>'
              '<path d="m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65"/>',
    "refresh": '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/>'
               '<path d="M21 3v5h-5"/>'
               '<path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/>'
               '<path d="M8 16H3v5"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
                '<polyline points="7 10 12 15 17 10"/><line x1="12" x2="12" y1="15" y2="3"/>',
    "external": '<path d="M15 3h6v6"/><path d="M10 14 21 3"/>'
                '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    "network": '<rect x="16" y="16" width="6" height="6" rx="1"/>'
               '<rect x="2" y="16" width="6" height="6" rx="1"/>'
               '<rect x="9" y="2" width="6" height="6" rx="1"/>'
               '<path d="M5 16v-3a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1v3"/><path d="M12 12V8"/>',
    "package": '<path d="m7.5 4.27 9 5.15"/>'
               '<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8'
               'a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/>'
               '<path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>',
    "arrow-up": '<circle cx="12" cy="12" r="10"/><path d="m16 12-4-4-4 4"/><path d="M12 16V8"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    "database": '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5V19A9 3 0 0 0 21 19V5"/>'
                '<path d="M3 12A9 3 0 0 0 21 12"/>',
}


def render_svg(svg: str, size: int, ratio: float = 2.0) -> QPixmap:
    """SVG 文本 → 高分屏清晰的 QPixmap（按 ratio 超采样）。"""
    px = int(size * ratio)
    pix = QPixmap(px, px)
    pix.fill(Qt.GlobalColor.transparent)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    if renderer.isValid():
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter, QRectF(0, 0, px, px))
        painter.end()
    pix.setDevicePixelRatio(ratio)
    return pix


def icon_pixmap(name: str, color: str = MUTED, size: int = 20) -> QPixmap:
    body = ICONS[name]
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
           f'stroke="{color}" stroke-width="2" stroke-linecap="round" '
           f'stroke-linejoin="round">{body}</svg>')
    return render_svg(svg, size)


def icon(name: str, color: str = MUTED, size: int = 20, checked_color: str | None = None) -> QIcon:
    """线性图标；给 checked_color 时选中态（On）用另一种颜色（导航按钮用）。"""
    ico = QIcon()
    ico.addPixmap(icon_pixmap(name, color, size), QIcon.Mode.Normal, QIcon.State.Off)
    ico.addPixmap(icon_pixmap(name, "#CBD5E1", size), QIcon.Mode.Disabled, QIcon.State.Off)
    if checked_color:
        ico.addPixmap(icon_pixmap(name, checked_color, size), QIcon.Mode.Normal, QIcon.State.On)
    return ico


def repolish(widget) -> None:
    """动态属性（tone 等）变化后刷新样式。"""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


# ---------- 全局样式表 ----------

STYLESHEET = f"""
* {{
    font-family: {FONT_STACK};
    font-size: 13px;
    color: {TEXT};
}}
QMainWindow, QFrame#contentArea {{
    background: {BG};
}}

/* 侧栏 */
QFrame#sidebar {{
    background: {SIDEBAR};
}}
QLabel#brandTitle {{
    color: #FFFFFF;
    font-size: 16px;
    font-weight: 600;
}}
QLabel#brandVersion {{
    color: #94A3B8;
    font-size: 12px;
}}
QPushButton#navButton {{
    background: transparent;
    color: #CBD5E1;
    border: none;
    border-radius: 8px;
    padding: 10px 14px;
    text-align: left;
    font-size: 14px;
}}
QPushButton#navButton:hover {{
    background: {SIDEBAR_HOVER};
    color: #FFFFFF;
}}
QPushButton#navButton:checked {{
    background: {PRIMARY};
    color: #FFFFFF;
    font-weight: 600;
}}

/* 页面标题 */
QLabel#pageTitle {{
    font-size: 20px;
    font-weight: 600;
}}
QLabel#sectionTitle {{
    font-size: 14px;
    font-weight: 600;
}}
QLabel#muted {{
    color: {MUTED};
}}

/* 卡片 */
QFrame#card, QFrame#statCard {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 12px;
}}
QFrame#statCard:hover {{
    border-color: #CBD5E1;
}}
QLabel#statValue {{
    font-size: 26px;
    font-weight: 700;
}}
QLabel#statCaption {{
    color: {SUBTLE};
    font-size: 12px;
}}

/* 首页主视觉 */
QFrame#hero {{
    border-radius: 16px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 {PRIMARY}, stop:0.55 #A93535, stop:1 #6E2121);
}}
QLabel#heroTitle {{
    color: #FFFFFF;
    font-size: 28px;
    font-weight: 700;
}}
QLabel#heroSubtitle {{
    color: #FDE2E2;
    font-size: 14px;
}}
QLabel#heroHint {{
    color: #FDE2E2;
    font-size: 12px;
}}
QPushButton#heroButton {{
    background: #FFFFFF;
    color: {PRIMARY};
    border: none;
    border-radius: 10px;
    padding: 12px 26px;
    font-size: 16px;
    font-weight: 700;
}}
QPushButton#heroButton:hover {{
    background: {PRIMARY_SOFT};
}}
QPushButton#heroButton:pressed {{
    background: #F5D0D0;
}}

/* 状态胶囊 */
QLabel#pill {{
    border-radius: 11px;
    padding: 3px 12px;
    font-size: 12px;
    font-weight: 600;
    background: rgba(255, 255, 255, 0.18);
    color: #FFFFFF;
}}
QLabel#pill[tone="ok"] {{ background: #DCFCE7; color: #166534; }}
QLabel#pill[tone="busy"] {{ background: #FEF3C7; color: #92400E; }}
QLabel#pill[tone="down"] {{ background: #FEE2E2; color: #991B1B; }}

/* 按钮 */
QPushButton {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 7px 14px;
}}
QPushButton:hover {{
    background: #F8FAFC;
    border-color: #CBD5E1;
}}
QPushButton:pressed {{
    background: {BORDER};
}}
QPushButton:disabled {{
    color: #94A3B8;
    background: #F8FAFC;
    border-color: {BORDER};
}}
QPushButton#primary {{
    background: {PRIMARY};
    border: 1px solid {PRIMARY};
    color: #FFFFFF;
    font-weight: 600;
}}
QPushButton#primary:hover {{
    background: {PRIMARY_HOVER};
    border-color: {PRIMARY_HOVER};
}}
QPushButton#primary:pressed {{
    background: {PRIMARY_PRESSED};
}}
QPushButton#primary:disabled {{
    background: #E9A8A8;
    border-color: #E9A8A8;
    color: #FFFFFF;
}}
QPushButton#ghost {{
    background: transparent;
    border: none;
    color: {MUTED};
    padding: 4px 8px;
}}
QPushButton#ghost:hover {{
    color: {PRIMARY};
}}
QPushButton:focus {{
    outline: none;
}}

/* 表格 / 文本区 */
QTableWidget, QPlainTextEdit {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 10px;
    selection-background-color: {PRIMARY_SOFT};
    selection-color: {TEXT};
}}
QTableWidget {{
    gridline-color: #F1F5F9;
    alternate-background-color: #F8FAFC;
}}
QTableWidget::item {{
    padding: 4px 6px;
}}
QTableWidget::item:hover {{
    background: #F8FAFC;
}}
QHeaderView::section {{
    background: #F8FAFC;
    color: {MUTED};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 8px 6px;
    font-weight: 600;
}}
QPlainTextEdit#logView {{
    font-family: "Cascadia Mono", "Consolas", "Menlo", monospace;
    font-size: 12px;
    background: #0F172A;
    color: #E2E8F0;
    border-color: #0F172A;
}}

/* 进度条 / 分割条 */
QProgressBar {{
    background: {BORDER};
    border: none;
    border-radius: 4px;
    max-height: 8px;
}}
QProgressBar::chunk {{
    background: {PRIMARY};
    border-radius: 4px;
}}
QSplitter::handle {{
    background: transparent;
}}
QSplitter::handle:hover {{
    background: {BORDER};
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
}}
QScrollBar::handle:vertical {{
    background: #CBD5E1;
    border-radius: 5px;
    min-height: 24px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0;
}}
"""
