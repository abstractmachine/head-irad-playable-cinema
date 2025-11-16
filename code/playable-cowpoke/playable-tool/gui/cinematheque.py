DEBUG = False  # Set to True to enable debug output

from PyQt5.QtCore import pyqtSignal, QTimer
from PyQt5.QtWidgets import (
    QHBoxLayout, QPushButton, QMessageBox, QLabel, QWidget, QStackedWidget
)
from PyQt5.QtCore import Qt
import os
from catalog import AbstractCatalogWindow
from gui.catalog_item import MovieItemWidget

class CinemathequeWindow(AbstractCatalogWindow):
    
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
        
        # Connect progress signal to update method
        self.catalog_loading_progress.connect(self.update_loading_progress)
        
        if DEBUG: 
            print(f"DEBUG: CinemathequeWindow: After super().__init__(), metadata_file = '{self.metadata_file}'")

    # ---- Project ----

    def get_assets_folder_name(self):
        """Get the name of the assets folder"""
        return "posters"
    
    # ---- Item ----

    def create_item_widget(self, item_data, assets_folder):
        """Create a movie item widget"""
        return MovieItemWidget(item_data, assets_folder, self.ui)
    
    def get_item_path(self, item_data):
        """Get the full path for a movie"""
        filename = item_data.get('filename', '')
        if filename and self.project_folder:
            return os.path.join(self.project_folder, "movies", filename)
        return None

    # ---- Bot Methods ----

    def load_items_from_metadata_threaded(self, metadata_path, project_folder):
        """Load items from metadata file using background thread - override to disable list progress"""
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
        from PyQt5.QtCore import QThread
        from catalog import CatalogLoadingWorker
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
        """Handle when loading is finished - override to not show thumbnails progress in list"""
        if DEBUG: print(f"DEBUG: {self.catalog_name}: Loading finished with {len(items_data)} items")
        
        # Update our button progress label to show thumbnail loading phase
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

    def create_next_batch(self):
        """Create the next batch of item widgets - override to update button progress"""
        if not hasattr(self, 'items_data') or self.current_batch_index >= len(self.items_data):
            # All batches processed - finish
            self.catalog_loading_progress.emit(100)
            self.update_item_list()
            return
            
        start_idx = self.current_batch_index
        end_idx = min(start_idx + self.batch_size, len(self.items_data))
        
        # Calculate thumbnail loading progress (0% to 100%) and update button progress
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
            from PyQt5.QtCore import QSize
            from PyQt5.QtWidgets import QListWidgetItem
            from gui.catalog_item import ITEM_HEIGHT
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
        from PyQt5.QtCore import QTimer
        QTimer.singleShot(100, self.create_next_batch)  # Increased from 50ms to 100ms

    def update_loading_progress(self, progress):
        """Update the loading progress display"""
        if DEBUG: print(f"DEBUG: Cinematheque: Updating progress to {progress}%")
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
                print(f"DEBUG: Cinematheque: Progress label exists: {self.progress_label is not None}")
                print(f"DEBUG: Cinematheque: Progress label visible: {self.progress_label.isVisible()}")
                print(f"DEBUG: Cinematheque: Old text: '{old_text}' -> New text: '{new_text}'")
                print(f"DEBUG: Cinematheque: Actual label text after update: '{self.progress_label.text()}'")
        else:
            if DEBUG: print(f"DEBUG: Cinematheque: progress_label is None!")
