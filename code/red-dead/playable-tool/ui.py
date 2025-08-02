DEBUG = False # Set to True to enable debug output

from PyQt5.QtGui import QFont, QFontDatabase
import os
import subprocess

DARK_DOCK_BORDER = "#111"
LIGHT_DOCK_BORDER = "#eee"

class UI:
    def __init__(self):
        # Map styles to font file paths and default sizes
        self.fonts = {
            # style: (family, size, weight, italic)
            'tiny':     ('Roboto Mono', 12, QFont.Bold, False),
            'tiny-condensed': ('Roboto Condensed', 12, QFont.Normal, False),
            'button':   ('Roboto', 12, QFont.Normal, False),
            'tab':      ('Roboto', 8, QFont.Normal, False),
            'collumn':  ('Roboto', 10, QFont.Normal, False),
            'cell':     ('Roboto', 12, QFont.Normal, False),
            'cell-tiny':('Roboto', 12, QFont.Thin, False),
            'cell-text':('Roboto Condensed', 13, QFont.Normal, False),
            'cell-mono':('Roboto Mono', 12, QFont.Normal, False),
            'text':     ('Hanken Grotesk', 16, QFont.Normal, False),
            'italic':   ('Hanken Grotesk', 14, QFont.Normal, True),
            'black':    ('Roboto Serif', 16, QFont.Black, False),
            'prompt':   ('Roboto Mono', 12, QFont.Normal, False),
            'monospace':('Roboto Mono', 14, QFont.Normal, False),
            'title':    ('Roboto Slab', 11, QFont.Black, False),
            'year':     ('Roboto Slab', 11, QFont.Normal, False),
            'director': ('Roboto Slab', 11, QFont.Normal, False),
            'tagline':  ('Roboto Serif', 11, QFont.Normal, True),
            'default':  ('Roboto', 16, QFont.Normal, False),
        }
        self.font_paths = [
            './ui/fonts/Hanken_Grotesk/static/HankenGrotesk-Regular.ttf',
            './ui/fonts/Hanken_Grotesk/static/HankenGrotesk-Bold.ttf',
            './ui/fonts/Hanken_Grotesk/static/HankenGrotesk-Black.ttf',
            './ui/fonts/Hanken_Grotesk/static/HankenGrotesk-Italic.ttf',
            './ui/fonts/Hanken_Grotesk/static/HankenGrotesk-Light.ttf',
            './ui/fonts/Hanken_Grotesk/static/HankenGrotesk-Thin.ttf',
            './ui/fonts/Roboto/static/Roboto-Regular.ttf',
            './ui/fonts/Roboto/static/Roboto-Black.ttf',
            './ui/fonts/Roboto/static/Roboto-Bold.ttf',
            './ui/fonts/Roboto/static/Roboto-Italic.ttf',
            './ui/fonts/Roboto/static/Roboto_Condensed-Regular.ttf',
            './ui/fonts/Roboto/static/Roboto_Condensed-Bold.ttf',  
            './ui/fonts/Roboto_Mono/static/RobotoMono-Thin.ttf',
            './ui/fonts/Roboto_Mono/static/RobotoMono-Regular.ttf',
            './ui/fonts/Roboto_Mono/static/RobotoMono-Bold.ttf',
            './ui/fonts/Roboto_Serif/static/RobotoSerif-Thin.ttf',
            './ui/fonts/Roboto_Serif/static/RobotoSerif-ThinItalic.ttf',
            './ui/fonts/Roboto_Serif/static/RobotoSerif-Light.ttf',
            './ui/fonts/Roboto_Serif/static/RobotoSerif-LightItalic.ttf',
            './ui/fonts/Roboto_Serif/static/RobotoSerif-Regular.ttf',
            './ui/fonts/Roboto_Serif/static/RobotoSerif-Italic.ttf',
            './ui/fonts/Roboto_Serif/static/RobotoSerif-Bold.ttf',
            './ui/fonts/Roboto_Serif/static/RobotoSerif-BoldItalic.ttf',
            './ui/fonts/Roboto_Serif/static/RobotoSerif-Black.ttf',
            './ui/fonts/Roboto_Slab/static/RobotoSlab-Regular.ttf',
            './ui/fonts/Roboto_Slab/static/RobotoSlab-Bold.ttf',
            './ui/fonts/Roboto_Slab/static/RobotoSlab-Light.ttf',
            './ui/fonts/Roboto_Slab/static/RobotoSlab-Thin.ttf', 
        ]
        self._load_fonts()

    def _load_fonts(self):
        for path in self.font_paths:
            abs_path = os.path.abspath(path)
            if os.path.exists(abs_path):
                font_id = QFontDatabase.addApplicationFont(abs_path)
                if DEBUG:
                    if font_id != -1:
                        print(f"[UI] Loaded font from {abs_path}")
                    else:
                        print(f"[UI] Failed to load font from {abs_path}")
            else:
                print(f"[UI] Font file not found at {abs_path}")
                
    def get_font(self, style=None, size=None):
        style = style if style in self.fonts else 'default'
        family, default_size, weight, italic = self.fonts[style]
        font_size = size if size is not None else default_size
        font = QFont(family, font_size)
        font.setWeight(weight)
        font.setItalic(italic)
        return font
    
    def is_dark_mode(self):
        try:
            result = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            return "Dark" in result.stdout
        except Exception:
            return False

    def get_dimensions(self, style=None):
        if style == 'tiny':
            return (22, 22)
        elif style == 'button':
            return (80, 24)
        elif style == 'tab':
            return (60, 20)
        else:
            return (140, 32)
        
    def get_style_sheet(self, style=None):
        if style == 'app':

            # common style sheet
            return_text = self.get_common_style_sheet()
        
            # differing styles based on dark|light mode
            if self.is_dark_mode():
                return_text += "\n\n" + self.get_dark_style_sheet()
            else:
                return_text += "\n\n" + self.get_light_style_sheet()

            return_text += "\n\n" + self.get_messy_style_hack()
            return return_text
        
        else:
            return ""
        
    # -------------------------------------------------------

    def get_messy_style_hack(self):

        # TODO: This gets called last, so this should be cleaned up in styles

        if self.is_dark_mode():
            tab_background = DARK_DOCK_BORDER
        else:
            tab_background = LIGHT_DOCK_BORDER

        return f"""
        
        /* TEMP HACKS GO HERE */

        QTabBar {{
            background: transparent;  /* prevents shadow line */
        }}

        """

    # -------------------------------------------------------

    def get_common_style_sheet(self):

        # get the button font
        button_font = self.get_font("button")
        button_font_name = button_font.family()
        button_font_size = button_font.pointSize()
        button_font_weight = button_font.weight()

        # get the tab font
        tab_font = self.get_font("tab")
        tab_font_name = tab_font.family()
        tab_font_size = tab_font.pointSize()
        tab_font_weight = tab_font.weight()
        tab_width, tab_height = self.get_dimensions("tab")

        # cell-mono font
        monospaced_font = self.get_font("monospaced")
        monospaced_font_name = monospaced_font.family()
        monospaced_font_size = monospaced_font.pointSize()
        monospaced_font_weight = monospaced_font.weight()

        # dimensions
        button_width = 80
        button_height = 24
        separator_size = 3

        return f"""
        
        /* FUSION HACK */

        QPushButton {{
            background-color: #dddddd;
            color: black;
            border: none;
            border-radius: 10%;
            padding: 4px 8px;
        }}

        QPushButton:disabled {{
            background-color: #cccccc;
            color: #888;
            border: 1px solid #aaa;
        }}

        /* --------------*/
        
        /* WINDOW */

        QMainWindow::separator {{
            width: {separator_size}px;
            height: {separator_size}px;
        }}

        QMainWindow {{
            border: none;
            outline: none;
        }}

        /* WIDGETS */

        QWidget {{
            border: none;
            outline: none;
        }}
                        
        QDockWidget {{
            border: none;
            background: transparent;
        }}
                        
        QDockWidget::title {{
            text-align: center;
            padding-left: 8px;
            padding-top: 2px;
            padding-bottom: 2px;
        }}

        /* SCROLL BARS */

        QScrollBar {{
            background: transparent;
            margin: 0px 0px 0px 0px;
            border: none;
        }}

        QScrollBar:vertical {{
            width: 10px;
        }}

        QScrollBar:horizontal {{
            height: 10px;
        }}

        QScrollBar::handle {{
            background: transparent;
            min-height: 32px;
            border: none;
            border-radius: 0px;
        }}

        QScrollBar::handle:vertical {{
            height: 0px;
        }}

        QScrollBar::handle:horizontal {{
            width: 0px;
        }}

        QScrollBar::add-line:vertical,
        QScrollBar::sub-line:vertical {{
            border: none;
            color: transparent;
            background: transparent;
        }}

        QScrollBar::add-page,
        QScrollBar::sub-page {{
            color: transparent;
            background: transparent;
            border: none;
        }}

        QScrollBar::add-page:horizontal,
        QScrollBar::sub-page:horizontal {{
        }}

        QScrollBar::add-page:vertical,
        QScrollBar::sub-page:vertical {{
        }}

        QScrollBar::add-line:horizontal,
        QScrollBar::sub-line:horizontal {{
            width: 0px;
            height: 0px;
            border: none;
            background: transparent;
            image: none;
            subcontrol-position: none;
        }}

        /* LISTS */

        QHBoxLayout {{
            margin: 0px;
            padding: 0px;
        }}

        QListWidget::item {{
            margin-left: 0px;
            padding-left: 0px;
            border: none;
        }}

        QListWidget {{
            padding-left: 0px;
            margin-left: 0px;
            border: none;
        }}

        /* TABLES */

        QHeaderView {{
            font-family: {button_font_name};
            font-size: {button_font_size}px;
            font-weight: {button_font_weight};
        }}

        QTableView {{
        }}

        QHeaderView::section:horizontal {{
        }}

        QHeaderView::section:horizontal:last {{
            /*border-right: none;*/
        }}

        QHeaderView::section:vertical {{
            padding: 0px 8px 0px 0px;
            font-family: {monospaced_font_name};
            font-size: 12px;
            font-weight: {monospaced_font_weight};
        }}

        QHeaderView::section:vertical:last {{
            border-bottom: none;
        }}

        QTableCornerButton::section {{
            border: none;
        }}

        /* DROP DOWN */

        QComboBox {{
            border: none;
            border-radius: 10%;
            padding: 0px 0x 0px 20px;
            font-family: {button_font_name};
            font-size: {button_font_size}px;
            font-weight: {button_font_weight};
            min-height: {button_height}px;
        }}

        QComboBox, QPushButton {{
            min-height: {button_height}px;
            max-height: {button_height}px;
            padding: 0 12px;
            margin: 0;
        }}

        QComboBox:disabled {{
        }}

        QComboBox::drop-down {{
            subcontrol-origin: padding;
            subcontrol-position: top right;
            width: 20px;
            border-radius: 10%;
        }}

        QComboBox::down-arrow {{
            image: url('ui/icons/dropdown-dark.png');
            width: 8px;
            height: 8px;
        }}

        /* CHECK BOXES */

        QCheckBox {{
            spacing: 4px;
        }}

        QCheckBox::indicator {{
            width: 16px;
            height: 16px;
            border: none;
        }}

        QCheckBox::indicator:checked {{
            image: url('ui/icons/checkbox_on.png');
        }}

        /* TABS */

        QTabBar::tab {{
            border: none;
            padding: 0px 10px 0px 10px;
            margin: 0px 0px 4px 0px;
            border-radius: 0px;
            font-family: {tab_font_name};
            font-size: {tab_font_size}px;
            font-weight: {tab_font_weight};
            min-width: {tab_width}px;
            max-width: {tab_width}px;
            min-height: {tab_height}px;
        }}

        QTabBar::tab:first {{
            margin: 0px 0px 4px 3px;
            border-top-left-radius: 10%;
            border-bottom-left-radius: 10%;
        }}

        QTabBar::tab:last {{
            border-top-right-radius: 10%;
            border-bottom-right-radius: 10%;
        }}

        QTabBar::tab {{
        }}

        QTabBar::tab:selected {{
        }}

        QTabWidget::pane {{
            background: #f00;
            border: none;
            border-radius: 0px;
            margin: 0px;
        }}

        QTabWidget {{
            background: #ddd;
            border: none;
        }}

        QDockWidget {{
            border-radius: 0px;
        }}

        /* LINE EDITS AND TEXT EDITS */

        QTextEdit {{
            border: none;
            padding: 0px;
            margin: 0px;
            border-radius: 0px;
        }}

        QLineEdit {{
            border-radius: 7%;
        }}

        /* LABELS */

        QLabel {{
        }}

        /* BUTTONS */
        
        QPushButton {{
            border-style: none;
            border-radius: 10%;
            padding: 0px 0px;
            margin: 5;
            font-family: {button_font_name};
            font-size: {button_font_size}px;
            font-weight: {button_font_weight};
            min-height: {button_height}px;
            vertical-align: center;
        }}

        QPushButton:pressed {{
        }}

        QPushButton:disabled {{
        }}

        /* SLIDER */

        QSlider {{
            background: transparent;
            height: 4px;
            margin: 8px 8px;
        }}

        QSlider::groove:horizontal {{
            border: none;
            height: 4px;
            border-radius: 2px;
        }}
        
        QSlider::sub-page:horizontal {{
            border-radius: 2px;
        }}

        QSlider::handle:horizontal:pressed {{
        }}

        QSlider::handle:horizontal:disabled {{
        }}

        QSlider::handle:horizontal {{
            border: none;
            width: 16px;
            height: 1px;
            margin: -6px 0; /* centers the handle */
            border-radius: 8px;
        }}
        
        """
    
    # -------------------------------------------------------
        
    def get_dark_style_sheet(self):
        highlight_color = "#f0f"
        dark_background = "#111"
        dark_color = "#eee"
        dark_window_background = "#222"
        dark_button_background = "#888"
        dark_button_color = "#fff"
        dark_button_disabled_background = "#444"
        dark_button_disabled_color = "#888"
        dark_title_background = "#222"
        dark_title_color = "#ccc"
        dark_dock_border = DARK_DOCK_BORDER
        dark_cell_border = "#666"
        dark_cell_grid = "#666"

        separator_size = 3

        return f"""
        
        /* FUSION HACK */

        QPushButton {{
            background-color: {dark_button_background};
            color: {dark_button_color};
        }}

        QPushButton:disabled {{
            background-color: {dark_button_disabled_background};
            color: {dark_button_disabled_color};
            border: none;
        }}

        /* WINDOW */

        QMainWindow::separator {{
            background: {dark_dock_border};
        }}

        QMainWindow {{
            /* background: #f0f;
            color: #ff0; */
        }}

        /* WIDGETS */

        QWidget {{
            background: {dark_window_background};
        }}
                        
        QDockWidget {{
            background: transparent;
        }}
                        
        QDockWidget::title {{
            background: {dark_title_background};
            color: {dark_title_color};
        }}

        /* SCROLL BARS */

        QScrollBar {{
            background: transparent;
        }}

        QScrollBar:vertical {{
        }}

        QScrollBar:horizontal {{
        }}

        QScrollBar::handle {{
            background: transparent;
        }}

        QScrollBar::handle:vertical {{
            border-top: 1px solid {highlight_color};
            border-bottom: 1px solid {highlight_color};
        }}

        QScrollBar::handle:horizontal {{
            border-left: 1px solid {highlight_color};
            border-right: 1px solid {highlight_color};
        }}

        QScrollBar::add-line:vertical,
        QScrollBar::sub-line:vertical {{
            color: transparent;
            background: transparent;
        }}

        QScrollBar::add-page,
        QScrollBar::sub-page {{
            color: transparent;
            background: transparent;
        }}

        QScrollBar::add-page:horizontal,
        QScrollBar::sub-page:horizontal {{
            border-top: 1px solid {highlight_color};
        }}

        QScrollBar::add-page:vertical,
        QScrollBar::sub-page:vertical {{
            border-left: 1px solid {highlight_color};
        }}

        /* LISTS */

        QListWidget, QListView, QListWidget::item {{
            background: transparent;
        }}

        QListWidget::item {{
            border-bottom: {separator_size}px solid {dark_dock_border};
        }}

        /* TABLES */

        QHeaderView {{
        }}

        QTableView {{
            gridline-color: {dark_cell_grid};
            border-bottom: 1px solid {dark_cell_border};
        }}

        QHeaderView::section:horizontal {{
            background: transparent;
            color: {dark_title_color};
            border: 0px solid {dark_cell_border};
            border-bottom: 1px solid {dark_cell_border};
            border-right: 1px solid {dark_cell_border};
        }}

        QHeaderView::section:horizontal:last {{
            border-right: none;
        }}

        QHeaderView::section:vertical {{
            background: transparent;
            color: {dark_title_color};
            border: 0px solid {dark_cell_border};
            border-right: 1px solid {dark_cell_border};
            border-bottom: 1px solid {dark_cell_border};
        }}

        QHeaderView::section:vertical:first {{
        }}

        QHeaderView::section:vertical:last {{
            border-bottom: none;
        }}

        QTableCornerButton::section {{
            background: transparent;
        }}

        /* DROP DOWN */

        QComboBox {{
            background: {dark_button_background};
            color: {dark_button_color};
        }}

        QComboBox:disabled {{
            background: {dark_button_disabled_background};
            color: {dark_button_disabled_color};
        }}

        QComboBox::drop-down {{
            background: {dark_button_background};
        }}

        QComboBox::down-arrow {{
        }}

        QComboBox QAbstractItemView {{
            /*background: #f0f;
            border: none;*/
        }}

        /* CHECK BOXES */

        QCheckBox::indicator:unchecked {{
            image: url('ui/icons/checkbox_dark_off.png');
        }}

        QCheckBox::indicator:checked {{
        }}

        /* TABS */

        QTabBar::tab {{
            background: {dark_button_disabled_background};    
            color: {dark_button_disabled_color};              
        }}

        QTabBar::tab:selected {{
            background: {dark_button_background};
            color: {dark_button_color};
        }}

        /* TEXT EDITS AND LINE EDITS */
        
        QTextEdit {{
            selection-background-color: {highlight_color};
            selection-color: #fff;
        }}

        QLineEdit {{
            border: 1px solid #888;
        }}

        QLineEdit:focus {{
            border: 1px solid {highlight_color};
            background: {highlight_color};
            color: #fff;
        }}

        /* LABELS */

        QLabel {{
            color: {dark_color};
        }}

        /* BUTTONS */
        
        QPushButton {{
            background: {dark_button_background};
            color: {dark_button_color};
        }}

        QPushButton:pressed {{
            background: {highlight_color};
        }}

        QPushButton:disabled {{
            background: {dark_button_disabled_background};
            color: {dark_button_disabled_color};
        }}

        /* SLIDER */

        QSlider {{
            background: transparent;
        }}

        QSlider::groove:horizontal {{
            background: {dark_button_disabled_background};
        }}
        
        QSlider::sub-page:horizontal {{
            background: {dark_button_background};
        }}

        QSlider::handle:horizontal:pressed {{
            background: {highlight_color};
        }}

        QSlider::handle:horizontal:disabled {{
            background: {dark_button_disabled_background};
        }}

        QSlider::handle:horizontal {{
            background: {dark_button_color};
        }}
                
        """

    # -------------------------------------------------------

    def get_light_style_sheet(self):
        highlight_color = "#f0f"
        light_background = "#eee"
        light_color = "#111"
        light_window_background = "#ddd"
        light_button_background = "#888"
        light_button_color = "#eee"
        light_button_disabled_background = "#ccc"
        light_button_disabled_color = "#888"
        light_title_background = "#ddd"
        light_title_color = "#333"
        light_dock_border = LIGHT_DOCK_BORDER
        light_cell_border = "#999"
        light_cell_grid = "#999"

        separator_size = 3

        return f"""
        
        /* FUSION HACK */

        QPushButton {{
            background-color: {light_button_background};
            color: {light_button_color};
        }}

        QPushButton:disabled {{
            background-color: {light_button_disabled_background};
            color: {light_button_disabled_color};
            border: none;
        }}

        /* WINDOW */

        QMainWindow::separator {{
            background: {light_dock_border};
        }}

        QMainWindow {{
            /* background: #f0f;
            color: #ff0; */
        }}

        /* WIDGETS */

        QWidget {{
            background: {light_window_background};
        }}
                        
        QDockWidget {{
            background: transparent;
        }}
                        
        QDockWidget::title {{
            background: {light_title_background};
            color: {light_title_color};
        }}

        /* SCROLL BARS */

        QScrollBar {{
            background: transparent;
            border: none;
        }}

        QScrollBar:vertical {{
        }}

        QScrollBar:horizontal {{
        }}

        QScrollBar::handle {{
            background: transparent;
        }}

        QScrollBar::handle:vertical {{
            border-top: 1px solid {highlight_color};
            border-bottom: 1px solid {highlight_color};
        }}

        QScrollBar::handle:horizontal {{
            border-left: 1px solid {highlight_color};
            border-right: 1px solid {highlight_color};
        }}

        QScrollBar::add-line:vertical,
        QScrollBar::sub-line:vertical {{
            color: transparent;
            background: transparent;
        }}

        QScrollBar::add-page,
        QScrollBar::sub-page {{
            color: transparent;
            background: transparent;
        }}

        QScrollBar::add-page:horizontal,
        QScrollBar::sub-page:horizontal {{
            border-top: 1px solid {highlight_color};
        }}

        QScrollBar::add-page:vertical,
        QScrollBar::sub-page:vertical {{
            border-left: 1px solid {highlight_color};
        }}

        /* LISTS */

        QListWidget, QListView, QListWidget::item {{
            background: transparent;        
        }}

        QListWidget::item {{
            border-bottom: {separator_size}px solid {light_dock_border};
        }}

        /* TABLES */

        QHeaderView {{
        }}

        QTableView {{
            gridline-color: {light_cell_grid};
            border-bottom: 1px solid {light_cell_border};
        }}

        QHeaderView::section:horizontal {{
            background: transparent;
            color: {light_title_color};
            border: 0px solid {light_cell_border};
            border-right: 1px solid {light_cell_border};
            border-bottom: 1px solid {light_cell_border};
        }}

        QHeaderView::section:horizontal:last {{
            border-right: none;
        }}

        QHeaderView::section:vertical {{
            background: transparent;
            color: {light_title_color};
            border: 0px solid {light_cell_border};
            border-right: 1px solid {light_cell_border};
            border-bottom: 1px solid {light_cell_border};
        }}

        QHeaderView::section:vertical:first {{
        }}

        QHeaderView::section:vertical:last {{
            border-bottom: none;
        }}

        QTableCornerButton::section {{
            background: transparent;
        }}

        /* LINE EDITS AND TEXT EDITS */
        
        QTextEdit {{
            selection-background-color: {highlight_color};
            selection-color: #fff;
        }}

        QLineEdit {{
            border: 1px solid #888;
        }}

        QLineEdit:focus {{
            border: 1px solid {highlight_color};
            background: {highlight_color};
            color: #fff;
        }}

        /* LABELS */

        QLabel {{
            color: {light_color};
        }}

        /* DROP DOWN */

        QComboBox {{
            background: {light_button_background};
            color: {light_button_color};
        }}

        QComboBox:disabled {{
            background: {light_button_disabled_background};
            color: {light_button_disabled_color};
        }}

        QComboBox::drop-down {{
            background: {light_button_background};
        }}

        QComboBox::down-arrow {{
        }}

        QComboBox QAbstractItemView {{
            background: {light_button_background};
        }}

        /* CHECK BOXES */

        QCheckBox::indicator:unchecked {{
            image: url('ui/icons/checkbox_light_off.png');
        }}

        QCheckBox::indicator:checked {{
        }}   

        /* TABS */

        QTabBar::tab {{
            background: {light_button_disabled_background};    /* Unselected tab background */
            color: {light_button_disabled_color};              /* Unselected tab text */
        }}

        QTabBar::tab:selected {{
            background: {light_button_background};    /* Selected tab background */
            color: {light_button_color};              /* Selected tab text */
        }}

        /* BUTTONS */
        
        QPushButton {{
            background: {light_button_background};
            color: {light_button_color};
        }}

        QPushButton:pressed {{
            background: {highlight_color};
        }}

        QPushButton:disabled {{
            background: {light_button_disabled_background};
            color: {light_button_disabled_color};
        }}
        
        /* SLIDER */

        QSlider {{
            background: transparent;
        }}

        QSlider::groove:horizontal {{
            background: {light_button_disabled_background};
        }}
        
        QSlider::sub-page:horizontal {{
            background: {light_button_background};
        }}

        QSlider::handle:horizontal:pressed {{
            background: {highlight_color};
        }}

        QSlider::handle:horizontal:disabled {{
            background: {light_button_disabled_background};
        }}

        QSlider::handle:horizontal {{
            background: {light_button_background};
        }}
                
        """
