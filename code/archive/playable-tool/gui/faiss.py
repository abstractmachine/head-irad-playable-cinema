DEBUG = False  # Set to True to enable debug output

from PyQt5.QtCore import QObject, pyqtSignal
from pathlib import Path
import json
import numpy as np
import pandas as pd
from sentence_transformers import SentenceTransformer
import faiss
import os

# Constants matching the dual-player logic
FIELD_ORDER = ["Protagonists", "Place", "Actions", "Objects"]
MODEL_NAME = "BAAI/bge-small-en-v1.5"

class FaissModule(QObject):
    """
    FAISS module that processes timecode changes when activated.
    This is not a window - just a background processing module.
    """
    preferences_save = pyqtSignal()
    preferences_load = pyqtSignal(dict)
    
    # Signal to send messages to robots console
    console_message = pyqtSignal(str)
    
    # Signal to request movie player to jump to a specific shot
    jump_to_movie_shot = pyqtSignal(str, int)  # (video_name, shot_index)

    def __init__(self, ui):
        super().__init__()
        self.ui = ui
        self.is_active = False
        self.project_folder = None
        self.last_shot_caption = ""
        self.model = None
        
        # Separate databases for playlists and shotlists
        self.playlist_db = {}  # Store playlist data
        self.shotlist_db = {}   # Store shotlist data
        
        # Local path to save the model in the project folder
        self.local_model_path = None  # Will be set in on_project_folder_loaded
        
        # FAISS index for fast similarity search
        self.faiss_index = None
        self.index_to_video = []  # Maps index position to (video_name, shot_index)

    def toggle(self):
        """Toggle FAISS mode on/off"""
        if self.is_active:
            self.deactivate()
        else:
            self.activate()
    
    def activate(self):
        """Activate FAISS mode"""
        self.is_active = True
        self.last_shot_caption = ""  # Reset when activating
        self.console_message.emit("FAISS on")
        if DEBUG: print("DEBUG: FAISS mode activated")
    
    def deactivate(self):
        """Deactivate FAISS mode"""
        self.is_active = False
        self.last_shot_caption = ""  # Reset when deactivating
        self.console_message.emit("FAISS off")
        if DEBUG: print("DEBUG: FAISS mode deactivated")
    
    def on_timecode_changed(self, timecode_ms):
        """Called when playhouse video timecode changes"""
        pass  # Not used for now
    
    def on_shot_caption_changed(self, caption):
        """Called when the current shot caption changes in playlist"""
        if not self.is_active:
            return
        
        # Only process if caption is non-empty and different from last
        if caption and caption != self.last_shot_caption:
            self.last_shot_caption = caption
            self.console_message.emit(f"[FAISS] Shot: {caption}")
            if DEBUG: print(f"DEBUG: FAISS shot caption changed to: {caption}")
            
            # Search for closest match in movie database
            self._search_similar_shot(caption)
    
    def _search_similar_shot(self, caption_json: str):
        """Search for the most similar shot in the shotlist database"""
        if not self.faiss_index or not self.model:
            self.console_message.emit("[FAISS] Search index not ready")
            return
        
        try:
            # Convert JSON caption to text format
            query_text = self._json_to_text(caption_json)
            
            if not query_text.strip():
                return
            
            # Encode the query
            query_embedding = self.model.encode([query_text], 
                                               convert_to_numpy=True, 
                                               normalize_embeddings=True).astype("float32")
            
            # Search for top 1 match in shotlist
            distances, indices = self.faiss_index.search(query_embedding, k=1)
            
            if len(indices) > 0 and len(indices[0]) > 0:
                best_idx = indices[0][0]
                distance = distances[0][0]
                
                # Get video name and shot index from shotlist
                video_name, shot_idx = self.index_to_video[best_idx]
                
                # Get the matched text
                matched_text = self.shotlist_db[video_name]["texts"][shot_idx]
                
                # Convert distance to similarity score (cosine similarity)
                similarity = 1.0 - distance
                
                self.console_message.emit(f"[FAISS] Match: {video_name} shot #{shot_idx} (similarity: {similarity:.3f})")
                self.console_message.emit(f"[FAISS] → {matched_text}")
                
                if DEBUG: print(f"DEBUG: Best match: {video_name}[{shot_idx}] sim={similarity:.3f}")
                
                # Emit signal to jump to this shot in the movie player
                self.jump_to_movie_shot.emit(video_name, shot_idx)
                
        except Exception as e:
            self.console_message.emit(f"[FAISS] Search error: {str(e)}")
            if DEBUG: print(f"DEBUG: Search error: {e}")
    
    def _load_model(self):
        """Load the model from local path or download if not available"""
        if self.local_model_path and os.path.exists(self.local_model_path):
            self.model = SentenceTransformer(self.local_model_path)
            self.console_message.emit(f"[FAISS] Loaded model from local path: {self.local_model_path}")
            if DEBUG: print(f"[FAISS] Loaded model from local path: {self.local_model_path}")
        else:
            self.console_message.emit(f"[FAISS] Downloading model {MODEL_NAME}...")
            if DEBUG: print(f"[FAISS] Downloading model {MODEL_NAME}...")
            try:
                self.model = SentenceTransformer(MODEL_NAME, device="cpu")
                if self.local_model_path:
                    os.makedirs(os.path.dirname(self.local_model_path), exist_ok=True)  # Create models directory
                    self.model.save(self.local_model_path)  # Save the model locally
                    self.console_message.emit(f"[FAISS] Model downloaded and saved to: {self.local_model_path}")
                    if DEBUG: print(f"[FAISS] Model downloaded and saved to: {self.local_model_path}")
            except Exception as e:
                self.console_message.emit(f"[FAISS] Error downloading model: {str(e)}")
                if DEBUG: print(f"DEBUG: Error downloading model: {e}")
    
    def on_project_folder_loaded(self, project_folder):
        """Set the project folder and process CSV files"""
        self.project_folder = project_folder
        self.local_model_path = os.path.join(project_folder, "models", "BAAI-bge-small-en-v1.5")  # Set model path
        if DEBUG: print(f"DEBUG: FAISS project folder set to {project_folder}")
        
        # Process all CSV files in playlists and shotlists
        self._process_project_csvs()
        
        # Load all embeddings into database
        self._load_embeddings_database()
        
        # Build FAISS index
        self._build_faiss_index()
    
    def _process_project_csvs(self):
        """Find and process all CSV files in project folders"""
        if not self.project_folder:
            return
        
        project_path = Path(self.project_folder)
        playlists_folder = project_path / "playlists"
        shotlists_folder = project_path / "shotlists"
        
        csv_files = []
        
        # Collect CSV files from both folders
        if playlists_folder.exists():
            csv_files.extend(playlists_folder.glob("*.csv"))
        
        if shotlists_folder.exists():
            csv_files.extend(shotlists_folder.glob("*.csv"))
        
        if not csv_files:
            if DEBUG: self.console_message.emit("[FAISS] No CSV files found in project")
            return
        
        if DEBUG: self.console_message.emit(f"[FAISS] Found {len(csv_files)} CSV file(s)")
        
        # Process each CSV
        for csv_path in csv_files:
            if "playlist" in csv_path.name.lower():
                self._process_playlist(csv_path)  # Process playlist separately
            elif "shotlist" in csv_path.name.lower():
                self._process_shotlist(csv_path)  # Process shotlist for embeddings

    def _process_playlist(self, csv_path: Path):
        """Process playlist CSV file"""
        # Load playlist data into playlist_db
        try:
            df = pd.read_csv(csv_path)
            self.playlist_db[csv_path.stem] = df  # Store the DataFrame
            self.console_message.emit(f"[FAISS] Loaded playlist: {csv_path.name}")
            if DEBUG: print(f"DEBUG: Loaded playlist: {csv_path.name}")
        except Exception as e:
            self.console_message.emit(f"[FAISS] ✗ Error loading playlist {csv_path.name}: {str(e)}")
            if DEBUG: print(f"DEBUG: Error loading playlist {csv_path.name}: {e}")

    def _process_shotlist(self, csv_path: Path):
        """Process shotlist CSV file and ensure embeddings are created"""
        self._ensure_embeddings(csv_path)  # Ensure embeddings for shotlist

    def _build_faiss_index(self):
        """Build FAISS index from all loaded embeddings"""
        if not self.shotlist_db:
            self.console_message.emit("[FAISS] No embeddings to index")
            return
        
        # Lazy-load model if needed for searches
        if self.model is None:
            self._load_model()  # Use the load_model method which handles local saving
        
        # Collect all non-empty embeddings and their mappings
        all_embeddings = []
        self.index_to_video = []
        
        for video_name, data in self.shotlist_db.items():
            embeddings = data["embeddings"]
            texts = data["texts"]
            
            for shot_idx, (emb, text) in enumerate(zip(embeddings, texts)):
                # Skip empty captions (ignored shots)
                if text.strip():
                    all_embeddings.append(emb)
                    self.index_to_video.append((video_name, shot_idx))
        
        if not all_embeddings:
            self.console_message.emit("[FAISS] No non-empty shots to index")
            return
        
        # Stack into matrix
        embeddings_matrix = np.vstack(all_embeddings).astype("float32")
        
        # Build FAISS index (using L2 distance, which works with normalized vectors)
        dimension = embeddings_matrix.shape[1]
        self.faiss_index = faiss.IndexFlatL2(dimension)
        self.faiss_index.add(embeddings_matrix)
        
        self.console_message.emit(f"[FAISS] Index built: {len(all_embeddings)} shots")
        if DEBUG: print(f"DEBUG: FAISS index built with {len(all_embeddings)} vectors, dim={dimension}")
    
    def _check_encoded_files(self, csv_path: Path):
        """Check if .txt and .npy files exist for a CSV"""
        stem = csv_path.stem
        parent = csv_path.parent
        txt_path = parent / f"{stem}.txt"
        npy_path = parent / f"{stem}.npy"
        
        if txt_path.exists() and npy_path.exists():
            return txt_path, npy_path
        return None, None
    
    def _json_to_text(self, caption_str: str) -> str:
        """Convert JSON caption to formatted text matching the reference format"""
        def to_list(v):
            if isinstance(v, list):
                return [str(x).strip() for x in v if str(x).strip()]
            if isinstance(v, str) and v.strip():
                return [v.strip()]
            return []
        
        values = {k: [] for k in FIELD_ORDER}
        
        if isinstance(caption_str, str) and caption_str.strip():
            try:
                obj = json.loads(caption_str)
                for k in FIELD_ORDER:
                    if k in obj:
                        values[k] = to_list(obj[k])
            except Exception:
                pass
        
        # Build parts in exact format: "Field: item1, item2, item3"
        parts = []
        for k in FIELD_ORDER:
            if values[k]:
                content = ", ".join(values[k])
            else:
                content = ""
            parts.append(f"{k}: {content}")
        
        # Join with " | " separator
        return " | ".join(parts)
    
    def _encode_csv_to_npy(self, csv_path: Path, out_txt: Path, out_npy: Path):
        """Encode CSV to .txt and .npy embeddings"""
        df = pd.read_csv(csv_path)
        
        # Keep row alignment: ignored rows become empty lines
        texts = []
        for _, row in df.iterrows():
            if str(row.get("Ignore", "")).strip().lower() == "yes":
                texts.append("")
            else:
                texts.append(self._json_to_text(row.get("Shot_Caption", "")))
        
        # Write .txt (one line per CSV row)
        with open(out_txt, "w", encoding="utf-8") as f:
            for line in texts:
                f.write(line + "\n")
        
        # Lazy-load the model
        if self.model is None:
            self.console_message.emit(f"[FAISS] Loading model {MODEL_NAME}...")
            self.model = SentenceTransformer(MODEL_NAME, device="cpu")
        
        # Embeddings for all rows (empty strings produce near-zero embeddings)
        X = self.model.encode(texts, convert_to_numpy=True, normalize_embeddings=True).astype("float32")
        np.save(out_npy, X)
        
        return X
    
    def _ensure_embeddings(self, csv_path: Path):
        """Ensure .txt and .npy files exist for a CSV"""
        txt_path, npy_path = self._check_encoded_files(csv_path)
        
        if txt_path and npy_path:
            if DEBUG: self.console_message.emit(f"[FAISS] ✓ {csv_path.name} already encoded")
            if DEBUG: print(f"DEBUG: Found precomputed files for {csv_path.name}")
            return txt_path, npy_path
        
        stem = csv_path.stem
        parent = csv_path.parent
        out_txt = parent / f"{stem}.txt"
        out_npy = parent / f"{stem}.npy"
        
        self.console_message.emit(f"[FAISS] Encoding {csv_path.name}...")
        if DEBUG: print(f"DEBUG: Encoding {csv_path.name} with {MODEL_NAME}")
        
        try:
            self._encode_csv_to_npy(csv_path, out_txt, out_npy)
            self.console_message.emit(f"[FAISS] ✓ Created {out_txt.name}, {out_npy.name}")
            if DEBUG: print(f"DEBUG: Done: {out_txt.name}, {out_npy.name}")
        except Exception as e:
            self.console_message.emit(f"[FAISS] ✗ Error encoding {csv_path.name}: {str(e)}")
            if DEBUG: print(f"DEBUG: Error encoding {csv_path.name}: {e}")
        
        return out_txt, out_npy
    
    def clear_project(self):
        """Clear project"""
        self.project_folder = None
        self.last_shot_caption = ""
        self.embeddings_db = {}  # Clear database
        self.faiss_index = None
        self.index_to_video = []
        if self.is_active:
            self.deactivate()
    
    def on_preferences_save(self):
        """Save preferences"""
        self._pending_save_data = {
            "is_active": self.is_active
        }
        return self._pending_save_data
    
    def on_preferences_load(self, data):
        """Load preferences"""
        # Don't auto-activate on startup - just store the preference
        # User must manually activate FAISS after video is loaded
        if data and data.get("is_active"):
            # Store that it was active, but don't activate yet
            # The user will need to manually toggle it on
            pass  # Do nothing - let user activate manually
    
    def _load_embeddings_database(self):
        """Load all .txt and .npy files into memory database"""
        if not self.project_folder:
            return
        
        self.shotlist_db = {}
        project_path = Path(self.project_folder)
        
        # Only check shotlists folder (not playlists)
        shotlists_folder = project_path / "shotlists"
        
        if not shotlists_folder.exists():
            if DEBUG: self.console_message.emit("[FAISS] No shotlists folder found")
            return
        
        loaded_count = 0
        # Find all .npy files in shotlists
        for npy_path in shotlists_folder.glob("*.npy"):
            txt_path = npy_path.with_suffix(".txt")
            
            # Only load if both .txt and .npy exist
            if not txt_path.exists():
                continue
            
            # Video name is the filename without extension
            video_name = npy_path.stem
            
            try:
                # Load embeddings
                embeddings = np.load(npy_path)
                
                # Load text lines
                with open(txt_path, "r", encoding="utf-8") as f:
                    texts = [line.rstrip("\n") for line in f]
                
                # Store in shotlist database
                self.shotlist_db[video_name] = {
                    "texts": texts,
                    "embeddings": embeddings,
                    "txt_path": txt_path,
                    "npy_path": npy_path
                }
                
                loaded_count += 1
                # self.console_message.emit(f"[FAISS] Loaded {video_name} ({len(texts)} shots)")
                if DEBUG: print(f"DEBUG: Loaded embeddings for {video_name}: {embeddings.shape}")
                
            except Exception as e:
                self.console_message.emit(f"[FAISS] ✗ Error loading {video_name}: {str(e)}")
                if DEBUG: print(f"DEBUG: Error loading {video_name}: {e}")
        
        if loaded_count > 0:
            self.console_message.emit(f"[FAISS] Database ready: {loaded_count} video(s)")
        else:
            self.console_message.emit("[FAISS] No embeddings found in shotlists")