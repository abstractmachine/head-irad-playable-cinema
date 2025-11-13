import os
import csv
from typing import List, Dict, Optional

class MediaLibrary:
    """
    Base class for loading and managing media metadata (cinematheque or gameplay).
    """
    def __init__(self, csv_path: str, project_root: str, video_dir: str, shotlist_dir: str):
        self.csv_path = csv_path
        self.project_root = project_root
        self.video_dir = video_dir
        self.shotlist_dir = shotlist_dir
        self.items: List[Dict] = []
        self._load()

    def _load(self):
        """Load the CSV into self.items as a list of dicts."""
        if not os.path.exists(self.csv_path):
            raise FileNotFoundError(f"CSV not found: {self.csv_path}")
        
        with open(self.csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            self.items = [row for row in reader]
        
        if not self.items:
            raise ValueError(f"No entries found in {self.csv_path}")

    def get(self, index: int) -> Optional[Dict]:
        """Retrieve an item by index. Returns None if out of range."""
        if 0 <= index < len(self.items):
            return self.items[index]
        return None

    def get_video_path(self, item: Dict) -> str:
        """Get the full path to the video file."""
        filename = item.get('Filename') or item.get('filename', '')
        return os.path.join(self.project_root, self.video_dir, filename)

    def load_shotlist(self, item: Dict) -> Optional[List[Dict]]:
        """
        Load the shotlist CSV for a given item.
        Returns list of shot dicts or None if not found.
        """
        filename = item.get('Filename') or item.get('filename', '')
        if not filename:
            return None
        
        # Extract filename without extension
        base_name = os.path.splitext(filename)[0]
        
        # Build path to shotlist
        shotlist_path = os.path.join(
            self.project_root,
            self.shotlist_dir,
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

    def save_shotlist(self, item: Dict, shots: List[Dict]) -> bool:
        """
        Save the shotlist CSV for a given item.
        Returns True if successful, False otherwise.
        """
        filename = item.get('Filename') or item.get('filename', '')
        if not filename:
            return False
        
        # Extract filename without extension
        base_name = os.path.splitext(filename)[0]
        
        # Build path to shotlist
        shotlist_path = os.path.join(
            self.project_root,
            self.shotlist_dir,
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

    def erase_shot_captions(self, item: Dict) -> bool:
        """
        Erase all Shot_Caption entries in the shotlist.
        Returns True if successful, False otherwise.
        """
        shotlist = self.load_shotlist(item)
        if not shotlist:
            return False
        
        # Clear Shot_Caption field
        for shot in shotlist:
            if 'Shot_Caption' in shot:
                shot['Shot_Caption'] = ''
        
        return self.save_shotlist(item, shotlist)

    def erase_scene_captions(self, item: Dict) -> bool:
        """
        Erase all Scene_Caption entries in the shotlist.
        Returns True if successful, False otherwise.
        """
        shotlist = self.load_shotlist(item)
        if not shotlist:
            return False
        
        # Clear Scene_Caption field
        for shot in shotlist:
            if 'Scene_Caption' in shot:
                shot['Scene_Caption'] = ''
        
        return self.save_shotlist(item, shotlist)

    def __len__(self):
        return len(self.items)

    def __repr__(self):
        return f"<{self.__class__.__name__}: {len(self.items)} items from {self.csv_path}>"


class Cinematheque(MediaLibrary):
    """
    Loads and manages the cinematheque.csv metadata for movies.
    """
    def __init__(self, csv_path: str, project_root: str):
        super().__init__(
            csv_path=csv_path,
            project_root=project_root,
            video_dir="movies",
            shotlist_dir="shotlists"
        )

    def get_title(self, item: Dict) -> str:
        """Get the display title for a film."""
        return f"{item.get('title', 'Unknown')} ({item.get('year', 'Unknown')})"


class Gameplay(MediaLibrary):
    """
    Loads and manages the gameplay.csv metadata for gameplay videos.
    """
    def __init__(self, csv_path: str, project_root: str):
        super().__init__(
            csv_path=csv_path,
            project_root=project_root,
            video_dir="gameplay",
            shotlist_dir="playlists"
        )

    def get_title(self, item: Dict) -> str:
        """Get the display title for a gameplay video."""
        return item.get('title', 'Unknown')