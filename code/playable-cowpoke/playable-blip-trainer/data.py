import os
import csv
from typing import List, Dict, Optional

class Cinematheque:
    """
    Loads and manages the cinematheque.csv metadata.
    """
    def __init__(self, csv_path: str, project_root: str):
        self.csv_path = csv_path
        self.project_root = project_root
        self.films: List[Dict] = []
        self._load()

    def _load(self):
        """Load the CSV into self.films as a list of dicts."""
        if not os.path.exists(self.csv_path):
            raise FileNotFoundError(f"Cinematheque CSV not found: {self.csv_path}")
        
        with open(self.csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            self.films = [row for row in reader]
        
        if not self.films:
            raise ValueError(f"No entries found in {self.csv_path}")

    def get(self, index: int) -> Optional[Dict]:
        """Retrieve a film by index. Returns None if out of range."""
        if 0 <= index < len(self.films):
            return self.films[index]
        return None

    def load_shotlist(self, film: Dict) -> Optional[List[Dict]]:
        """
        Load the shotlist CSV for a given film.
        Expects film dict to have a 'filename' key with the video filename.
        Returns list of shot dicts or None if not found.
        """
        if 'filename' not in film:
            return None
        
        # Extract filename without extension
        filename = film['filename']
        base_name = os.path.splitext(filename)[0]
        
        # Build path to shotlist
        shotlist_path = os.path.join(
            self.project_root,
            "shotlists",
            f"{base_name}.csv"
        )
        
        if not os.path.exists(shotlist_path):
            return None
        
        # Load shotlist CSV
        shots = []
        with open(shotlist_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            shots = [row for row in reader]
        
        return shots

    def save_shotlist(self, film: Dict, shots: List[Dict]) -> bool:
        """
        Save the shotlist CSV for a given film.
        Returns True if successful, False otherwise.
        """
        if 'filename' not in film:
            return False
        
        # Extract filename without extension
        filename = film['filename']
        base_name = os.path.splitext(filename)[0]
        
        # Build path to shotlist
        shotlist_path = os.path.join(
            self.project_root,
            "shotlists",
            f"{base_name}.csv"
        )
        
        if not shots:
            return False
        
        # Write shotlist CSV
        try:
            with open(shotlist_path, 'w', newline="", encoding="utf-8") as f:
                fieldnames = shots[0].keys()
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(shots)
            return True
        except Exception as e:
            print(f"Error saving shotlist: {e}")
            return False

    def erase_shot_captions(self, film: Dict) -> bool:
        """
        Erase all Shot_Caption entries in the shotlist.
        Returns True if successful, False otherwise.
        """
        shotlist = self.load_shotlist(film)
        if not shotlist:
            return False
        
        # Clear Shot_Caption field
        for shot in shotlist:
            if 'Shot_Caption' in shot:
                shot['Shot_Caption'] = ''
        
        return self.save_shotlist(film, shotlist)

    def erase_scene_captions(self, film: Dict) -> bool:
        """
        Erase all Scene_Caption entries in the shotlist.
        Returns True if successful, False otherwise.
        """
        shotlist = self.load_shotlist(film)
        if not shotlist:
            return False
        
        # Clear Scene_Caption field
        for shot in shotlist:
            if 'Scene_Caption' in shot:
                shot['Scene_Caption'] = ''
        
        return self.save_shotlist(film, shotlist)

    def __len__(self):
        return len(self.films)

    def __repr__(self):
        return f"<Cinematheque: {len(self.films)} films from {self.csv_path}>"