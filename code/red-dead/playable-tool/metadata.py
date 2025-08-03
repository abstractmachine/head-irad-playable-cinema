DEBUG = False  # Set to True to enable debug output

from PyQt5.QtCore import QObject, pyqtSignal
import os
import csv
import requests
import time
import re
import subprocess
import json

class MetadataWorker(QObject):
    """Worker class for rebuilding metadata in a separate thread"""
    
    # Signals for communication with main thread
    progress = pyqtSignal(str)  # Progress message
    finished = pyqtSignal(bool)  # Success/failure
    error = pyqtSignal(str)  # Error message
    
    def __init__(self, project_folder, data_folder="movies", metadata_filename="metadata.csv"):
        super().__init__()
        self.project_folder = project_folder
        self.data_folder = data_folder  # "movies" or "gameplay"
        self.metadata_filename = metadata_filename  # "metadata.csv" or "gameplay_metadata.csv"
        self.tmdb_api_key = None
        self.opensubtitles_api_key = None
        
        if DEBUG:
            print(f"DEBUG: MetadataWorker: data_folder = '{self.data_folder}', metadata_filename = '{self.metadata_filename}'")
    
    def set_data_folder(self, data_folder):
        """Set the data folder (movies, gameplay, etc.)"""
        self.data_folder = data_folder
        if DEBUG: print(f"DEBUG: MetadataWorker: data_folder set to '{self.data_folder}'")
    
    def set_metadata_filename(self, metadata_filename):
        """Set the metadata filename"""
        self.metadata_filename = metadata_filename
        if DEBUG: print(f"DEBUG: MetadataWorker: metadata_filename set to '{self.metadata_filename}'")
        
    def load_api_keys(self):
        """Load API keys from files"""
        try:
            # Load TMDB API key
            tmdb_key_path = os.path.join(self.project_folder, 'preferences', 'tmdb_api_key.txt')
            with open(tmdb_key_path, 'r') as f:
                self.tmdb_api_key = f.read().strip()

            # Load OpenSubtitles API key
            opensubtitles_key_path = os.path.join(self.project_folder, 'preferences', 'opensubtitles_api_key.txt')
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
            
            self.progress.emit(f"Scanning {self.data_folder} files...")
            
            # Get list of .mp4 files from data folder (was hardcoded to "movies")
            data_folder_path = os.path.join(self.project_folder, self.data_folder)
            if not os.path.exists(data_folder_path):
                self.error.emit(f"{self.data_folder} folder not found")
                return
                
            # Filter out hidden files and macOS metadata files
            video_files = [
                f for f in os.listdir(data_folder_path) 
                if f.endswith('.mp4') and not f.startswith('.') and not f.startswith('._')
            ]
            
            if not video_files:
                self.error.emit(f"No .mp4 files found in {self.data_folder} folder")
                return
                
            self.progress.emit(f"Found {len(video_files)} videos")
            
            # Parse video data and fetch TMDB metadata (if it's a movie)
            videos_data = []
            for i, filename in enumerate(video_files):
                self.progress.emit(f"Processing {i+1}/{len(video_files)}: {filename}")
                
                if self.data_folder == "movies":
                    # For movies, parse filename and fetch TMDB data
                    video_data = self.parse_movie_filename(filename)
                    if video_data:
                        # Get video duration
                        duration = self.get_video_duration(os.path.join(data_folder_path, filename))
                        if duration:
                            video_data['duration'] = duration
                        
                        tmdb_data = self.fetch_tmdb_data(video_data['tmdb'])
                        if tmdb_data:
                            video_data.update(tmdb_data)
                            videos_data.append(video_data)
                else:
                    # For gameplay videos, create basic metadata
                    video_data = self.create_basic_video_metadata(filename)
                    duration = self.get_video_duration(os.path.join(data_folder_path, filename))
                    if duration:
                        video_data['duration'] = duration
                    videos_data.append(video_data)
                
                time.sleep(0.1)  # Be nice to the API
            
            if self.data_folder == "movies":
                # Download missing posters and subtitles only for movies
                self.progress.emit("Checking posters...")
                self.download_missing_posters(videos_data)
                
                self.progress.emit("Checking subtitles...")
                self.download_missing_subtitles(videos_data)
            elif self.data_folder == "gameplay":
                # Generate thumbnails for gameplay videos
                self.progress.emit("Generating thumbnails...")
                self.generate_missing_thumbnails(videos_data)
            
            # Write metadata file
            self.progress.emit(f"Writing {self.metadata_filename}...")
            self.write_metadata_csv(videos_data)
            
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
                    
                    # Find the subtitle entry with the highest ratings
                    best_subtitle_entry = None
                    highest_ratings = -1
                    
                    for entry in data.get('data', []):
                        files = entry.get('attributes', {}).get('files', [])
                        ratings = entry.get('attributes', {}).get('ratings', 0)
                        
                        if files and ratings > highest_ratings:
                            highest_ratings = ratings
                            best_subtitle_entry = files[0]
                    
                    if best_subtitle_entry:
                        subtitle_file_id = best_subtitle_entry['file_id']
                        download_url = 'https://api.opensubtitles.com/api/v1/download'
                        download_response = requests.post(download_url, headers=headers, json={'file_id': subtitle_file_id})
                        
                        if download_response.status_code == 200:
                            download_data = download_response.json()
                            subtitle_url = download_data['link']
                            subtitle_file = requests.get(subtitle_url)
                            
                            if subtitle_file.status_code == 200:
                                with open(subtitle_path, 'wb') as f:
                                    f.write(subtitle_file.content)
                                self.progress.emit(f"Downloaded subtitle: {movie['title']} (rating: {highest_ratings})")
                
                time.sleep(1)  # Be polite to the API
                
            except Exception as e:
                print(f"Error downloading subtitle for {movie['title']}: {e}")
    
    def get_video_duration(self, video_path):
        """Get video duration in minutes using ffprobe"""
        try:
            # Use ffprobe to get video duration
            cmd = [
                'ffprobe', 
                '-v', 'quiet',
                '-print_format', 'json',
                '-show_format',
                video_path
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0:
                data = json.loads(result.stdout)
                duration_seconds = float(data['format']['duration'])
                duration_minutes = int(duration_seconds / 60)
                return duration_minutes
            else:
                print(f"ffprobe failed for {video_path}: {result.stderr}")
                return None
                
        except subprocess.TimeoutExpired:
            print(f"ffprobe timeout for {video_path}")
            return None
        except FileNotFoundError:
            print("ffprobe not found. Please install FFmpeg.")
            return None
        except Exception as e:
            print(f"Error getting duration for {video_path}: {e}")
            return None
    
    def create_basic_video_metadata(self, filename):
        """Create basic metadata for non-movie videos (like gameplay)"""
        # Extract title from filename (remove extension and clean up)
        title = os.path.splitext(filename)[0]
        title = title.replace('-', ' ').replace('_', ' ')
        
        return {
            'title': title,
            'year': '',
            'director': '',
            'tmdb': '',
            'imdb': '',
            'filename': filename,
            'overview': '',
            'tagline': ''
        }
    
    def write_metadata_csv(self, videos_data):
        """Write metadata to CSV file"""
        metadata_folder = os.path.join(self.project_folder, "metadata")
        os.makedirs(metadata_folder, exist_ok=True)
        
        csv_path = os.path.join(metadata_folder, self.metadata_filename)  # Use configurable filename
        fieldnames = ['title', 'year', 'director', 'tmdb', 'imdb', 'filename', 'duration', 'overview', 'tagline']
        
        # Sort videos alphabetically by title (case-insensitive)
        sorted_videos = sorted(videos_data, key=lambda video: video.get('title', '').lower())
        
        with open(csv_path, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            for video in sorted_videos:
                writer.writerow({field: video.get(field, '') for field in fieldnames})
    
    def generate_missing_thumbnails(self, videos_data):
        """Generate thumbnail images from first frame of gameplay videos"""
        thumbnails_folder = os.path.join(self.project_folder, "thumbnails")
        os.makedirs(thumbnails_folder, exist_ok=True)
        
        for video in videos_data:
            filename = video.get('filename', '')
            if not filename:
                continue
                
            # Check if thumbnail already exists
            filename_base = filename[:-4] if filename.endswith('.mp4') else filename  # Remove .mp4
            thumbnail_path = os.path.join(thumbnails_folder, f"{filename_base}.jpg")
            
            if os.path.exists(thumbnail_path):
                continue  # Thumbnail already exists
                
            # Generate thumbnail from first frame
            try:
                video_path = os.path.join(self.project_folder, self.data_folder, filename)
                if not os.path.exists(video_path):
                    continue
                    
                # Use ffmpeg to extract first frame as thumbnail
                cmd = [
                    'ffmpeg',
                    '-i', video_path,           # Input video
                    '-ss', '00:00:01',          # Seek to 1 second (to avoid black frames)
                    '-vframes', '1',            # Extract only 1 frame
                    '-q:v', '2',                # High quality
                    '-y',                       # Overwrite output file
                    thumbnail_path              # Output thumbnail
                ]
                
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                
                if result.returncode == 0:
                    self.progress.emit(f"Generated thumbnail: {video['title']}")
                    if DEBUG:
                        print(f"DEBUG: Generated thumbnail for {filename}: {thumbnail_path}")
                else:
                    if DEBUG:
                        print(f"DEBUG: ffmpeg failed for {filename}: {result.stderr}")
                    
                    # Try with no seek (some videos might not support seeking)
                    cmd_no_seek = [
                        'ffmpeg',
                        '-i', video_path,
                        '-vframes', '1',
                        '-q:v', '2',
                        '-y',
                        thumbnail_path
                    ]
                    
                    result_no_seek = subprocess.run(cmd_no_seek, capture_output=True, text=True, timeout=60)
                    if result_no_seek.returncode == 0:
                        self.progress.emit(f"Generated thumbnail: {video['title']}")
                        if DEBUG:
                            print(f"DEBUG: Generated thumbnail (no seek) for {filename}: {thumbnail_path}")
                    else:
                        if DEBUG:
                            print(f"DEBUG: ffmpeg failed (no seek) for {filename}: {result_no_seek.stderr}")
                
            except subprocess.TimeoutExpired:
                if DEBUG:
                    print(f"DEBUG: ffmpeg timeout for {filename}")
            except FileNotFoundError:
                if DEBUG:
                    print("DEBUG: ffmpeg not found. Please install FFmpeg.")
                self.progress.emit("FFmpeg not found - cannot generate thumbnails")
                break
            except Exception as e:
                if DEBUG:
                    print(f"DEBUG: Error generating thumbnail for {filename}: {e}")