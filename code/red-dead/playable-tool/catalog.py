DEBUG = False  # Set to True to enable debug output

from PyQt5.QtCore import Qt, pyqtSignal, QSize, QThread, QTimer
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QPushButton, QListWidget, QListWidgetItem, QSizePolicy, 
    QMessageBox, QAbstractItemView
)

# OS Stuff
import os
import csv
from metadata import MetadataWorker, read_metadata_csv
from catalog_item import AbstractCatalogItemWidget, MovieItemWidget, ITEM_HEIGHT

class AbstractCatalogWindow(QMainWindow):
    """Abstract base class for catalog windows"""
    
    # Define signals for communication
    request_save = pyqtSignal()
    request_load = pyqtSignal(dict)
    catalog_started_loading = pyqtSignal()
    catalog_cleared_contents = pyqtSignal()
    catalog_finished_loading = pyqtSignal()
    item_selected = pyqtSignal(str, dict)  # Signal to send item path AND metadata
    
    def __init__(self, ui):
        super().__init__()
        self.ui = ui
        self.project_folder = None  # Current project folder
        self.currently_loading_item = None  # Track what item is currently being requested
        self.selected_item_widget = None  # Track currently selected item widget
        
        if DEBUG:
            print(f"DEBUG: AbstractCatalogWindow.__init__: metadata_file = '{getattr(self, 'metadata_file', 'NOT_SET')}'")
        
        # Only set defaults if not already set by subclass
        if not hasattr(self, 'catalog_name'):
            self.catalog_name = "Catalog"
        if not hasattr(self, 'data_folder'):
            self.data_folder = ""
        if not hasattr(self, 'metadata_file'):
            self.metadata_file = ""
            
        if DEBUG:
            print(f"DEBUG: AbstractCatalogWindow.__init__: After defaults, metadata_file = '{self.metadata_file}'")
        
        self.setup_ui()
        self.setup_connections()
        
    def setup_connections(self):
        """Setup signal connections - can be overridden by subclasses"""
        # Base class has no connections to set up by default
        # Subclasses can override this to add their own connections
        pass
        
    def setup_ui(self):
        """Setup the main UI - can be overridden by subclasses"""
        # Create main widget and layout
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        
        layout = QVBoxLayout()
        layout.setSpacing(0)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Item list viewer
        self.item_list = QListWidget()
        self.item_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.item_list.setAlternatingRowColors(False)
        self.item_list.setSpacing(0)
        self.item_list.setSelectionMode(QListWidget.NoSelection)

        self.item_list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.item_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.item_list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.item_list.setUniformItemSizes(True)
        self.item_list.setAutoScroll(False)

        layout.addWidget(self.item_list)
        
        # Button layout
        button_layout = self.create_button_layout()
        layout.addLayout(button_layout)
        main_widget.setLayout(layout)
        
        # Initialize thread variables
        self.metadata_thread = None
        self.metadata_worker = None
        
        # Initialize animation timer for rebuilding button
        self.rebuild_animation_timer = QTimer()
        self.rebuild_animation_timer.timeout.connect(self.animate_rebuild_button)
        self.rebuild_dot_count = 0
    
    def create_button_layout(self):
        """Create the button layout - can be overridden by subclasses"""
        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(0)
        button_width, button_height = self.ui.get_dimensions('button')

        # Metadata rebuild button
        self.metadata_button = QPushButton("Rebuild Metadata")
        self.metadata_button.setFont(self.ui.get_font('button'))
        self.metadata_button.clicked.connect(self.rebuild_metadata)
        self.metadata_button.setEnabled(False)
        self.metadata_button.setFixedSize(160, button_height)

        button_layout.addWidget(self.metadata_button)
        button_layout.addStretch()
        
        return button_layout

    def rebuild_metadata(self):
        """Rebuild metadata for this catalog"""
        if not self.project_folder:
            QMessageBox.warning(self, "No Project", "Please set a project folder first.")
            return

        if DEBUG: print(f"DEBUG: {self.catalog_name}: Starting metadata rebuild")
        
        # Start animation
        self.rebuild_animation_timer.start(500)
        self.metadata_button.setEnabled(False)
        
        # Create worker thread for metadata processing
        self.metadata_thread = QThread()
        self.metadata_worker = MetadataWorker(self.project_folder, self.data_folder, self.metadata_file)
        self.metadata_worker.moveToThread(self.metadata_thread)
        
        # Connect signals
        self.metadata_thread.started.connect(self.metadata_worker.run)
        self.metadata_worker.finished.connect(self.on_metadata_finished)
        self.metadata_worker.finished.connect(self.metadata_thread.quit)
        self.metadata_worker.finished.connect(self.metadata_worker.deleteLater)
        self.metadata_thread.finished.connect(self.metadata_thread.deleteLater)
        
        # Start the thread
        self.metadata_thread.start()

    def animate_rebuild_button(self):
        """Animate the rebuild button text while processing"""
        self.rebuild_dot_count = (self.rebuild_dot_count + 1) % 4
        dots = "." * self.rebuild_dot_count
        self.metadata_button.setText(f"Rebuilding{dots}")

    def on_metadata_finished(self, success, message):
        """Handle when metadata rebuild is finished"""
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Metadata rebuild finished: {success}, {message}")
        
        # Stop animation
        self.rebuild_animation_timer.stop()
        self.metadata_button.setText("Rebuild Metadata")
        self.metadata_button.setEnabled(True)
        
        if success:
            if DEBUG: print(f"DEBUG: {self.catalog_name}: Reloading catalog data after successful rebuild")
            # Reload the catalog data
            self.load_catalog_data()
            # Emit signal that catalog has finished loading
            self.catalog_finished_loading.emit()
        else:
            QMessageBox.critical(self, "Error", f"Failed to rebuild metadata:\n{message}")

    def update_item_list(self):
        """Update the item list display - can be overridden by subclasses"""
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Item list updated with {self.item_list.count()} items")
        # Emit signal that catalog has finished loading
        self.catalog_finished_loading.emit()

    def create_item_widget(self, item_data, assets_folder):
        """Create an item widget - must be overridden by subclasses"""
        raise NotImplementedError("Subclasses must implement create_item_widget()")
    
    def get_item_path(self, item_data):
        """Get the full path for an item - must be overridden by subclasses"""
        raise NotImplementedError("Subclasses must implement get_item_path()")
    
    def get_assets_folder_name(self):
        """Get the name of the assets folder - can be overridden by subclasses"""
        return "assets"
    
    def set_project_folder(self, project_folder):
        """Set project folder and load catalog data"""
        if DEBUG: 
            print(f"DEBUG: {self.catalog_name}: Project folder set to: {project_folder}")
        
        self.project_folder = project_folder
        
        if project_folder:
            # Emit signal that catalog is starting to load
            self.catalog_started_loading.emit()
            # Enable buttons when project is loaded
            self.metadata_button.setEnabled(True)
            # Load catalog data
            self.load_catalog_data()
        else:
            # Clear current data
            self.item_list.clear()
            self.selected_item_widget = None
            self.metadata_button.setEnabled(False)
            # Emit signal that catalog contents are cleared
            self.catalog_cleared_contents.emit()
            if DEBUG: print(f"DEBUG: {self.catalog_name}: Project folder cleared, catalog contents cleared")
    
    def load_catalog_data(self):
        """Load catalog data from project"""
        if not self.project_folder:
            if DEBUG: print(f"DEBUG: {self.catalog_name}: No project folder set")
            return
        
        metadata_path = os.path.join(self.project_folder, "metadata", self.metadata_file)
        if DEBUG: 
            print(f"DEBUG: {self.catalog_name}: Looking for metadata file: {metadata_path}")
            print(f"DEBUG: {self.catalog_name}: metadata_file = '{self.metadata_file}'")
            print(f"DEBUG: {self.catalog_name}: File exists: {os.path.exists(metadata_path)}")
        
        if os.path.exists(metadata_path):
            if DEBUG: print(f"DEBUG: {self.catalog_name}: Loading items from metadata")
            self.load_items_from_metadata(metadata_path, self.project_folder)
        else:
            if DEBUG: print(f"DEBUG: {self.catalog_name}: No metadata file found, showing placeholder")
            # No metadata file, show placeholder
            self.item_list.clear()
            placeholder_item = QListWidgetItem(f"No {self.metadata_file} found. Click 'Rebuild Metadata' to create it.")
            self.item_list.addItem(placeholder_item)

    def load_items_from_metadata(self, metadata_path, project_folder):
        """Load items from metadata file"""
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Loading items from: {metadata_path}")
        
        self.item_list.clear()
        self.catalog_cleared_contents.emit()
        
        # Clear previous selection
        self.selected_item_widget = None
        assets_folder = os.path.join(project_folder, self.get_assets_folder_name())
        
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Assets folder: {assets_folder}")
        
        try:
            # Use the read_metadata_csv function to read the CSV file
            for row in read_metadata_csv(metadata_path):
                if DEBUG: print(f"DEBUG: {self.catalog_name}: Processing row: {row}")
                # Create custom widget for this item
                item_widget = self.create_item_widget(row, assets_folder)
                
                # Connect the widget's clicked signal to handle selection
                if hasattr(item_widget, 'clicked'):
                    item_widget.clicked.connect(lambda data, widget=item_widget: self.on_widget_clicked(widget, data))
                
                # Create list item with fixed height
                item = QListWidgetItem()
                item.setSizeHint(QSize(item_widget.width(), ITEM_HEIGHT))

                # Add to list
                self.item_list.addItem(item)
                self.item_list.setItemWidget(item, item_widget)

            # Now that we've loaded, update the list
            self.update_item_list()

        except Exception as e:
            if DEBUG: print(f"DEBUG: {self.catalog_name}: Error loading metadata: {str(e)}")
            QMessageBox.critical(self, "Error", f"Failed to load {self.metadata_file}:\n{str(e)}")

    def on_widget_clicked(self, item_widget, item_data):
        """Handle clicks from custom item widgets"""
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Widget clicked with data: {item_data}")
        
        # Clear previous selection
        if self.selected_item_widget and self.selected_item_widget != item_widget:
            self.selected_item_widget.set_selected(False)
            self.selected_item_widget.update()
        
        # Set new selection
        item_widget.set_selected(True)
        item_widget.update()
        self.selected_item_widget = item_widget
        
        # Scroll to item only if it's offscreen
        self._scroll_to_selected_item_if_needed()
        
        # Get item path and emit selection signal
        item_path = self.get_item_path(item_data)
        if item_path and os.path.exists(item_path):
            self.currently_loading_item = item_path
            self.item_selected.emit(item_path, item_data)
        else:
            QMessageBox.warning(self, "File Not Found", f"Item file not found:\n{item_path}")

    def _set_new_selection(self, item_widget):
        """Set the new selection and emit signals"""
        if DEBUG: print(f"DEBUG: {self.catalog_name}: _set_new_selection called with widget: {item_widget}")
        
        item_widget.set_selected(True)
        item_widget.update()
        self.selected_item_widget = item_widget
        
        # Scroll to item only if it's offscreen
        self._scroll_to_selected_item_if_needed()
        
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Widget selected, getting item path")
        
        # Get item path and emit selection signal
        item_data = item_widget.item_data
        item_path = self.get_item_path(item_data)
        
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Item path: {item_path}")
        
        if item_path and os.path.exists(item_path):
            if DEBUG: print(f"DEBUG: {self.catalog_name}: Emitting item_selected signal")
            self.currently_loading_item = item_path
            self.item_selected.emit(item_path, item_data)
        else:
            if DEBUG: print(f"DEBUG: {self.catalog_name}: File not found")
            QMessageBox.warning(self, "File Not Found", f"Item file not found:\n{item_path}")

    def _scroll_to_selected_item_if_needed(self):
        """Scroll to the selected item only if it's not fully visible"""
        if not self.selected_item_widget:
            return
            
        # Find the index of the selected item widget
        for i in range(self.item_list.count()):
            item = self.item_list.item(i)
            if item and self.item_list.itemWidget(item) == self.selected_item_widget:
                self.scroll_to_item(i)
                return

    def scroll_to_item(self, index):
        """Scrolls the item list so the item at 'index' is visible at the top if offscreen."""
        item = self.item_list.item(index)
        if not item:
            if DEBUG:
                print(f"DEBUG: {self.catalog_name}: scroll_to_item: No item at index {index}")
            return

        item_rect = self.item_list.visualItemRect(item)
        viewport_rect = self.item_list.viewport().rect()

        if DEBUG:
            print(f"DEBUG: {self.catalog_name}: scroll_to_item called for index {index}")
            print(f"DEBUG: {self.catalog_name}: item_rect={item_rect}, viewport_rect={viewport_rect}")

        # Check only vertical visibility (ignore horizontal since items can be wider than viewport)
        item_top = item_rect.top()
        item_bottom = item_rect.bottom()
        viewport_top = viewport_rect.top()
        viewport_bottom = viewport_rect.bottom()
        
        # Item is fully visible vertically if both top and bottom are within viewport
        is_fully_visible_vertically = (item_top >= viewport_top and item_bottom <= viewport_bottom)

        if DEBUG:
            print(f"DEBUG: {self.catalog_name}: item vertical span: {item_top}-{item_bottom}, viewport vertical span: {viewport_top}-{viewport_bottom}")
            print(f"DEBUG: {self.catalog_name}: is_fully_visible_vertically: {is_fully_visible_vertically}")

        # If the item is not fully visible vertically, scroll to it
        if not is_fully_visible_vertically:
            if DEBUG:
                print(f"DEBUG: {self.catalog_name}: scrolling to item {index} (not fully visible vertically)")
            self.item_list.scrollToItem(item, self.item_list.PositionAtTop)
        else:
            if DEBUG:
                print(f"DEBUG: {self.catalog_name}: item {index} already fully visible vertically, no scroll needed.")

    def on_item_loaded_with_metadata(self, item_path, metadata):
        """Handle when an item is loaded"""
        self.currently_loading_item = None

    # ---- Save/Load Preferences ----

    def on_request_save(self):
        """Save preferences - subclasses can override to add more data"""
        self._pending_save_data = {}
    
    def on_request_load(self, data):
        """Load preferences - subclasses can override to handle more data"""
        pass

    def select_next_item(self):
        """Select the next item in the catalog"""
        if DEBUG: print(f"DEBUG: {self.catalog_name}: select_next_item() called")
        count = self.item_list.count()
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Total item count: {count}")
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Current selected_item_widget: {self.selected_item_widget}")
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Project folder: {self.project_folder}")
        
        # If nothing is selected but there are items and a project folder, select the first item
        if count > 0 and self.project_folder and not self.selected_item_widget:
            if DEBUG: print(f"DEBUG: {self.catalog_name}: No selection, selecting first item")
            self.on_selection_will_change()  # Allow subclasses to handle selection change
            first_item = self.item_list.item(0)
            self._direct_select_item(first_item)
            self._scroll_to_selected_item_if_needed()
            return

        if count == 0 or not self.selected_item_widget:
            if DEBUG: print(f"DEBUG: {self.catalog_name}: No items or no selection, returning")
            self.on_selection_will_change()  # Allow subclasses to handle selection change
            return
            
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Looking for current selection in list")
        for i in range(count):
            widget = self.item_list.itemWidget(self.item_list.item(i))
            if DEBUG: print(f"DEBUG: {self.catalog_name}: Checking index {i}, widget: {widget}")
            if widget == self.selected_item_widget:
                if DEBUG: print(f"DEBUG: {self.catalog_name}: Found current selection at index {i}")
                next_index = i + 1
                if next_index < count:
                    if DEBUG: print(f"DEBUG: {self.catalog_name}: Moving to next index {next_index}")
                    self.on_selection_will_change()  # Allow subclasses to handle selection change
                    next_item = self.item_list.item(next_index)
                    if DEBUG: print(f"DEBUG: {self.catalog_name}: About to call _direct_select_item with item: {next_item}")
                    self._direct_select_item(next_item)
                    self._scroll_to_selected_item_if_needed()
                else:
                    if DEBUG: print(f"DEBUG: {self.catalog_name}: Already at last item")
                    self.on_selection_will_change()  # Allow subclasses to handle selection change
                break

    def select_previous_item(self):
        """Select the previous item in the catalog"""
        if DEBUG: print(f"DEBUG: {self.catalog_name}: select_previous_item() called")
        count = self.item_list.count()
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Total item count: {count}")
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Current selected_item_widget: {self.selected_item_widget}")
        
        if count == 0 or not self.selected_item_widget:
            if DEBUG: print(f"DEBUG: {self.catalog_name}: No items or no selection, returning")
            return
            
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Looking for current selection in list")
        for i in range(count):
            widget = self.item_list.itemWidget(self.item_list.item(i))
            if DEBUG: print(f"DEBUG: {self.catalog_name}: Checking index {i}, widget: {widget}")
            if widget == self.selected_item_widget:
                if DEBUG: print(f"DEBUG: {self.catalog_name}: Found current selection at index {i}")
                prev_index = i - 1
                if prev_index >= 0:
                    if DEBUG: print(f"DEBUG: {self.catalog_name}: Moving to previous index {prev_index}")
                    self.on_selection_will_change()  # Allow subclasses to handle selection change
                    prev_item = self.item_list.item(prev_index)
                    if DEBUG: print(f"DEBUG: {self.catalog_name}: About to call _direct_select_item with item: {prev_item}")
                    self._direct_select_item(prev_item)
                    self._scroll_to_selected_item_if_needed()
                else:
                    if DEBUG: print(f"DEBUG: {self.catalog_name}: Already at first item")
                break

    def _direct_select_item(self, item):
        """Directly select an item without the delayed mechanism"""
        if DEBUG: print(f"DEBUG: {self.catalog_name}: _direct_select_item called with item: {item}")
        
        if item is None:
            if DEBUG: print(f"DEBUG: {self.catalog_name}: Item is None, returning")
            return
            
        item_widget = self.item_list.itemWidget(item)
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Item widget: {item_widget}")
        
        if not item_widget or not hasattr(item_widget, 'item_data'):
            if DEBUG: print(f"DEBUG: {self.catalog_name}: No widget or item_data, returning")
            return

        # Clear previous selection
        if self.selected_item_widget and self.selected_item_widget != item_widget:
            if DEBUG: print(f"DEBUG: {self.catalog_name}: Clearing previous selection: {self.selected_item_widget}")
            self.selected_item_widget.set_selected(False)
            self.selected_item_widget.update()

        # Set new selection immediately
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Setting new selection: {item_widget}")
        item_widget.set_selected(True)
        item_widget.update()
        self.selected_item_widget = item_widget
        
        # Don't scroll here - let the caller decide if scrolling is needed
        
        # Get item path and emit selection signal
        item_data = item_widget.item_data
        item_path = self.get_item_path(item_data)
        
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Item path: {item_path}")
        
        if item_path and os.path.exists(item_path):
            if DEBUG: print(f"DEBUG: {self.catalog_name}: Emitting item_selected signal")
            self.currently_loading_item = item_path
            self.item_selected.emit(item_path, item_data)
            
            # Allow subclasses to handle post-selection logic
            self.on_item_selection_changed(item_widget, item_data)
        else:
            if DEBUG: print(f"DEBUG: {self.catalog_name}: File not found")
            QMessageBox.warning(self, "File Not Found", f"Item file not found:\n{item_path}")

    def on_selection_will_change(self):
        """Called before selection changes - can be overridden by subclasses"""
        pass

    def on_item_selection_changed(self, item_widget, item_data):
        """Called after an item is selected - can be overridden by subclasses"""
        pass