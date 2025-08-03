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
from metadata import MetadataWorker
from catalog_item import AbstractCatalogItemWidget, MovieItemWidget, ITEM_HEIGHT

class AbstractCatalogWindow(QMainWindow):
    """Abstract base class for catalog windows"""
    
    # Define signals for communication
    request_save = pyqtSignal()
    request_load = pyqtSignal(dict)
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
        self.item_list.itemClicked.connect(self.on_item_clicked)

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
    
    def setup_connections(self):
        """Setup signal connections"""
        self.request_save.connect(self.on_request_save)
        self.request_load.connect(self.on_request_load)
    
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
            # Enable buttons when project is loaded
            self.metadata_button.setEnabled(True)
            # Load catalog data
            self.load_catalog_data()
        else:
            # Clear current data
            self.item_list.clear()
            self.selected_item_widget = None
            self.metadata_button.setEnabled(False)
    
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
        self.selected_item_widget = None
        assets_folder = os.path.join(project_folder, self.get_assets_folder_name())
        
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Assets folder: {assets_folder}")
        
        try:
            with open(metadata_path, 'r', encoding='utf-8') as csvfile:
                if DEBUG: print(f"DEBUG: {self.catalog_name}: Successfully opened metadata file")
                reader = csv.DictReader(csvfile)
                item_count = 0
                for row in reader:
                    if DEBUG: print(f"DEBUG: {self.catalog_name}: Processing row {item_count}: {row}")
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
                    item_count += 1

                if DEBUG: print(f"DEBUG: {self.catalog_name}: Loaded {item_count} items")

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
        
        # Get item path and emit selection signal
        item_path = self.get_item_path(item_data)
        if item_path and os.path.exists(item_path):
            self.currently_loading_item = item_path
            self.item_selected.emit(item_path, item_data)
        else:
            QMessageBox.warning(self, "File Not Found", f"Item file not found:\n{item_path}")

    def update_item_list(self):
        """Update item list display"""
        for i in range(self.item_list.count()):
            item = self.item_list.item(i)
            if item:
                item_widget = self.item_list.itemWidget(item)
                if item_widget:
                    item_widget.update_background()
    
    def on_item_clicked(self, item):
        """Handle item click with delay to prevent double-clicks"""
        if DEBUG: print(f"DEBUG: {self.catalog_name}: on_item_clicked called with item: {item}")
        
        if item is None:
            if DEBUG: print(f"DEBUG: {self.catalog_name}: Item is None, returning")
            return
            
        item_widget = self.item_list.itemWidget(item)
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Item widget: {item_widget}")
        
        if not item_widget or not hasattr(item_widget, 'item_data'):
            if DEBUG: print(f"DEBUG: {self.catalog_name}: No widget or item_data, returning")
            return

        # Clear previous selection first
        if DEBUG: print(f"DEBUG: {self.catalog_name}: About to clear previous selection")
        self._clear_previous_selection()
        
        # Set selection after a short delay to ensure clearing is processed
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Setting timer for new selection")
        QTimer.singleShot(10, lambda: self._set_new_selection(item_widget))

    def _clear_previous_selection(self):
        """Clear the previous selection"""
        if DEBUG: print(f"DEBUG: {self.catalog_name}: _clear_previous_selection called")
        if self.selected_item_widget:
            if DEBUG: print(f"DEBUG: {self.catalog_name}: Clearing selection for widget: {self.selected_item_widget}")
            self.selected_item_widget.set_selected(False)
            self.selected_item_widget.update()
            self.selected_item_widget = None
        else:
            if DEBUG: print(f"DEBUG: {self.catalog_name}: No previous selection to clear")

    def _set_new_selection(self, item_widget):
        """Set the new selection and emit signals"""
        if DEBUG: print(f"DEBUG: {self.catalog_name}: _set_new_selection called with widget: {item_widget}")
        
        item_widget.set_selected(True)
        item_widget.update()
        self.selected_item_widget = item_widget
        
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

    def rebuild_metadata(self):
        """Start metadata rebuild process in worker thread"""
        if not self.project_folder:
            QMessageBox.warning(self, "Warning", "Please select a project folder first.")
            return
        
        # Disable buttons during rebuild
        self.metadata_button.setText("        Rebuilding")
        self.metadata_button.setEnabled(False)
        
        # Set button text alignment to left during rebuild
        self.metadata_button.setStyleSheet("QPushButton { text-align: left; }")
        
        # Start animated dots
        self.rebuild_dot_count = 0
        self.rebuild_animation_timer.start(500)
        
        # Clear item list and show progress
        self.item_list.clear()
        progress_item = QListWidgetItem("Starting metadata rebuild...")
        self.item_list.addItem(progress_item)
        
        # Create worker thread with proper parameters
        self.metadata_thread = QThread()
        self.metadata_worker = MetadataWorker(
            self.project_folder,
            data_folder=self.data_folder,
            metadata_filename=self.metadata_file
        )
        self.metadata_worker.moveToThread(self.metadata_thread)
        
        # Connect signals
        self.metadata_thread.started.connect(self.metadata_worker.run)
        self.metadata_worker.progress.connect(self.on_metadata_progress)
        self.metadata_worker.error.connect(self.on_metadata_error)
        self.metadata_worker.finished.connect(self.on_metadata_finished)
        
        # Start thread
        self.metadata_thread.start()

    def animate_rebuild_button(self):
        """Animate the rebuilding button with dots"""
        self.rebuild_dot_count = (self.rebuild_dot_count + 1) % 4
        dots = "." * self.rebuild_dot_count
        self.metadata_button.setText(f"        Rebuilding{dots}")

    def on_metadata_progress(self, message):
        """Handle progress updates from metadata worker"""
        if self.item_list.count() > 0:
            item = self.item_list.item(0)
            item.setText(message)

    def on_metadata_error(self, error_message):
        """Handle errors from metadata worker"""
        QMessageBox.critical(self, "Metadata Rebuild Error", error_message)
        self.cleanup_metadata_thread()

    def on_metadata_finished(self, success):
        """Handle completion of metadata rebuild"""
        self.cleanup_metadata_thread()
        
        if success:
            self.load_catalog_data()
        else:
            QMessageBox.critical(self, "Error", "Metadata rebuild failed.")

    def cleanup_metadata_thread(self):
        """Clean up the metadata worker thread"""
        self.rebuild_animation_timer.stop()
        
        if self.metadata_thread:
            self.metadata_thread.quit()
            self.metadata_thread.wait()
            self.metadata_thread = None
            self.metadata_worker = None
        
        # Re-enable buttons
        self.metadata_button.setText("Rebuild Metadata")
        self.metadata_button.setEnabled(True)
        self.metadata_button.setStyleSheet("QPushButton { text-align: center; }")

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

        # If the item is not fully visible, scroll to it
        if not viewport_rect.contains(item_rect):
            if DEBUG:
                print(f"DEBUG: {self.catalog_name}: scrolling to item {index} (not fully visible)")
            self.item_list.scrollToItem(item, self.item_list.PositionAtTop)
        else:
            if DEBUG:
                print(f"DEBUG: {self.catalog_name}: item {index} already fully visible, no scroll needed.")

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