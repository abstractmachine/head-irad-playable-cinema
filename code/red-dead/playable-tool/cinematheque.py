DEBUG = True  # Set to True to enable debug output

from PyQt5.QtCore import Qt, pyqtSignal, QSize, QThread, QTimer
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QPushButton, QListWidget, QListWidgetItem, QLabel, QSizePolicy, 
    QFileDialog, QMessageBox
)
from PyQt5.QtGui import QPixmap, QColor
import os
import csv
from catalog import AbstractCatalogWindow
from catalog_item import MovieItemWidget

class CinemathequeWindow(AbstractCatalogWindow):
    
    # Additional signals specific to cinematheque
    shotlist_bot_start = pyqtSignal()  # Signal to start shotlist bot
    request_caption_bot_autostart = pyqtSignal()
    
    def __init__(self, ui):
        # Set catalog-specific properties before calling super().__init__()
        self.catalog_name = "Cinemathèque"
        self.data_folder = "movies"
        self.metadata_file = "cinematheque.csv"
        
        if DEBUG: 
            print(f"DEBUG: CinemathequeWindow: Setting metadata_file to '{self.metadata_file}'")
            print(f"DEBUG: CinemathequeWindow: Setting data_folder to '{self.data_folder}'")
        
        # Call parent constructor
        super().__init__(ui)
        
        if DEBUG: 
            print(f"DEBUG: CinemathequeWindow: After super().__init__(), metadata_file = '{self.metadata_file}'")
        
        # Bot-related properties
        self.shotlist_bot_active = False
        self.shotlist_bot_anim_timer = QTimer()
        self.shotlist_bot_anim_timer.timeout.connect(self.animate_shotlist_bot)
        self.shotlist_bot_dots = 0
        
        # Add movie_selected signal as alias for item_selected
        self.movie_selected = self.item_selected
        
    def create_button_layout(self):
        """Create the button layout with cinematheque-specific buttons"""
        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(0)
        
        # Get button dimensions
        button_width, button_height = self.ui.get_dimensions('button')
        
        # Metadata rebuild button
        self.metadata_button = QPushButton("Rebuild Metadata")
        self.metadata_button.setFont(self.ui.get_font('button'))
        self.metadata_button.clicked.connect(self.rebuild_metadata)
        self.metadata_button.setEnabled(False)
        self.metadata_button.setFixedSize(160, button_height)
        
        # Bot buttons
        self.shotlist_bot_button = QPushButton("Shotlist Bot Off")
        self.shotlist_bot_button.setFont(self.ui.get_font('button'))
        self.shotlist_bot_button.setFixedSize(140, button_height)
        self.shotlist_bot_button.clicked.connect(self.handle_shotlist_bot)
        self.disable_shotlist_bot_button()

        self.scene_bot_button = QPushButton("Scene Bot Off")
        self.scene_bot_button.setFont(self.ui.get_font('button'))
        self.scene_bot_button.setFixedSize(140, button_height)
        self.scene_bot_button.setEnabled(False)
        self.scene_bot_button.clicked.connect(self.handle_caption_bot)

        self.caption_bot_button = QPushButton("Caption Bot Off")
        self.caption_bot_button.setFont(self.ui.get_font('button'))
        self.caption_bot_button.setFixedSize(140, button_height)
        self.caption_bot_button.setEnabled(False)
        self.caption_bot_button.clicked.connect(self.handle_caption_bot)

        button_layout.addWidget(self.metadata_button)
        button_layout.addWidget(self.shotlist_bot_button)
        button_layout.addWidget(self.scene_bot_button)
        button_layout.addWidget(self.caption_bot_button)
        button_layout.addStretch()
        
        return button_layout
    
    def get_assets_folder_name(self):
        """Get the name of the assets folder"""
        return "posters"
    
    def create_item_widget(self, item_data, assets_folder):
        """Create a movie item widget"""
        return MovieItemWidget(item_data, assets_folder, self.ui)
    
    def get_item_path(self, item_data):
        """Get the full path for a movie"""
        filename = item_data.get('filename', '')
        if filename and self.project_folder:
            return os.path.join(self.project_folder, "movies", filename)
        return None

    # Override the item list to use movie_list for backward compatibility
    @property
    def movie_list(self):
        return self.item_list
    
    @property
    def currently_loading_video(self):
        return self.currently_loading_item
    
    @currently_loading_video.setter
    def currently_loading_video(self, value):
        self.currently_loading_item = value
    
    @property
    def selected_movie_widget(self):
        return self.selected_item_widget
    
    @selected_movie_widget.setter
    def selected_movie_widget(self, value):
        self.selected_item_widget = value
    
    def on_movie_clicked(self, item):
        """Handle movie item click - alias for on_item_clicked"""
        if DEBUG: print(f"DEBUG: on_movie_clicked called with item: {item}")
        self.turn_off_all_bots()
        self.on_item_clicked(item)
    
    def _set_new_selection(self, item_widget):
        """Override to update bot button state after selection"""
        super()._set_new_selection(item_widget)
        QTimer.singleShot(20, self.update_shotlist_bot_button_state)
    
    def update_movie_list(self):
        """Update movie list - alias for update_item_list"""
        self.update_item_list()
    
    def load_movies_from_metadata(self, metadata_path, project_folder):
        """Load movies from metadata - alias for load_items_from_metadata"""
        self.load_items_from_metadata(metadata_path, project_folder)
    
    # ---- Bot Methods ----
    
    def shot_bot_finished(self):
        """Handle bot finished signal"""
        if DEBUG: print("DEBUG: Cinematheque: bot finished")
        was_caption_bot_active = self.caption_bot_button.text().startswith("    Caption Bot On")
        count = self.movie_list.count()
        if count == 0 or not self.selected_movie_widget:
            if DEBUG: print("DEBUG: No movies or no selection, returning")
            self.turn_off_all_bots()
            return

        for i in range(count):
            widget = self.movie_list.itemWidget(self.movie_list.item(i))
            if widget == self.selected_movie_widget:
                next_index = i + 1
                if next_index < count:
                    if DEBUG: print(f"DEBUG: Moving to next movie at index {next_index}")
                    next_item = self.movie_list.item(next_index)
                    self._direct_select_item(next_item)
                    self.scroll_to_item(next_index)
                    # Start the Caption Bot for the next movie after a short delay
                    if was_caption_bot_active:
                        QTimer.singleShot(500, self.start_caption_bot)
                else:
                    if DEBUG: print("DEBUG: Already at last movie, turning off bots")
                    self.turn_off_all_bots()
                break

    def start_caption_bot(self):
        if self.caption_bot_button.isEnabled():
            if DEBUG: print("DEBUG: Starting Caption Bot for next movie")
            self.request_caption_bot_autostart.emit()
            self.caption_bot_button.click()

    def turn_off_all_bots(self):
        """Turn off all running bots and reset their buttons."""
        if self.shotlist_bot_active:
            self.shotlist_bot_active = False
            self.shotlist_bot_button.setText("Shotlist Bot Off")
            self.shotlist_bot_button.setStyleSheet("QPushButton { text-align: center; }")
            self.shotlist_bot_anim_timer.stop()
        self.scene_bot_button.setText("Scene Bot Off")
        self.scene_bot_button.setStyleSheet("QPushButton { text-align: center; }")
        self.caption_bot_button.setText("Caption Bot Off")
        self.caption_bot_button.setStyleSheet("QPushButton { text-align: center; }")

    def update_shotlist_bot_button_state(self):
        self.enable_shotlist_bot_button()

    def enable_shotlist_bot_button(self):
        # Enable only if a movie is currently selected
        self.shotlist_bot_button.setEnabled(self.selected_movie_widget is not None)

    def disable_shotlist_bot_button(self):
        self.shotlist_bot_button.setEnabled(False)

    def handle_shotlist_bot(self):
        if not self.shotlist_bot_active:
            self.shotlist_bot_active = True
            self.shotlist_bot_button.setText("      Shotlist Bot On")
            self.shotlist_bot_button.setStyleSheet("QPushButton { text-align: left; }")
            self.shotlist_bot_anim_timer.start(500)
            # Send signal to shotlist to start detection
            self.shotlist_bot_start.emit()
        else:
            # Optionally allow stopping the bot
            self.shotlist_bot_active = False
            self.shotlist_bot_button.setText("Shotlist Bot Off")
            self.shotlist_bot_button.setStyleSheet("QPushButton { text-align: center; }")
            self.shotlist_bot_anim_timer.stop()

    def animate_shotlist_bot(self):
        self.shotlist_bot_dots = (self.shotlist_bot_dots + 1) % 4
        dots = "." * self.shotlist_bot_dots
        self.shotlist_bot_button.setText(f"      Shotlist Bot On{dots}")
        self.shotlist_bot_button.setStyleSheet("QPushButton { text-align: left; }")

    def on_shotlist_status(self, finished):
        # Called by shotlist.py when detection is finished
        if finished and self.shotlist_bot_active:
            # Select next movie in the list
            self.select_next_movie()
        elif not finished:
            # Detection is still running, keep animating
            pass

    def select_next_movie(self):
        if DEBUG: print("DEBUG: select_next_movie() called")
        count = self.item_list.count()
        if DEBUG: print(f"DEBUG: Total movie count: {count}")
        if DEBUG: print(f"DEBUG: Current selected_item_widget: {self.selected_item_widget}")
        if DEBUG: print(f"DEBUG: Project folder: {self.project_folder}")
        
        # If nothing is selected but there are movies and a project folder, select the first movie
        if count > 0 and self.project_folder and not self.selected_item_widget:
            if DEBUG: print("DEBUG: No selection, selecting first movie")
            self.turn_off_all_bots()
            first_item = self.item_list.item(0)
            self._direct_select_item(first_item)
            self.scroll_to_item(0)  # Changed from scroll_to_movie
            return

        if count == 0 or not self.selected_item_widget:
            if DEBUG: print("DEBUG: No movies or no selection, returning")
            self.turn_off_all_bots()
            return
            
        if DEBUG: print("DEBUG: Looking for current selection in list")
        for i in range(count):
            widget = self.item_list.itemWidget(self.item_list.item(i))
            if DEBUG: print(f"DEBUG: Checking index {i}, widget: {widget}")
            if widget == self.selected_movie_widget:
                if DEBUG: print(f"DEBUG: Found current selection at index {i}")
                next_index = i + 1
                if next_index < count:
                    if DEBUG: print(f"DEBUG: Moving to next index {next_index}")
                    self.turn_off_all_bots()
                    next_item = self.item_list.item(next_index)
                    if DEBUG: print(f"DEBUG: About to call _direct_select_item with item: {next_item}")
                    self._direct_select_item(next_item)
                    self.scroll_to_item(next_index)  # Changed from scroll_to_movie
                else:
                    if DEBUG: print("DEBUG: Already at last movie")
                    self.turn_off_all_bots()
                break

    def select_previous_movie(self):
        if DEBUG: print("DEBUG: select_previous_movie() called")
        count = self.item_list.count()
        if DEBUG: print(f"DEBUG: Total movie count: {count}")
        if DEBUG: print(f"DEBUG: Current selected_item_widget: {self.selected_movie_widget}")
        
        if count == 0 or not self.selected_movie_widget:
            if DEBUG: print("DEBUG: No movies or no selection, returning")
            return
            
        if DEBUG: print("DEBUG: Looking for current selection in list")
        for i in range(count):
            widget = self.item_list.itemWidget(self.item_list.item(i))
            if DEBUG: print(f"DEBUG: Checking index {i}, widget: {widget}")
            if widget == self.selected_movie_widget:
                if DEBUG: print(f"DEBUG: Found current selection at index {i}")
                prev_index = i - 1
                if prev_index >= 0:
                    if DEBUG: print(f"DEBUG: Moving to previous index {prev_index}")
                    self.turn_off_all_bots()
                    prev_item = self.item_list.item(prev_index)
                    if DEBUG: print(f"DEBUG: About to call _direct_select_item with item: {prev_item}")
                    self._direct_select_item(prev_item)
                    self.scroll_to_item(prev_index)  # Changed from scroll_to_movie
                else:
                    if DEBUG: print("DEBUG: Already at first movie")
                break

    def _direct_select_item(self, item):
        """Directly select an item without the delayed mechanism"""
        if DEBUG: print(f"DEBUG: _direct_select_item called with item: {item}")
        
        if item is None:
            if DEBUG: print(f"DEBUG: Item is None, returning")
            return
            
        item_widget = self.item_list.itemWidget(item)
        if DEBUG: print(f"DEBUG: Item widget: {item_widget}")
        
        if not item_widget or not hasattr(item_widget, 'item_data'):
            if DEBUG: print(f"DEBUG: No widget or item_data, returning")
            return

        # Clear previous selection
        if self.selected_item_widget and self.selected_item_widget != item_widget:
            if DEBUG: print(f"DEBUG: Clearing previous selection: {self.selected_item_widget}")
            self.selected_item_widget.set_selected(False)
            self.selected_item_widget.update()

        # Set new selection immediately
        if DEBUG: print(f"DEBUG: Setting new selection: {item_widget}")
        item_widget.set_selected(True)
        item_widget.update()
        self.selected_item_widget = item_widget
        
        # Get item path and emit selection signal
        item_data = item_widget.item_data
        item_path = self.get_item_path(item_data)
        
        if DEBUG: print(f"DEBUG: Item path: {item_path}")
        
        if item_path and os.path.exists(item_path):
            if DEBUG: print(f"DEBUG: Emitting item_selected signal")
            self.currently_loading_item = item_path
            self.item_selected.emit(item_path, item_data)
            
            # Update bot button state
            QTimer.singleShot(20, self.update_shotlist_bot_button_state)
        else:
            if DEBUG: print(f"DEBUG: File not found")
            QMessageBox.warning(self, "File Not Found", f"Item file not found:\n{item_path}")

    def on_movie_loaded_with_metadata(self, movie_path, metadata):
        """Handle when a movie is loaded - alias for on_item_loaded_with_metadata"""
        self.on_item_loaded_with_metadata(movie_path, metadata)
        # If bot is still active, start detection again
        if self.shotlist_bot_active:
            self.shotlist_bot_start.emit()

    def handle_caption_bot(self):
        print("Caption Bot button pressed.")