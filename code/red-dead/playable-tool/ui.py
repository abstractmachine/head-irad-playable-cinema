DEBUG = False # Set to True to enable debug output

from PyQt5.QtGui import QFont, QFontDatabase
import os

class UI:
    def __init__(self):
        # Map styles to font file paths and default sizes
        self.fonts = {
            # style: (family, size, weight, italic)
            'tiny':     ('Roboto Mono', 12, QFont.Bold, False),
            'button':   ('Roboto', 12, QFont.Normal, False),
            'collumn':  ('Roboto', 10, QFont.Normal, False),
            'cell':     ('Roboto', 12, QFont.Normal, False),
            'cell-tiny':('Roboto', 12, QFont.Thin, False),
            'cell-text':('Hanken Grotesk', 13, QFont.Normal, False),
            'cell-mono':('Roboto Mono', 12, QFont.Normal, False),
            'text':     ('Hanken Grotesk', 24, QFont.Normal, False),
            'italic':   ('Hanken Grotesk', 14, QFont.Normal, True),
            'black':    ('Roboto Serif', 16, QFont.Black, False),
            'prompt':   ('Roboto Mono', 14, QFont.Normal, False),
            'monospace':('Roboto Mono', 14, QFont.Normal, False),
            'title':    ('Roboto Slab', 17, QFont.Black, False),
            'year':     ('Roboto Slab', 14, QFont.Normal, False),
            'director': ('Roboto Slab', 14, QFont.Normal, False),
            'tagline':  ('Roboto Serif', 14, QFont.Thin, True),
            'default':  ('Roboto', 16, QFont.Normal, False)
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

    def get_dimensions(self, style=None):
        if style == 'tiny':
            return (28, 28)
        elif style == 'button':
            return (100, 32)
        else:
            return (140, 32)