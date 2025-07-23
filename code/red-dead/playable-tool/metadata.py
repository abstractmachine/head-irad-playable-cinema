from PyQt5.QtCore import QObject, pyqtSignal
import os
import csv
import requests
import time
import re

class MetadataWorker(QObject):
    """Worker class for rebuilding metadata in a separate thread"""
    
    # Signals for communication with main thread
    progress = pyqtSignal(str)  # Progress message
    finished = pyqtSignal(bool)  # Success/failure
    error = pyqtSignal(str)  # Error message
    
    def __init__(self, project_folder):
        super().__init__()
        self.project_folder = project_folder
        self.tmdb_api_key = None
        self.opensubtitles_api_key = None
        
    def load_api_keys(self):
        """Load API keys from files"""
        try:
            # Load TMDB API key
            tmdb_key_path = os.path.join(os.path.dirname(__file__), 'preferences/tmdb_api_key.txt')
            with open(tmdb_key_path, 'r') as f:
                self.tmdb_api_key = f.read().strip()
            
            # Load OpenSubtitles API key
            opensubtitles_key_path = os.path.join(os.path.dirname(__file__), 'preferences/opensubtitles_api_key.txt')
            with open(opensubtitles_key_path, 'r') as f:
                self.opensubtitles_api_key = f.read().strip()
                
            return True
        except Exception as e:
            self.error.emit(f"Failed to load API keys: {str(e)}")
            return False
    
    def run(self):
        """Main worker function"""
        try:
            if not self.load_api_keys():
                return
            
            self.progress.emit("Scanning movie files...")
            
            # Get list of .mp4 files from movies folder
            movies_folder = os.path.join(self.project_folder, "movies")
            if not os.path.exists(movies_folder):
                self.error.emit("Movies folder not found")
                return
                
            # Filter out hidden files and macOS metadata files
            movie_files = [
                f for f in os.listdir(movies_folder) 
                if f.endswith('.mp4') and not f.startswith('.') and not f.startswith('._')
            ]
            
            if not movie_files:
                self.error.emit("No .mp4 files found in movies folder")
                return
                
            self.progress.emit(f"Found {len(movie_files)} movies")
            
            # Parse movie data and fetch TMDB metadata
            movies_data = []
            for i, filename in enumerate(movie_files):
                self.progress.emit(f"Processing {i+1}/{len(movie_files)}: {filename}")
                
                movie_data = self.parse_movie_filename(filename)
                if movie_data:
                    tmdb_data = self.fetch_tmdb_data(movie_data['tmdb'])
                    if tmdb_data:
                        movie_data.update(tmdb_data)
                        movies_data.append(movie_data)
                
                time.sleep(0.1)  # Be nice to the API
            
            # Download missing posters
            self.progress.emit("Checking posters...")
            self.download_missing_posters(movies_data)
            
            # Download missing subtitles
            self.progress.emit("Checking subtitles...")
            self.download_missing_subtitles(movies_data)
            
            # Write metadata.csv
            self.progress.emit("Writing metadata.csv...")
            self.write_metadata_csv(movies_data)
            
            self.progress.emit("Metadata rebuild complete!")
            self.finished.emit(True)
            
        except Exception as e:
            self.error.emit(f"Error during metadata rebuild: {str(e)}")
            self.finished.emit(False)
    
    def parse_movie_filename(self, filename):
        """Parse movie filename to extract name, year, tmdb_id"""
        # Pattern: Movie-Name(YYYY){tmdb-#####}.mp4
        match = re.match(r'^([^-]+(?:-[^-]+)*?)\((\d{4})\)\{tmdb-(\d+)\}\.mp4$', filename)
        if match:
            name = match.group(1).replace('-', ' ')
            year = match.group(2)
            tmdb_id = match.group(3)
            return {
                'title': name,
                'year': year,
                'tmdb': tmdb_id,  # Changed from 'tmdb_id' to 'tmdb'
                'filename': filename
            }
        return None
    
    def fetch_tmdb_data(self, tmdb_id):
        """Fetch movie data from TMDB API"""
        try:
            # Get basic movie info
            url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={self.tmdb_api_key}"
            response = requests.get(url)
            if response.status_code != 200:
                return None
                
            movie_data = response.json()
            
            # Get director from credits
            credits_url = f"https://api.themoviedb.org/3/movie/{tmdb_id}/credits?api_key={self.tmdb_api_key}"
            credits_response = requests.get(credits_url)
            director = ''
            if credits_response.status_code == 200:
                credits_data = credits_response.json()
                for crew_member in credits_data.get('crew', []):
                    if crew_member.get('job') == 'Director':
                        director = crew_member.get('name', '')
                        break
            
            return {
                'director': director,
                'imdb': movie_data.get('imdb_id', ''),  # Changed from 'imdb_id' to 'imdb'
                'overview': movie_data.get('overview', ''),
                'tagline': movie_data.get('tagline', ''),
                'poster_path': movie_data.get('poster_path', '')
            }
            
        except Exception as e:
            print(f"Error fetching TMDB data for {tmdb_id}: {e}")
            return None
    
    def download_missing_posters(self, movies_data):
        """Download missing poster images"""
        posters_folder = os.path.join(self.project_folder, "posters")
        os.makedirs(posters_folder, exist_ok=True)
        
        base_url = "https://image.tmdb.org/t/p/original"
        
        for movie in movies_data:
            if not movie.get('poster_path'):
                continue
                
            # Check if poster already exists
            filename_base = movie['filename'][:-4]  # Remove .mp4
            poster_path = None
            for ext in ['.jpg', '.jpeg', '.png']:
                potential_path = os.path.join(posters_folder, f"{filename_base}{ext}")
                if os.path.exists(potential_path):
                    poster_path = potential_path
                    break
            
            if poster_path:
                continue  # Poster already exists
                
            # Download poster
            try:
                poster_url = f"{base_url}{movie['poster_path']}"
                response = requests.get(poster_url)
                if response.status_code == 200:
                    poster_filename = os.path.join(posters_folder, f"{filename_base}.jpg")
                    with open(poster_filename, 'wb') as f:
                        f.write(response.content)
                    self.progress.emit(f"Downloaded poster: {movie['title']}")
            except Exception as e:
                print(f"Error downloading poster for {movie['title']}: {e}")
    
    def download_missing_subtitles(self, movies_data):
        """Download missing subtitle files"""
        subtitles_folder = os.path.join(self.project_folder, "subtitles")
        os.makedirs(subtitles_folder, exist_ok=True)
        
        for movie in movies_data:
            if not movie.get('imdb'):  # Changed from 'imdb_id' to 'imdb'
                continue
                
            # Check if subtitle already exists
            filename_base = movie['filename'][:-4]  # Remove .mp4
            subtitle_path = os.path.join(subtitles_folder, f"{filename_base}.srt")
            if os.path.exists(subtitle_path):
                continue  # Subtitle already exists
                
            # Download subtitle
            try:
                imdb_num = movie['imdb'].replace('tt', '')  # Changed from 'imdb_id' to 'imdb'
                url = f'https://api.opensubtitles.com/api/v1/subtitles?imdb_id={imdb_num}&languages=en'
                headers = {
                    'Api-Key': self.opensubtitles_api_key,
                    'User-Agent': 'PlayableCinema/1.0'
                }
                
                response = requests.get(url, headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    
                    # Find the first subtitle entry with a file
                    subtitle_entry = None
                    for entry in data.get('data', []):
                        files = entry.get('attributes', {}).get('files', [])
                        if files:
                            subtitle_entry = files[0]
                            break
                    
                    if subtitle_entry:
                        subtitle_file_id = subtitle_entry['file_id']
                        download_url = 'https://api.opensubtitles.com/api/v1/download'
                        download_response = requests.post(download_url, headers=headers, json={'file_id': subtitle_file_id})
                        
                        if download_response.status_code == 200:
                            download_data = download_response.json()
                            subtitle_url = download_data['link']
                            subtitle_file = requests.get(subtitle_url)
                            
                            if subtitle_file.status_code == 200:
                                with open(subtitle_path, 'wb') as f:
                                    f.write(subtitle_file.content)
                                self.progress.emit(f"Downloaded subtitle: {movie['title']}")
                
                time.sleep(1)  # Be polite to the API
                
            except Exception as e:
                print(f"Error downloading subtitle for {movie['title']}: {e}")
    
    def write_metadata_csv(self, movies_data):
        """Write metadata to CSV file"""
        metadata_folder = os.path.join(self.project_folder, "metadata")
        os.makedirs(metadata_folder, exist_ok=True)
        
        csv_path = os.path.join(metadata_folder, "metadata.csv")
        fieldnames = ['title', 'year', 'director', 'tmdb', 'imdb', 'filename', 'overview', 'tagline']
        
        # Sort movies alphabetically by title (case-insensitive)
        sorted_movies = sorted(movies_data, key=lambda movie: movie.get('title', '').lower())
        
        with open(csv_path, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            for movie in sorted_movies:
                writer.writerow({field: movie.get(field, '') for field in fieldnames})