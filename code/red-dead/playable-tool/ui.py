DEBUG = False # Set to True to enable debug output

from PyQt5.QtGui import QFont, QFontDatabase
import os
import subprocess

class UI:
    def __init__(self):
        # Map styles to font file paths and default sizes
        self.fonts = {
            # style: (family, size, weight, italic)
            'tiny':     ('Roboto Mono', 12, QFont.Bold, False),
            'tiny-condensed': ('Roboto Condensed', 12, QFont.Normal, False),
            'button':   ('Roboto', 12, QFont.Normal, False),
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
            'title':    ('Roboto Slab', 17, QFont.Black, False),
            'year':     ('Roboto Slab', 14, QFont.Normal, False),
            'director': ('Roboto Slab', 14, QFont.Normal, False),
            'tagline':  ('Roboto Serif', 13, QFont.Light, True),
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
            return (100, 32)
        else:
            return (140, 32)
        
    def get_style_sheet(self, style=None):
        if style == 'app':
            if self.is_dark_mode():
                return self.get_dark_style_sheet()
            else:
                return self.get_light_style_sheet()
        else:
            return ""
        
    def get_dark_style_sheet(self):
        highlight_color = "#f0f"
        dark_background = "#111"
        dark_color = "#eee"
        dark_button_background = "#888"
        dark_button_color = "#fff"
        dark_button_disabled_background = "#444"
        dark_button_disabled_color = "#666"
        dark_title_background = "#222"
        dark_title_color = "#ccc"
        dark_dock_border = "#111"

        # get the button font
        button_font = self.get_font("button")
        # get that font's name
        button_font_name = button_font.family()
        button_font_size = button_font.pointSize()
        button_font_weight = button_font.weight()
        # get the button dimensions
        # button_width = 80
        button_height = 24

        return f"""

        /* WINDOW */

        QMainWindow::separator {{
            background: {dark_dock_border};
            width: 2px;
            height: 2px;
        }}

        QMainWindow {{
            /* background: #f0f;
            color: #ff0; */
            border: none;
            outline: none;
        }}

        /* WIDGETS */

        QWidget {{
            background: {dark_background};
            border: none;
            outline: none;
        }}
                        
        QDockWidget {{
            border: none;
            background: transparent;
        }}
                        
        QDockWidget::title {{
            background: {dark_title_background};
            color: {dark_title_color};
            text-align: center;
            padding-left: 8px;
            padding-top: 2px;
            padding-bottom: 2px;
        }}

        /* DROP DOWN */

        QComboBox {{
            background: {dark_button_background};         /* Background of the combo box */
            color: {dark_button_color};              /* Text color */
            border: none;   /* Border */
            border-radius: 10%;
            padding: 0px 0x 0px 20px; /* Space for the arrow */
            /* Font settings */
            font-family: {button_font_name};
            font-size: {button_font_size}px;
            font-weight: {button_font_weight};
            min-height: {button_height}px;
        }}

        QComboBox:disabled {{
            background: {{dark_button_disabled_background}};
            color: {{dark_button_disabled_color}};
        }}

        QComboBox::drop-down {{
            subcontrol-origin: padding;
            subcontrol-position: top right;
            width: 20px;
            border-radius: 10%;
            background: {dark_button_background};
        }}

        QComboBox::down-arrow {{
            image: url('ui/icons/dropdown-dark.png');
            width: 8px;
            height: 8px;
        }}

        QComboBox QAbstractItemView {{
            background: #f0f;
            /*color: #0f0;*/
            border: none;
            /*selection-background-color: #00f;
            selection-color: #f0f;*/
        }}

        /* TABS */

        QTabBar::tab {{
            background: {dark_button_disabled_background};    /* Unselected tab background */
            color: {dark_button_disabled_color};              /* Unselected tab text */
            border: none;
            border-bottom: none;
            padding: 2px 2px;
            min-width: 80px;
            border-radius: 0px;
            font-family: {button_font_name};
            font-size: {button_font_size}px;
            font-weight: {button_font_weight};
            min-height: {button_height}px;
        }}

        QTabBar::tab:selected {{
            background: {dark_button_background};    /* Selected tab background */
            color: {dark_button_color};              /* Selected tab text */
            border: none;
            border-radius: 0px;
        }}

        QTabBar::tab:!selected {{
            margin-top: 0px;          /* Slight offset for unselected tabs */
            border-radius: 0px;
        }}

        QTabWidget {{
            background: pink; /* Background for the tab widget */
            color: orange;           /* Text color for the tab widget */  
            border-radius: 0px;
        }}

        QTabWidget::pane {{
            border: none;    /* or your preferred border */
            background: #0ff;          /* match your window background */
            border-radius: 0px;        /* remove rounded corners */
            top: 0px;                  /* align with tab bar */
            border-radius: 0px;
        }}

        QLineEdit, QTextEdit {{
            border: 1px solid #888;
            border-radius: 0px;
        }}

        /* BUTTONS */
        
        QPushButton {{
            background: {dark_button_background};
            color: {dark_button_color};
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
            background: {highlight_color};
        }}

        QPushButton:disabled {{
            background: {dark_button_disabled_background};
            color: {dark_button_disabled_color};
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
            background: {dark_button_disabled_background};
            border-radius: 2px;
        }}
        
        QSlider::sub-page:horizontal {{
            background: {dark_button_background};
            border-radius: 2px;
        }}

        QSlider::handle:horizontal:pressed {{
            background: {highlight_color};
        }}

        QSlider::handle:horizontal:disabled {{
            background: {dark_button_disabled_background};
        }}

        QSlider::handle:horizontal {{
            background: {dark_button_color};
            border: none;
            width: 16px;
            height: 16px;
            margin: -6px 0; /* centers the handle */
            border-radius: 8px;
        }}
                
        """

    def get_light_style_sheet(self):
        highlight_color = "#f0f"
        light_background = "#eee"
        light_color = "#111"
        light_button_background = "#888"
        light_button_color = "#eee"
        light_button_disabled_background = "#ccc"
        light_button_disabled_color = "#eee"
        light_title_background = "#ddd"
        light_title_color = "#333"
        light_dock_border = "#eee"

        # get the button font
        button_font = self.get_font("button")
        # get that font's name
        button_font_name = button_font.family()
        button_font_size = button_font.pointSize()
        button_font_weight = button_font.weight()
        # get the button dimensions
        # button_width = 80
        button_height = 24

        return f"""

        /* WINDOW */

        QMainWindow::separator {{
            background: {light_dock_border};
            width: 2px;
            height: 2px;
        }}

        QMainWindow {{
            /* background: #f0f;
            color: #ff0; */
            border: none;
            outline: none;
        }}

        /* WIDGETS */

        QWidget {{
            background: {light_background};
            border: none;
            outline: none;
        }}
                        
        QDockWidget {{
            border: none;
            background: transparent;
        }}
                        
        QDockWidget::title {{
            background: {light_title_background};
            color: {light_title_color};
            text-align: center;
            padding-left: 8px;
            padding-top: 2px;
            padding-bottom: 2px;
        }}

        /* LINE EDITS AND TEXT EDITS */

        QLineEdit, QTextEdit {{
            border: 1px solid #888;
            border-radius: 0px;
        }}

        /* DROP DOWN */

        QComboBox {{
            background: {light_button_background};         /* Background of the combo box */
            color: {light_button_color};              /* Text color */
            border: none;   /* Border */
            border-radius: 10%;
            padding: 0px 0x 0px 20px; /* Space for the arrow */
            /* Font settings */
            font-family: {button_font_name};
            font-size: {button_font_size}px;
            font-weight: {button_font_weight};
            min-height: {button_height}px;
        }}

        QComboBox:disabled {{
            background: {{light_button_disabled_background}};
            color: {{light_button_disabled_color}};
        }}

        QComboBox::drop-down {{
            subcontrol-origin: padding;
            subcontrol-position: top right;
            width: 20px;
            border-radius: 10%;
            background: {light_button_background};
        }}

        QComboBox::down-arrow {{
            image: url('ui/icons/dropdown-dark.png');
            width: 8px;
            height: 8px;
        }}

        QComboBox QAbstractItemView {{
            background: #f0f;
            /*color: #0f0;*/
            border: none;
            /*selection-background-color: #00f;
            selection-color: #f0f;*/
        }}        

        /* TABS */

        QTabBar::tab {{
            background: {light_button_disabled_background};    /* Unselected tab background */
            color: {light_button_disabled_color};              /* Unselected tab text */
            border: none;
            border-bottom: none;
            padding: 2px 2px;
            min-width: 80px;
            border-radius: 0px;
            font-family: {button_font_name};
            font-size: {button_font_size}px;
            font-weight: {button_font_weight};
            min-height: {button_height}px;
        }}

        QTabBar::tab:selected {{
            background: {light_button_background};    /* Selected tab background */
            color: {light_button_color};              /* Selected tab text */
            border: none;
            border-radius: 0px;
        }}

        QTabBar::tab:!selected {{
            margin-top: 0px;          /* Slight offset for unselected tabs */
            border-radius: 0px;
        }}

        QTabWidget {{
            background: {light_background};
            border: none;
        }}

        QTabWidget::pane {{
            background: {light_background};
            border: none;
            border-radius: 0px;
            top: 0px;
        }}

        /* LINE EDITS AND TEXT EDITS */

        QLineEdit, QTextEdit {{
            border: 1px solid #888;
            border-radius: 0px;
        }}

        /* BUTTONS */
        
        QPushButton {{
            background: {light_button_background};
            color: {light_button_color};
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
            background: {highlight_color};
        }}

        QPushButton:disabled {{
            background: {light_button_disabled_background};
            color: {light_button_disabled_color};
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
            background: {light_button_disabled_background};
            border-radius: 2px;
        }}
        
        QSlider::sub-page:horizontal {{
            background: {light_button_background};
            border-radius: 2px;
        }}

        QSlider::handle:horizontal:pressed {{
            background: {highlight_color};
        }}

        QSlider::handle:horizontal:disabled {{
            background: {light_button_disabled_background};
        }}

        QSlider::handle:horizontal {{
            background: {light_button_background};
            border: none;
            width: 16px;
            height: 16px;
            margin: -6px 0; /* centers the handle */
            border-radius: 8px;
        }}
                
        """
