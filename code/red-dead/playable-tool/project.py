DEBUG = False  # Set to True to enable debug output

# Qt stuff
from PyQt5.QtCore import QObject, pyqtSignal, Qt
from PyQt5.QtWidgets import QMessageBox
from PyQt5.QtGui import QPixmap

# Python stuff
import os

class ProjectManager(QObject):
    """Core project management logic (non-UI)"""
    
    # Signals
    project_loaded = pyqtSignal(str)  # Emitted when project folder is set/loaded
    project_changed = pyqtSignal(str)  # Emitted when project folder changes
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.project_folder = None
        self.parent_widget = parent  # For showing message boxes
    
    def set_project_folder(self, folder):
        """Set the project folder and validate its structure"""
        if DEBUG: 
            print(f"DEBUG: ProjectManager: Setting project folder to {folder}")
        
        # Don't reload if it's the same folder
        if self.project_folder == folder:
            if DEBUG: 
                print(f"DEBUG: ProjectManager: Project folder already set to this folder, skipping reload")
            # Only emit project_loaded, not project_changed
            self.project_loaded.emit(folder)
            return True
        
        # Validate and setup project structure
        if self.validate_and_setup_project_structure(folder):
            old_folder = self.project_folder
            self.project_folder = folder
            
            # Emit signals only if folder changed
            if old_folder != folder:
                self.project_changed.emit(folder)
            self.project_loaded.emit(folder)
            
            return True
        return False
    
    def get_project_folder(self):
        """Get the current project folder"""
        return self.project_folder
    
    def validate_and_setup_project_structure(self, folder):
        """Validate project folder structure and create missing components"""
        if DEBUG: 
            print(f"DEBUG: ProjectManager: Validating project structure in {folder}")
        
        try:
            # Get required structure
            required_folders = self.get_required_folders()
            gitignore_folders = self.get_gitignore_folders()
            required_files = self.get_required_files()
            
            # Check and create missing folders
            if not self._create_missing_folders(folder, required_folders, gitignore_folders):
                return False
            
            # Check and create missing files
            if not self._create_missing_files(folder, required_files):
                return False
            
            if DEBUG: 
                print("DEBUG: ProjectManager: All required folders and files are present.")
            return True
            
        except Exception as e:
            self._show_error(f"Failed to validate project structure:\n{str(e)}")
            return False
    
    def _create_missing_folders(self, base_folder, required_folders, gitignore_folders):
        """Create missing folders and prompt user if necessary"""
        missing_folders = []
        for required_folder in required_folders:
            folder_path = os.path.join(base_folder, required_folder)
            if not os.path.exists(folder_path):
                missing_folders.append(required_folder)
        
        if not missing_folders:
            return True
        
        # Prompt user to create missing folders
        message = ("The following project folder(s) could not be found:\n" + 
                  "\n".join(missing_folders) + 
                  "\n\nDo you want to create them now?")
        
        if not self._show_question("Missing Folders", message):
            return False
        
        # Create the folders
        try:
            for folder_name in missing_folders:
                folder_path = os.path.join(base_folder, folder_name)
                os.makedirs(folder_path, exist_ok=True)
                
                # Add .gitignore if needed
                if folder_name in gitignore_folders:
                    self._create_gitignore(folder_path)
            return True
            
        except Exception as e:
            self._show_error(f"Failed to create folders:\n{str(e)}")
            return False
    
    def _create_missing_files(self, base_folder, required_files):
        """Create missing required files"""
        for required_file in required_files:
            file_path = os.path.join(base_folder, required_file)
            if not os.path.exists(file_path):
                try:
                    os.makedirs(os.path.dirname(file_path), exist_ok=True)
                    with open(file_path, 'w', encoding="utf-8") as f:
                        f.write("")  # Create an empty file
                except Exception as e:
                    self._show_error(f"Failed to create {required_file}:\n{str(e)}")
                    return False
        return True
    
    def _create_gitignore(self, folder_path):
        """Create a .gitignore file in the specified folder"""
        gitignore_path = os.path.join(folder_path, ".gitignore")
        if not os.path.exists(gitignore_path):
            try:
                with open(gitignore_path, 'w', encoding="utf-8") as f:
                    f.write("# Ignore all files in this folder (Remove this to reactivate git syncing)\n*\n")
            except Exception as e:
                self._show_error(f"Failed to create .gitignore:\n{str(e)}")
    
    def get_required_folders(self):
        """Get list of required folders for the project"""
        return [
            "datasets", 
            "gameplay", 
            "metadata", 
            "movies", 
            "posters", 
            "preferences", 
            "prompts", 
            "shotlists", 
            "subtitles",
            "thumbnails"
        ]
    
    def get_gitignore_folders(self):
        """Get list of folders that should have .gitignore files"""
        return [
            "movies", 
            "gameplay", 
            "posters", 
            "preferences", 
            "subtitles",
            "thumbnails"
        ]
    
    def get_required_files(self):
        """Get list of required files for the project"""
        return [
            "preferences/openai_api_key.txt",
            "preferences/tmdb_api_key.txt", 
            "preferences/opensubtitles_api_key.txt"
        ]
    
    def get_folder_path(self, folder_name):
        """Get the full path to a specific folder within the project"""
        if not self.project_folder:
            return None
        return os.path.join(self.project_folder, folder_name)
    
    def get_file_path(self, file_path):
        """Get the full path to a specific file within the project"""
        if not self.project_folder:
            return None
        return os.path.join(self.project_folder, file_path)
    
    def folder_exists(self, folder_name):
        """Check if a specific folder exists in the project"""
        folder_path = self.get_folder_path(folder_name)
        return folder_path and os.path.exists(folder_path)
    
    def file_exists(self, file_path):
        """Check if a specific file exists in the project"""
        full_path = self.get_file_path(file_path)
        return full_path and os.path.exists(full_path)
    
    def _show_error(self, message):
        """Show error message to user"""
        if self.parent_widget:
            QMessageBox.critical(self.parent_widget, "Project Error", message)
        else:
            print(f"PROJECT ERROR: {message}")
    
    def _show_question(self, title, message):
        """Show question dialog to user"""
        if self.parent_widget:
            msg = QMessageBox()
            msg.setWindowTitle(title)
            msg.setText(message)
            msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            msg.setDefaultButton(QMessageBox.No)

            # Set custom icon
            icon = QPixmap("ui/icons/cowgirl_folder.png")
            icon_size = 128
            scaled_icon = icon.scaled(icon_size, icon_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            msg.setIconPixmap(scaled_icon)

            result = msg.exec_()
            return result == QMessageBox.Yes
        else:
            print(f"PROJECT QUESTION: {title} - {message}")
            return True  # Default to yes in non-GUI mode
    
    # ---- Save/Load Preferences ----
    
    def get_preferences_data(self):
        """Get project data for saving to preferences"""
        return {
            "project_folder": self.project_folder
        }
    
    def load_preferences_data(self, data):
        """Load project data from preferences"""
        if "project_folder" in data and data["project_folder"]:
            folder = data["project_folder"]
            if os.path.exists(folder):
                if self.project_folder != folder:
                    self.set_project_folder(folder)
            else:
                # Project folder no longer exists, reset
                self.project_folder = None
                self.project_changed.emit("")
