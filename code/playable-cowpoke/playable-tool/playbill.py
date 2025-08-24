DEBUG = False  # Set to True to enable debug output

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QPushButton, QMessageBox
import os
from catalog import AbstractCatalogWindow
from gameplay_item import GameplayItemWidget

class PlaybillWindow(AbstractCatalogWindow):
    
    def __init__(self, ui):
        # Set catalog-specific properties before calling super().__init__()
        self.catalog_name = "Playbill"
        self.data_folder = "gameplay"
        self.metadata_file = "gameplay.csv"
        
        # Call parent constructor
        super().__init__(ui)
        
    def create_button_layout(self):
        """Create the button layout with playbill-specific buttons"""
        # Get the base button layout (metadata button + progress label)
        button_layout = super().create_button_layout()
        
        # Get button dimensions
        button_width, button_height = self.ui.get_dimensions('button')
        
        # Add playbill-specific export button after the metadata button
        self.export_button = QPushButton("Export Gameplay")
        self.export_button.setFont(self.ui.get_font('button'))
        self.export_button.setFixedSize(140, button_height)
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self.handle_export_gameplay)

        # Insert export button before the progress label and stretch
        # The layout should be: [metadata_button] [export_button] [progress_label] [stretch]
        # Remove the stretch first
        stretch_item = button_layout.takeAt(button_layout.count() - 1)
        
        # Add export button
        button_layout.addWidget(self.export_button)
        
        # Add stretch back
        button_layout.addItem(stretch_item)
        
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

    def enable_bot_buttons(self):
        """Handle this subclass-specific enabling of bot buttons - called by switchboard"""
        if DEBUG: print("DEBUG: Playbill: Enabling bot buttons")       
        # Show playbill-specific export button again
        self.enable_export_button()

    def disable_bot_buttons(self):
        """Handle this subclass-specific disabling of bot buttons - called by switchboard"""
        if DEBUG: print("DEBUG: Playbill: Disabling bot buttons")
        # Hide playbill-specific export button
        self.disable_export_button()

    def enable_export_button(self):
        """Enable export button - called by switchboard"""
        self.export_button.setEnabled(True)

    def disable_export_button(self):
        """Disable export button - called by switchboard"""
        self.export_button.setEnabled(False)

    def clear_project(self):
        """Clear current project and cancel any ongoing operations - override to handle playbill-specific state"""
        if DEBUG: print(f"DEBUG: Playbill: Clearing project")
        
        # Call parent clear method
        super().clear_project()
        
        # Show playbill-specific button and disable it
        if hasattr(self, 'export_button'):
            self.export_button.setVisible(True)
            self.export_button.setEnabled(False)  # Disabled when no project
        
        if DEBUG: print(f"DEBUG: Playbill: Project cleared")

    def handle_export_gameplay(self):
        """Handle export gameplay button click"""
        if DEBUG: print("DEBUG: Playbill: Export Gameplay button pressed")
        
        if not self.selected_item_widget:
            QMessageBox.information(self, "Export Gameplay", "Please select a gameplay video first.")
            return
            
        # Placeholder for future export functionality
        QMessageBox.information(self, "Export Gameplay", "Export functionality coming soon!")
