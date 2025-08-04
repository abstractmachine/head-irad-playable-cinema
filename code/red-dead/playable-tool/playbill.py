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
from gameplay_item import GameplayItemWidget

class PlaybillWindow(AbstractCatalogWindow):
    
    # Additional signals specific to playbill
    gameplay_selected = pyqtSignal(str, dict)  # Signal for gameplay selection
    
    def __init__(self, ui):
        # Set catalog-specific properties before calling super().__init__()
        self.catalog_name = "Playbill"
        self.data_folder = "gameplay"
        self.metadata_file = "gameplay.csv"
        
        # Call parent constructor
        super().__init__(ui)
        
        # Add gameplay_selected signal as alias for item_selected
        self.gameplay_selected = self.item_selected
        
    def create_button_layout(self):
        """Create the button layout with playbill-specific buttons"""
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
        
        # Export gameplay button (placeholder for future functionality)
        self.export_button = QPushButton("Export Gameplay")
        self.export_button.setFont(self.ui.get_font('button'))
        self.export_button.setFixedSize(140, button_height)
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self.handle_export_gameplay)

        button_layout.addWidget(self.metadata_button)
        button_layout.addWidget(self.export_button)
        button_layout.addStretch()
        
        return button_layout
    
    def get_assets_folder_name(self):
        """Get the name of the assets folder"""
        return "thumbnails"  # Use thumbnails for gameplay videos instead of posters
    
    def create_item_widget(self, item_data, assets_folder):
        """Create a gameplay item widget"""
        return GameplayItemWidget(item_data, assets_folder, self.ui)
    
    def get_item_path(self, item_data):
        """Get the full path for a gameplay video"""
        filename = item_data.get('filename', '')
        if filename and self.project_folder:
            return os.path.join(self.project_folder, self.data_folder, filename)
        return None

    def set_project_folder(self, project_folder):
        """Override to enable export button when project is loaded"""
        super().set_project_folder(project_folder)
        
        # if project_folder:
        #     self.export_button.setEnabled(True)
        # else:
        #     self.export_button.setEnabled(False)

    # Override aliases for backward compatibility
    @property
    def gameplay_list(self):
        return self.item_list
    
    @property
    def currently_loading_gameplay(self):
        return self.currently_loading_item
    
    @currently_loading_gameplay.setter
    def currently_loading_gameplay(self, value):
        self.currently_loading_item = value
    
    @property
    def selected_gameplay_widget(self):
        return self.selected_item_widget
    
    @selected_gameplay_widget.setter
    def selected_gameplay_widget(self, value):
        self.selected_item_widget = value
    
    def on_gameplay_clicked(self, item):
        """Handle gameplay item click - alias for on_item_clicked"""
        self.on_item_clicked(item)
    
    def update_gameplay_list(self):
        """Update gameplay list - alias for update_item_list"""
        self.update_item_list()
    
    def load_gameplay_from_metadata(self, metadata_path, project_folder):
        """Load gameplay from metadata - alias for load_items_from_metadata"""
        self.load_items_from_metadata(metadata_path, project_folder)
    
    def on_gameplay_loaded_with_metadata(self, gameplay_path, metadata):
        """Handle when gameplay is loaded - alias for on_item_loaded_with_metadata"""
        self.on_item_loaded_with_metadata(gameplay_path, metadata)

    def handle_export_gameplay(self):
        """Handle export gameplay button click"""
        if DEBUG: print("DEBUG: Playbill: Export Gameplay button pressed")
        
        if not self.selected_item_widget:
            QMessageBox.information(self, "Export Gameplay", "Please select a gameplay video first.")
            return
            
        # Placeholder for future export functionality
        QMessageBox.information(self, "Export Gameplay", "Export functionality coming soon!")