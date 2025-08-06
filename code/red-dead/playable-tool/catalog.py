DEBUG = False  # Set to True to enable debug output

from PyQt5.QtCore import Qt, pyqtSignal, QSize, QThread, QTimer, QObject
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QPushButton, QListWidget, QListWidgetItem, QSizePolicy, 
    QMessageBox, QAbstractItemView, QLabel
)

# OS Stuff
import os
import csv
from metadata import MetadataWorker, read_metadata_csv
from catalog_item import AbstractCatalogItemWidget, MovieItemWidget, ITEM_HEIGHT

class CatalogLoadingWorker(QObject):
    """Worker class for loading catalog data in a separate thread"""
    
    progress = pyqtSignal(int)  # Progress percentage (0-100)
    finished = pyqtSignal(list)  # List of item data when finished
    error = pyqtSignal(str)  # Error message
    
    def __init__(self, metadata_path, project_folder, assets_folder_name):
        super().__init__()
        self.metadata_path = metadata_path
        self.project_folder = project_folder
        self.assets_folder_name = assets_folder_name
        
    def run(self):
        """Load catalog data in background thread"""
        try:
            if DEBUG: print(f"DEBUG: CatalogLoadingWorker: Loading from {self.metadata_path}")
            
            # First, count total rows for progress calculation
            self.progress.emit(5)  # 5% - Starting to count rows
            
            total_rows = 0
            with open(self.metadata_path, 'r', encoding='utf-8') as csvfile:
                reader = csv.DictReader(csvfile)
                for _ in reader:
                    total_rows += 1
            
            if DEBUG: print(f"DEBUG: CatalogLoadingWorker: Found {total_rows} items to load")
            self.progress.emit(10)  # 10% - Finished counting
            
            # Now load the actual data
            items_data = []
            current_row = 0
            
            for row in read_metadata_csv(self.metadata_path):
                items_data.append(row)
                current_row += 1
                
                # Calculate progress (10% to 90% for loading data)
                if total_rows > 0:
                    progress = 10 + int((current_row / total_rows) * 80)
                    self.progress.emit(progress)
            
            self.progress.emit(95)  # 95% - Data loaded, finishing up
            
            if DEBUG: print(f"DEBUG: CatalogLoadingWorker: Loaded {len(items_data)} items")
            self.finished.emit(items_data)
            
        except Exception as e:
            if DEBUG: print(f"DEBUG: CatalogLoadingWorker: Error loading data: {str(e)}")
            self.error.emit(str(e))

class AbstractCatalogWindow(QMainWindow):
    """Abstract base class for catalog windows"""
    
    # Define signals for communication
    request_save = pyqtSignal()
    request_load = pyqtSignal(dict)
    catalog_loading_started = pyqtSignal()
    catalog_loading_finished = pyqtSignal()
    catalog_loading_progress = pyqtSignal(int)  # Add progress signal
    catalog_contents_cleared = pyqtSignal()
    item_selected = pyqtSignal(str, dict)  # Signal to send item path AND metadata
    
    def __init__(self, ui):
        super().__init__()
        self.ui = ui
        self.project_folder = None  # Current project folder
        self.currently_loading_item = None  # Track what item is currently being requested
        self.selected_item_widget = None  # Track currently selected item widget
        self._pending_save_data = {}  # Initialize this attribute
        
        # Loading thread variables
        self.loading_thread = None
        self.loading_worker = None
        self.loading_progress_item = None
        
        if DEBUG:
            print(f"DEBUG: AbstractCatalogWindow: Initializing {self.__class__.__name__}")
        
        # Only set defaults if not already set by subclass
        if not hasattr(self, 'catalog_name'):
            self.catalog_name = "Unknown Catalog"
        if not hasattr(self, 'data_folder'):
            self.data_folder = "data"
        if not hasattr(self, 'metadata_file'):
            self.metadata_file = "metadata.csv"
            
        if DEBUG:
            print(f"DEBUG: AbstractCatalogWindow: catalog_name='{self.catalog_name}', data_folder='{self.data_folder}', metadata_file='{self.metadata_file}'")
        
        self.setup_ui()
        self.setup_connections()

    def closeEvent(self, event):
        """Clean up threads when window is closing"""
        if DEBUG: print(f"DEBUG: {self.catalog_name}: closeEvent called")
        
        # Clean up loading thread
        if hasattr(self, 'loading_thread') and self.loading_thread:
            if self.loading_thread.isRunning():
                if DEBUG: print(f"DEBUG: {self.catalog_name}: Stopping loading thread")
                self.loading_thread.quit()
                if not self.loading_thread.wait(3000):  # Wait up to 3 seconds
                    if DEBUG: print(f"DEBUG: {self.catalog_name}: Force terminating loading thread")
                    self.loading_thread.terminate()
                    self.loading_thread.wait()
        
        # Clean up metadata thread
        if hasattr(self, 'metadata_thread') and self.metadata_thread:
            if self.metadata_thread.isRunning():
                if DEBUG: print(f"DEBUG: {self.catalog_name}: Stopping metadata thread")
                self.metadata_thread.quit()
                if not self.metadata_thread.wait(3000):  # Wait up to 3 seconds
                    if DEBUG: print(f"DEBUG: {self.catalog_name}: Force terminating metadata thread")
                    self.metadata_thread.terminate()
                    self.metadata_thread.wait()
        
        # Clean up workers
        if hasattr(self, 'loading_worker') and self.loading_worker:
            self.loading_worker = None
        if hasattr(self, 'metadata_worker') and self.metadata_worker:
            self.metadata_worker = None
            
        super().closeEvent(event)

    def clear_project(self):
        """Clear current project and cancel any ongoing operations"""
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Clearing project")
        
        # Stop any existing loading thread
        if hasattr(self, 'loading_thread') and self.loading_thread and self.loading_thread.isRunning():
            if DEBUG: print(f"DEBUG: {self.catalog_name}: Stopping loading thread due to project clear")
            self.loading_thread.quit()
            self.loading_thread.wait(1000)  # Wait up to 1 second
            if self.loading_thread.isRunning():
                if DEBUG: print(f"DEBUG: {self.catalog_name}: Force terminating loading thread")
                self.loading_thread.terminate()
                self.loading_thread.wait()
        
        # Stop any existing metadata thread
        if hasattr(self, 'metadata_thread') and self.metadata_thread and self.metadata_thread.isRunning():
            if DEBUG: print(f"DEBUG: {self.catalog_name}: Stopping metadata thread due to project clear")
            self.metadata_thread.quit()
            self.metadata_thread.wait(1000)  # Wait up to 1 second
            if self.metadata_thread.isRunning():
                if DEBUG: print(f"DEBUG: {self.catalog_name}: Force terminating metadata thread")
                self.metadata_thread.terminate()
                self.metadata_thread.wait()
        
        # Hide progress label and show metadata button
        if hasattr(self, 'progress_label') and self.progress_label:
            self.progress_label.setVisible(False)
        
        if hasattr(self, 'metadata_button'):
            self.metadata_button.setVisible(True)
            self.metadata_button.setText("Rebuild Metadata")
            self.metadata_button.setEnabled(False)  # Disabled when no project
        
        # Clear UI state
        self.item_list.clear()
        self.selected_item_widget = None
        self.currently_loading_item = None
        self.loading_progress_item = None
        
        # Reset project folder to None (but don't trigger reload)
        self.project_folder = None
        
        # Emit cleared signal
        self.catalog_contents_cleared.emit()
        
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Project cleared")

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
        
        # Remove the animation timer setup since we're using percentage progress instead
        # self.rebuild_animation_timer = QTimer()
        # self.rebuild_animation_timer.timeout.connect(self.animate_rebuild_button)
        # self.rebuild_dot_count = 0

    def create_button_layout(self):
        """Create the button layout - can be overridden by subclasses"""
        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(0)
        button_width, button_height = self.ui.get_dimensions('button')

        # Store button height for progress label
        self.button_height = button_height

        # Metadata rebuild button (always visible)
        self.metadata_button = QPushButton("Rebuild Metadata")
        self.metadata_button.setFont(self.ui.get_font('button'))
        self.metadata_button.clicked.connect(self.rebuild_metadata)
        self.metadata_button.setEnabled(False)
        self.metadata_button.setFixedSize(160, button_height)

        # Progress label spans full width (initially hidden)
        self.progress_label = QLabel("Loading catalog... 0%")
        self.progress_label.setFont(self.ui.get_font('button'))
        self.progress_label.setFixedHeight(button_height)
        self.progress_label.setAlignment(Qt.AlignCenter)
        self.progress_label.setStyleSheet("QLabel { padding: 0px 10px 0px 10px; background-color: #f0f; color: #fff; }")
        self.progress_label.setVisible(False)

        # Add widgets to main layout
        button_layout.addWidget(self.metadata_button)
        button_layout.addWidget(self.progress_label)
        button_layout.addStretch()
        
        return button_layout

    def on_catalog_loading_started(self):
        """Handle when catalog starts loading - can be overridden by subclasses"""
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Catalog loading started")
        # Hide metadata button during loading
        self.metadata_button.setVisible(False)
        
        # Show progress label which will now span full width
        if self.progress_label:
            self.progress_label.setText("Loading catalog... 0%")
            self.progress_label.setVisible(True)
            if DEBUG: 
                print(f"DEBUG: {self.catalog_name}: Progress label text set to: '{self.progress_label.text()}'")
                print(f"DEBUG: {self.catalog_name}: Progress label visible: {self.progress_label.isVisible()}")
        else:
            if DEBUG: print(f"DEBUG: {self.catalog_name}: progress_label is None in on_catalog_loading_started!")

    def on_catalog_loading_finished(self):
        """Handle when catalog finishes loading - can be overridden by subclasses"""
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Catalog loading finished")
        
        # Hide progress label
        if self.progress_label:
            self.progress_label.setVisible(False)
        
        # Show metadata button again
        self.metadata_button.setVisible(True)
        
        # Re-enable metadata button after loading completes
        if self.project_folder:  # Only enable if we have a project
            self.metadata_button.setEnabled(True)

    def update_loading_progress(self, progress):
        """Update the loading progress display"""
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Updating progress to {progress}%")
        if self.progress_label:
            if progress < 95:
                # Phase 1: Loading data
                new_text = f"Loading data... {progress}%"
            else:
                # Phase 2: Will be handled by create_next_batch
                new_text = f"Loading data... {progress}%"
            
            old_text = self.progress_label.text()
            self.progress_label.setText(new_text)
            
            if DEBUG:
                print(f"DEBUG: {self.catalog_name}: Progress label exists: {self.progress_label is not None}")
                print(f"DEBUG: {self.catalog_name}: Progress label visible: {self.progress_label.isVisible()}")
                print(f"DEBUG: {self.catalog_name}: Old text: '{old_text}' -> New text: '{new_text}'")
                print(f"DEBUG: {self.catalog_name}: Actual label text after update: '{self.progress_label.text()}'")
        else:
            if DEBUG: print(f"DEBUG: {self.catalog_name}: progress_label is None!")

    def rebuild_metadata(self):
        """Rebuild metadata for the catalog"""
        if not self.project_folder:
            return
        
        # Stop any existing metadata thread
        if hasattr(self, 'metadata_thread') and self.metadata_thread and self.metadata_thread.isRunning():
            if DEBUG: print(f"DEBUG: {self.catalog_name}: Stopping existing metadata thread")
            self.metadata_thread.quit()
            self.metadata_thread.wait()
        
        # Clear the catalog items first
        self.item_list.clear()
        self.selected_item_widget = None
        self.catalog_contents_cleared.emit()
        
        # Don't add a progress item - just show progress in the button
        self.loading_progress_item = None
        
        # Disable button and start with 0% progress
        self.metadata_button.setEnabled(False)
        self.metadata_button.setText("Rebuilding 0%")
        
        # Create worker thread
        from metadata import MetadataWorker
        self.metadata_thread = QThread()
        self.metadata_worker = MetadataWorker(self.project_folder, self.data_folder, self.metadata_file)
        self.metadata_worker.moveToThread(self.metadata_thread)
        
        # Connect signals properly
        self.metadata_thread.started.connect(self.metadata_worker.run)
        self.metadata_worker.progress.connect(self.on_metadata_progress)
        self.metadata_worker.finished.connect(lambda success: self.on_metadata_finished(success, ""))
        self.metadata_worker.error.connect(lambda error_msg: self.on_metadata_finished(False, error_msg))
        self.metadata_worker.finished.connect(self.metadata_thread.quit)
        self.metadata_worker.finished.connect(self.metadata_worker.deleteLater)
        self.metadata_thread.finished.connect(self.metadata_thread.deleteLater)
        
        # Clean up references when thread finishes
        self.metadata_thread.finished.connect(lambda: setattr(self, 'metadata_thread', None))
        self.metadata_worker.finished.connect(lambda success: setattr(self, 'metadata_worker', None))
        
        # Start the thread
        self.metadata_thread.start()

    def on_metadata_progress(self, progress):
        """Handle metadata rebuild progress updates"""
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Metadata progress: {progress}% (type: {type(progress)})")
        
        # Convert progress to int if it's a string
        try:
            if isinstance(progress, str):
                progress = int(progress)
            elif not isinstance(progress, int):
                progress = int(progress)
        except (ValueError, TypeError):
            if DEBUG: print(f"DEBUG: {self.catalog_name}: Could not convert progress to int: {progress}")
            progress = 0
        
        # Update button text with percentage (button remains disabled)
        if hasattr(self, 'metadata_button'):
            self.metadata_button.setText(f"Rebuilding {progress}%")

    def on_metadata_finished(self, success, message=""):
        """Handle metadata rebuild completion"""
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Metadata rebuild finished - success: {success}, message: '{message}'")
        
        # Re-enable button and reset text
        self.metadata_button.setEnabled(True)
        self.metadata_button.setText("Rebuild Metadata")
        
        if success:
            # Reload the catalog data after successful metadata rebuild
            self.load_catalog_data()
        else:
            # Show error message
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.critical(self, "Metadata Error", f"Failed to rebuild metadata:\n{message}")
            # Still emit cleared signal since we cleared the list
            self.catalog_contents_cleared.emit()

    def load_catalog_data(self):
        """Load catalog data from metadata file"""
        if not self.project_folder:
            if DEBUG: print(f"DEBUG: {self.catalog_name}: No project folder set, cannot load catalog")
            return
            
        # Build path to metadata file (in metadata subfolder)
        metadata_path = os.path.join(self.project_folder, "metadata", self.metadata_file)
        
        if DEBUG: 
            print(f"DEBUG: {self.catalog_name}: Looking for metadata at: {metadata_path}")
            # List all CSV files in the metadata folder to see what's actually there
            try:
                metadata_folder = os.path.join(self.project_folder, "metadata")
                if os.path.exists(metadata_folder):
                    all_files = os.listdir(metadata_folder)
                    csv_files = [f for f in all_files if f.endswith('.csv')]
                    print(f"DEBUG: {self.catalog_name}: CSV files found in metadata folder: {csv_files}")
                else:
                    print(f"DEBUG: {self.catalog_name}: Metadata folder does not exist: {metadata_folder}")
            except Exception as e:
                print(f"DEBUG: {self.catalog_name}: Error listing metadata folder: {e}")
        
        if not os.path.exists(metadata_path):
            if DEBUG: print(f"DEBUG: {self.catalog_name}: Metadata file not found: {metadata_path}")
            # Clear any existing items and show empty state
            self.item_list.clear()
            self.selected_item_widget = None
            self.catalog_contents_cleared.emit()
            return
        
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Metadata file found, starting threaded loading")
        
        # Emit loading started signal
        self.catalog_loading_started.emit()
            
        # Load items using threaded loading
        self.load_items_from_metadata_threaded(metadata_path, self.project_folder)

    def load_items_from_metadata_threaded(self, metadata_path, project_folder):
        """Load items from metadata file using background thread"""
        # Stop any existing loading thread
        if hasattr(self, 'loading_thread') and self.loading_thread and self.loading_thread.isRunning():
            if DEBUG: print(f"DEBUG: {self.catalog_name}: Stopping existing loading thread")
            self.loading_thread.quit()
            self.loading_thread.wait()

        # Clear the list and DON'T show loading progress in the list
        self.item_list.clear()
        self.catalog_contents_cleared.emit()
        self.selected_item_widget = None

        # Create worker thread
        self.loading_thread = QThread()
        self.loading_worker = CatalogLoadingWorker(metadata_path, project_folder, self.get_assets_folder_name())
        self.loading_worker.moveToThread(self.loading_thread)
        
        # Connect signals - don't connect to show_loading_progress since we handle it differently
        self.loading_thread.started.connect(self.loading_worker.run)
        self.loading_worker.progress.connect(self.catalog_loading_progress.emit)  # Only emit progress signal
        self.loading_worker.finished.connect(self.on_loading_finished)
        self.loading_worker.error.connect(self.on_loading_error)
        self.loading_worker.finished.connect(self.loading_thread.quit)
        self.loading_worker.finished.connect(self.loading_worker.deleteLater)
        self.loading_thread.finished.connect(self.loading_thread.deleteLater)
        
        # Clean up references when thread finishes
        self.loading_thread.finished.connect(lambda: setattr(self, 'loading_thread', None))
        self.loading_worker.finished.connect(lambda items_data: setattr(self, 'loading_worker', None))
        
        # Start the thread
        self.loading_thread.start()

    def on_loading_finished(self, items_data):
        """Handle when loading is finished"""
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Loading finished with {len(items_data)} items")
        
        # Update our progress label to show thumbnail loading phase
        if self.progress_label:
            self.progress_label.setText("Loading thumbnails... 0%")
        
        # Store the data and start creating widgets in batches
        self.items_data = items_data
        self.assets_folder = os.path.join(self.project_folder, self.get_assets_folder_name())
        self.current_batch_index = 0
        self.batch_size = 10  # Create 10 widgets at a time
        self.total_items = len(items_data)
        
        # Start creating widgets in batches
        self.create_next_batch()

    def on_loading_error(self, error_message):
        """Handle loading errors"""
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Loading error: {error_message}")
        QMessageBox.critical(self, "Loading Error", f"Failed to load catalog:\n{error_message}")
        self.catalog_loading_finished.emit()

    def create_next_batch(self):
        """Create the next batch of item widgets"""
        if not hasattr(self, 'items_data') or self.current_batch_index >= len(self.items_data):
            # All batches processed - finish
            self.catalog_loading_progress.emit(100)
            self.update_item_list()
            return
            
        start_idx = self.current_batch_index
        end_idx = min(start_idx + self.batch_size, len(self.items_data))
        
        # Calculate thumbnail loading progress (0% to 100%) and update progress label
        if self.total_items > 0:
            thumbnail_progress = int((start_idx / self.total_items) * 100)
            if self.progress_label:
                self.progress_label.setText(f"Loading thumbnails... {thumbnail_progress}%")
        
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Creating widgets {start_idx} to {end_idx-1}, thumbnail progress: {thumbnail_progress}%")
        
        # Create widgets for this batch
        for i in range(start_idx, end_idx):
            item_data = self.items_data[i]
            if DEBUG: print(f"DEBUG: {self.catalog_name}: Creating widget for: {item_data}")
            
            # Create custom widget for this item
            item_widget = self.create_item_widget(item_data, self.assets_folder)
            
            # Connect the widget's clicked signal to handle selection
            if hasattr(item_widget, 'clicked'):
                item_widget.clicked.connect(lambda data, widget=item_widget: self.on_widget_clicked(widget, data))
            
            # Create list item with fixed height
            item = QListWidgetItem()
            item.setSizeHint(QSize(item_widget.width(), ITEM_HEIGHT))

            # Add to list
            self.item_list.addItem(item)
            self.item_list.setItemWidget(item, item_widget)
        
        # Update batch index
        self.current_batch_index = end_idx
        
        # Process events to ensure UI responsiveness
        from PyQt5.QtWidgets import QApplication
        QApplication.processEvents()
        
        # Schedule next batch with longer delay to allow UI interaction
        QTimer.singleShot(100, self.create_next_batch)

    def update_item_list(self):
        """Called when all items have been loaded"""
        if DEBUG: print(f"DEBUG: {self.catalog_name}: All items loaded, finalizing")
        
        # Emit the loading finished signal
        self.catalog_loading_finished.emit()
        
        # Clean up temporary data
        if hasattr(self, 'items_data'):
            delattr(self, 'items_data')
        if hasattr(self, 'assets_folder'):
            delattr(self, 'assets_folder')

    def set_project_folder(self, project_folder):
        """Set the project folder and load catalog data"""
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Setting project folder to: {project_folder}")
        
        self.project_folder = project_folder
        
        # Enable metadata button if we have a project
        if hasattr(self, 'metadata_button'):
            self.metadata_button.setEnabled(project_folder is not None)
        
        # Load catalog data if project folder is set
        if project_folder:
            self.load_catalog_data()

    def on_request_save(self):
        """Handle save requests - can be overridden by subclasses"""
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Save requested")
        # Base implementation provides empty save data
        pass

    def on_request_load(self, data):
        """Handle load requests - can be overridden by subclasses"""
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Load requested with data: {data}")
        # Base implementation does nothing - subclasses can override
        if data and "project_folder" in data:
            # Don't automatically set project folder from preferences
            # Let the switchboard handle project coordination
            pass

    def on_widget_clicked(self, widget, data):
        """Handle when an item widget is clicked"""
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Widget clicked with data: {data}")
        
        # Call selection will change handler if it exists
        if hasattr(self, 'on_selection_will_change'):
            self.on_selection_will_change()
        
        # Update selected widget
        if self.selected_item_widget:
            self.selected_item_widget.set_selected(False)
        
        self.selected_item_widget = widget
        widget.set_selected(True)
        
        # Get the item path
        item_path = self.get_item_path(data)
        
        # Emit selection signal with both path and data
        if item_path:
            self.item_selected.emit(item_path, data)
        
        # Call selection changed handler if it exists
        if hasattr(self, 'on_item_selection_changed'):
            self.on_item_selection_changed(widget, data)

    def select_next_item(self):
        """Select the next item in the list"""
        if not self.selected_item_widget or self.item_list.count() == 0:
            return
        
        # Find current selected item index
        current_index = -1
        for i in range(self.item_list.count()):
            item = self.item_list.item(i)
            widget = self.item_list.itemWidget(item)
            if widget == self.selected_item_widget:
                current_index = i
                break
        
        # Move to next item (wrap around to beginning if at end)
        if current_index >= 0:
            next_index = (current_index + 1) % self.item_list.count()
            next_item = self.item_list.item(next_index)
            next_widget = self.item_list.itemWidget(next_item)
            
            # Simulate click on next widget
            if hasattr(next_widget, 'data'):
                self.on_widget_clicked(next_widget, next_widget.data)

    def select_previous_item(self):
        """Select the previous item in the list"""
        if not self.selected_item_widget or self.item_list.count() == 0:
            return
        
        # Find current selected item index
        current_index = -1
        for i in range(self.item_list.count()):
            item = self.item_list.item(i)
            widget = self.item_list.itemWidget(item)
            if widget == self.selected_item_widget:
                current_index = i
                break
        
        # Move to previous item (wrap around to end if at beginning)
        if current_index >= 0:
            prev_index = (current_index - 1) % self.item_list.count()
            prev_item = self.item_list.item(prev_index)
            prev_widget = self.item_list.itemWidget(prev_item)
            
            # Simulate click on previous widget
            if hasattr(prev_widget, 'data'):
                self.on_widget_clicked(prev_widget, prev_widget.data)

    # Remove the duplicate clear_project method
    def clear_project(self):
        """Clear current project and cancel any ongoing operations"""
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Clearing project")
        
        # Stop any existing loading thread
        if hasattr(self, 'loading_thread') and self.loading_thread and self.loading_thread.isRunning():
            if DEBUG: print(f"DEBUG: {self.catalog_name}: Stopping loading thread due to project clear")
            self.loading_thread.quit()
            self.loading_thread.wait(1000)  # Wait up to 1 second
            if self.loading_thread.isRunning():
                if DEBUG: print(f"DEBUG: {self.catalog_name}: Force terminating loading thread")
                self.loading_thread.terminate()
                self.loading_thread.wait()
        
        # Stop any existing metadata thread
        if hasattr(self, 'metadata_thread') and self.metadata_thread and self.metadata_thread.isRunning():
            if DEBUG: print(f"DEBUG: {self.catalog_name}: Stopping metadata thread due to project clear")
            self.metadata_thread.quit()
            self.metadata_thread.wait(1000)  # Wait up to 1 second
            if self.metadata_thread.isRunning():
                if DEBUG: print(f"DEBUG: {self.catalog_name}: Force terminating metadata thread")
                self.metadata_thread.terminate()
                self.metadata_thread.wait()
        
        # Hide progress label and show metadata button
        if hasattr(self, 'progress_label') and self.progress_label:
            self.progress_label.setVisible(False)
        
        if hasattr(self, 'metadata_button'):
            self.metadata_button.setVisible(True)
            self.metadata_button.setText("Rebuild Metadata")
            self.metadata_button.setEnabled(False)  # Disabled when no project
        
        # Clear UI state
        self.item_list.clear()
        self.selected_item_widget = None
        self.currently_loading_item = None
        self.loading_progress_item = None
        
        # Reset project folder to None (but don't trigger reload)
        self.project_folder = None
        
        # Emit cleared signal
        self.catalog_contents_cleared.emit()
        
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Project cleared")

    # Abstract methods that subclasses must implement
    def get_assets_folder_name(self):
        """Get the name of the assets folder - must be implemented by subclasses"""
        raise NotImplementedError("Subclasses must implement get_assets_folder_name()")
    
    def create_item_widget(self, item_data, assets_folder):
        """Create a widget for an item - must be implemented by subclasses"""
        raise NotImplementedError("Subclasses must implement create_item_widget()")
    
    def get_item_path(self, item_data):
        """Get the full path for an item - must be implemented by subclasses"""
        raise NotImplementedError("Subclasses must implement get_item_path()")