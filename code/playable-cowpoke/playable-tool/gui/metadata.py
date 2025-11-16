DEBUG = True  # Set to True to enable debug output

from PyQt5.QtCore import QObject, pyqtSignal
import os
import csv
import requests
import time
import re
import subprocess
import json
import unicodedata

from gui.utility import html_encode_text, html_decode_text

class MetadataWorker(QObject):
    """Worker class for rebuilding metadata in a separate thread"""
    
    # Signals for communication with main thread
    progress = pyqtSignal(int)  # Progress percentage (0-100) - CHANGED FROM STRING TO INT
    finished = pyqtSignal(bool)  # Success/failure
    error = pyqtSignal(str)  # Error message
    
    def __init__(self, project_folder, data_folder="movies", metadata_filename="metadata.csv"):
        super().__init__()
        self.project_folder = project_folder
        self.data_folder = data_folder
        self.metadata_filename = metadata_filename
        self.tmdb_api_key = None
        self.opensubtitles_api_key = None

        if DEBUG:
            print(f"DEBUG: __init__: project_folder={self.project_folder}, data_folder={self.data_folder}, metadata_filename={self.metadata_filename}")

    def set_data_folder(self, data_folder):
        self.data_folder = data_folder
        if DEBUG:
            print(f"DEBUG: set_data_folder: data_folder set to '{self.data_folder}'")

    def set_metadata_filename(self, metadata_filename):
        self.metadata_filename = metadata_filename
        if DEBUG:
            print(f"DEBUG: set_metadata_filename: metadata_filename set to '{self.metadata_filename}'")

    def load_api_keys(self):
        try:
            tmdb_key_path = os.path.join(self.project_folder, 'preferences', 'tmdb_api_key.txt')
            if DEBUG:
                print(f"DEBUG: load_api_keys: Loading TMDB key from {tmdb_key_path}")
            with open(tmdb_key_path, 'r', encoding="utf-8") as f:
                self.tmdb_api_key = f.read().strip()
            if DEBUG:
                print(f"DEBUG: load_api_keys: TMDB key loaded: {self.tmdb_api_key[:4]}...")

            opensubtitles_key_path = os.path.join(self.project_folder, 'preferences', 'opensubtitles_api_key.txt')
            if DEBUG:
                print(f"DEBUG: load_api_keys: Loading OpenSubtitles key from {opensubtitles_key_path}")
            with open(opensubtitles_key_path, 'r', encoding="utf-8") as f:
                self.opensubtitles_api_key = f.read().strip()
            if DEBUG:
                print(f"DEBUG: load_api_keys: OpenSubtitles key loaded: {self.opensubtitles_api_key[:4]}...")

            return True
        except Exception as e:
            if DEBUG:
                print(f"DEBUG: load_api_keys: Exception: {e}")
            self.error.emit(f"Failed to load API keys: {str(e)}")
            return False

    def run(self):
        try:
            if DEBUG:
                print("DEBUG: run: Starting metadata rebuild process")
            if not self.load_api_keys():
                if DEBUG:
                    print("DEBUG: run: Failed to load API keys, aborting")
                return

            self.progress.emit(5)
            data_folder_path = os.path.join(self.project_folder, self.data_folder)
            if DEBUG:
                print(f"DEBUG: run: data_folder_path = {data_folder_path}")
            if not os.path.exists(data_folder_path):
                if DEBUG:
                    print(f"DEBUG: run: Data folder '{data_folder_path}' not found")
                self.error.emit(f"{self.data_folder} folder not found")
                return

            video_files = [
                f for f in os.listdir(data_folder_path)
                if f.endswith('.mp4') and not f.startswith('.') and not f.startswith('._')
            ]
            if DEBUG:
                print(f"DEBUG: run: Found {len(video_files)} video files: {video_files}")

            if not video_files:
                if DEBUG:
                    print(f"DEBUG: run: No .mp4 files found in {self.data_folder}")
                self.error.emit(f"No .mp4 files found in {self.data_folder} folder")
                return

            self.progress.emit(10)
            videos_data = []
            total_files = len(video_files)

            for i, filename in enumerate(video_files):
                progress = 10 + int((i / total_files) * 50)
                self.progress.emit(progress)
                if DEBUG:
                    print(f"DEBUG: run: Processing file {i+1}/{total_files}: {filename}")

                if self.data_folder == "movies":
                    video_data = self.parse_movie_filename(filename)
                    if DEBUG:
                        print(f"DEBUG: run: Parsed movie filename: {video_data}")
                    if video_data:
                        duration = self.get_video_duration(os.path.join(data_folder_path, filename))
                        if DEBUG:
                            print(f"DEBUG: run: Duration for {filename}: {duration}")
                        if duration:
                            video_data['duration'] = duration

                        tmdb_data = self.fetch_tmdb_data(video_data['tmdb'])
                        if DEBUG:
                            print(f"DEBUG: run: TMDB data for {filename}: {tmdb_data}")
                        if tmdb_data:
                            video_data.update(tmdb_data)
                            videos_data.append(video_data)
                else:
                    video_data = self.create_basic_video_metadata(filename)
                    if DEBUG:
                        print(f"DEBUG: run: Basic video metadata: {video_data}")
                    duration = self.get_video_duration(os.path.join(data_folder_path, filename))
                    if DEBUG:
                        print(f"DEBUG: run: Duration for {filename}: {duration}")
                    if duration:
                        video_data['duration'] = duration
                    videos_data.append(video_data)

                time.sleep(0.1)

            if self.data_folder == "movies":
                self.progress.emit(65)
                if DEBUG:
                    print("DEBUG: run: Downloading missing posters")
                self.download_missing_posters(videos_data)

                self.progress.emit(80)
                if DEBUG:
                    print("DEBUG: run: Downloading missing subtitles")
                self.download_missing_subtitles(videos_data)
            elif self.data_folder == "gameplay":
                self.progress.emit(70)
                if DEBUG:
                    print("DEBUG: run: Generating missing thumbnails")
                self.generate_missing_thumbnails(videos_data)

            self.progress.emit(95)
            if DEBUG:
                print("DEBUG: run: Writing metadata CSV")
            self.write_metadata_csv(videos_data)

            self.progress.emit(100)
            if DEBUG:
                print("DEBUG: run: Metadata rebuild complete")
            self.finished.emit(True)

        except Exception as e:
            if DEBUG:
                print(f"DEBUG: run: Exception: {e}")
            self.error.emit(f"Error during metadata rebuild: {str(e)}")
            self.finished.emit(False)

    def parse_movie_filename(self, filename):
        if DEBUG:
            print(f"DEBUG: parse_movie_filename: Parsing filename: {filename}")
        match = re.match(r'^([^-]+(?:-[^-]+)*?)\((\d{4})\)\{tmdb-(\d+)\}\.mp4$', filename)
        if match:
            name = match.group(1).replace('-', ' ')
            year = match.group(2)
            tmdb_id = match.group(3)
            if DEBUG:
                print(f"DEBUG: parse_movie_filename: Parsed name={name}, year={year}, tmdb_id={tmdb_id}")
            return {
                'title': name,
                'year': year,
                'tmdb': tmdb_id,
                'filename': filename
            }
        if DEBUG:
            print("DEBUG: parse_movie_filename: No match found")
        return None

    def fetch_tmdb_data(self, tmdb_id):
        try:
            if DEBUG:
                print(f"DEBUG: fetch_tmdb_data: Fetching TMDB data for tmdb_id={tmdb_id}")
            url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={self.tmdb_api_key}"
            response = requests.get(url)
            if DEBUG:
                print(f"DEBUG: fetch_tmdb_data: TMDB response status={response.status_code}")
            if response.status_code != 200:
                return None

            movie_data = response.json()
            # if DEBUG:
            #     print(f"DEBUG: fetch_tmdb_data: movie_data={movie_data}")

            credits_url = f"https://api.themoviedb.org/3/movie/{tmdb_id}/credits?api_key={self.tmdb_api_key}"
            credits_response = requests.get(credits_url)
            director = ''
            if credits_response.status_code == 200:
                credits_data = credits_response.json()
                # if DEBUG:
                #     print(f"DEBUG: fetch_tmdb_data: credits_data={credits_data}")
                for crew_member in credits_data.get('crew', []):
                    if crew_member.get('job') == 'Director':
                        director = crew_member.get('name', '')
                        break

            return {
                'director': director,
                'imdb': movie_data.get('imdb_id', ''),
                'overview': movie_data.get('overview', ''),
                'tagline': movie_data.get('tagline', ''),
                'poster_path': movie_data.get('poster_path', '')
            }

        except Exception as e:
            print(f"Error fetching TMDB data for {tmdb_id}: {e}")
            if DEBUG:
                print(f"DEBUG: fetch_tmdb_data: Exception: {e}")
            return None

    def download_missing_posters(self, movies_data):
        posters_folder = os.path.join(self.project_folder, "posters")
        os.makedirs(posters_folder, exist_ok=True)
        base_url = "https://image.tmdb.org/t/p/original"

        for movie in movies_data:
            if not movie.get('poster_path'):
                if DEBUG:
                    print(f"DEBUG: download_missing_posters: No poster_path for {movie.get('title')}")
                continue

            filename_base = movie['filename'][:-4]
            poster_path = None
            for ext in ['.jpg', '.jpeg', '.png']:
                potential_path = os.path.join(posters_folder, f"{filename_base}{ext}")
                if os.path.exists(potential_path):
                    poster_path = potential_path
                    if DEBUG:
                        print(f"DEBUG: download_missing_posters: Poster already exists: {potential_path}")
                    break

            if poster_path:
                continue

            try:
                poster_url = f"{base_url}{movie['poster_path']}"
                if DEBUG:
                    print(f"DEBUG: download_missing_posters: Downloading poster from {poster_url}")
                response = requests.get(poster_url)
                if response.status_code == 200:
                    poster_filename = os.path.join(posters_folder, f"{filename_base}.jpg")
                    with open(poster_filename, 'wb') as f:
                        f.write(response.content)
                    self.progress.emit(f"Downloaded poster: {movie['title']}")
                    if DEBUG:
                        print(f"DEBUG: download_missing_posters: Poster downloaded: {poster_filename}")
                else:
                    if DEBUG:
                        print(f"DEBUG: download_missing_posters: Failed to download poster, status={response.status_code}")
            except Exception as e:
                print(f"Error downloading poster for {movie['title']}: {e}")
                if DEBUG:
                    print(f"DEBUG: download_missing_posters: Exception: {e}")

    def download_missing_subtitles(self, movies_data):
        subtitles_folder = os.path.join(self.project_folder, "subtitles")
        os.makedirs(subtitles_folder, exist_ok=True)

        for movie in movies_data:
            if not movie.get('imdb'):
                if DEBUG:
                    print(f"DEBUG: download_missing_subtitles: No imdb for {movie.get('title')}")
                continue

            filename_base = movie['filename'][:-4]
            subtitle_path = os.path.join(subtitles_folder, f"{filename_base}.srt")
            if os.path.exists(subtitle_path):
                if DEBUG:
                    print(f"DEBUG: download_missing_subtitles: Subtitle already exists: {subtitle_path}")
                continue

            try:
                imdb_num = movie['imdb'].replace('tt', '')
                url = f'https://api.opensubtitles.com/api/v1/subtitles?imdb_id={imdb_num}&languages=en'
                headers = {
                    'Api-Key': self.opensubtitles_api_key,
                    'User-Agent': 'PlayableCinema/1.0'
                }
                if DEBUG:
                    print(f"DEBUG: download_missing_subtitles: Requesting subtitles from {url}")
                response = requests.get(url, headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    if DEBUG:
                        print(f"DEBUG: download_missing_subtitles: Response data: {data}")

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
                        if DEBUG:
                            print(f"DEBUG: download_missing_subtitles: Downloading subtitle file_id={subtitle_file_id}")
                        download_response = requests.post(download_url, headers=headers, json={'file_id': subtitle_file_id})

                        if download_response.status_code == 200:
                            download_data = download_response.json()
                            subtitle_url = download_data['link']
                            if DEBUG:
                                print(f"DEBUG: download_missing_subtitles: Downloading subtitle from {subtitle_url}")
                            subtitle_file = requests.get(subtitle_url)

                            if subtitle_file.status_code == 200:
                                with open(subtitle_path, 'wb') as f:
                                    f.write(subtitle_file.content)
                                self.progress.emit(f"Downloaded subtitle: {movie['title']} (rating: {highest_ratings})")
                                if DEBUG:
                                    print(f"DEBUG: download_missing_subtitles: Subtitle downloaded: {subtitle_path}")
                            else:
                                if DEBUG:
                                    print(f"DEBUG: download_missing_subtitles: Failed to download subtitle file, status={subtitle_file.status_code}")
                        else:
                            if DEBUG:
                                print(f"DEBUG: download_missing_subtitles: Failed to get download link, status={download_response.status_code}")
                else:
                    if DEBUG:
                        print(f"DEBUG: download_missing_subtitles: Failed to get subtitles, status={response.status_code}")

                time.sleep(1)

            except Exception as e:
                print(f"Error downloading subtitle for {movie['title']}: {e}")
                if DEBUG:
                    print(f"DEBUG: download_missing_subtitles: Exception: {e}")

    def get_video_duration(self, video_path):
        try:
            if DEBUG:
                print(f"DEBUG: get_video_duration: Getting duration for {video_path}")
            cmd = [
                'ffprobe',
                '-v', 'quiet',
                '-print_format', 'json',
                '-show_format',
                video_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if DEBUG:
                print(f"DEBUG: get_video_duration: ffprobe returncode={result.returncode}")
            if result.returncode == 0:
                data = json.loads(result.stdout)
                duration_seconds = float(data['format']['duration'])
                duration_minutes = int(duration_seconds / 60)
                if DEBUG:
                    print(f"DEBUG: get_video_duration: duration_seconds={duration_seconds}, duration_minutes={duration_minutes}")
                return duration_minutes
            else:
                print(f"ffprobe failed for {video_path}: {result.stderr}")
                if DEBUG:
                    print(f"DEBUG: get_video_duration: ffprobe failed: {result.stderr}")
                return None

        except subprocess.TimeoutExpired:
            print(f"ffprobe timeout for {video_path}")
            if DEBUG:
                print(f"DEBUG: get_video_duration: ffprobe timeout")
            return None
        except FileNotFoundError:
            print("ffprobe not found. Please install FFmpeg.")
            if DEBUG:
                print("DEBUG: get_video_duration: ffprobe not found")
            return None
        except Exception as e:
            print(f"Error getting duration for {video_path}: {e}")
            if DEBUG:
                print(f"DEBUG: get_video_duration: Exception: {e}")
            return None

    def create_basic_video_metadata(self, filename):
        title = os.path.splitext(filename)[0]
        title = title.replace('-', ' ').replace('_', ' ')
        if DEBUG:
            print(f"DEBUG: create_basic_video_metadata: filename={filename}, title={title}")
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
        metadata_folder = os.path.join(self.project_folder, "metadata")
        os.makedirs(metadata_folder, exist_ok=True)
        csv_path = os.path.join(metadata_folder, self.metadata_filename)
        fieldnames = ['title', 'year', 'director', 'tmdb', 'imdb', 'filename', 'duration', 'overview', 'tagline']
        sorted_videos = sorted(videos_data, key=lambda video: video.get('title', '').lower())
        if DEBUG:
            print(f"DEBUG: write_metadata_csv: Writing {len(sorted_videos)} entries to {csv_path}")

        with open(csv_path, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            for video in sorted_videos:
                row = {field: html_encode_text(video.get(field, '')) for field in fieldnames}
                if DEBUG:
                    print(f"DEBUG: write_metadata_csv: Writing row: {row}")
                writer.writerow(row)

    def generate_missing_thumbnails(self, videos_data):
        thumbnails_folder = os.path.join(self.project_folder, "thumbnails")
        os.makedirs(thumbnails_folder, exist_ok=True)

        for video in videos_data:
            filename = video.get('filename', '')
            if not filename:
                if DEBUG:
                    print("DEBUG: generate_missing_thumbnails: No filename in video data")
                continue

            filename_base = filename[:-4] if filename.endswith('.mp4') else filename
            thumbnail_path = os.path.join(thumbnails_folder, f"{filename_base}.jpg")

            if os.path.exists(thumbnail_path):
                if DEBUG:
                    print(f"DEBUG: generate_missing_thumbnails: Thumbnail already exists: {thumbnail_path}")
                continue

            try:
                video_path = os.path.join(self.project_folder, self.data_folder, filename)
                if not os.path.exists(video_path):
                    if DEBUG:
                        print(f"DEBUG: generate_missing_thumbnails: Video file does not exist: {video_path}")
                    continue

                cmd = [
                    'ffmpeg',
                    '-i', video_path,
                    '-ss', '00:00:01',
                    '-vframes', '1',
                    '-q:v', '2',
                    '-y',
                    thumbnail_path
                ]
                if DEBUG:
                    print(f"DEBUG: generate_missing_thumbnails: Running ffmpeg: {' '.join(cmd)}")
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

                if result.returncode == 0:
                    self.progress.emit(f"Generated thumbnail: {video['title']}")
                    if DEBUG:
                        print(f"DEBUG: generate_missing_thumbnails: Thumbnail generated: {thumbnail_path}")
                else:
                    if DEBUG:
                        print(f"DEBUG: generate_missing_thumbnails: ffmpeg failed: {result.stderr}")
                    cmd_no_seek = [
                        'ffmpeg',
                        '-i', video_path,
                        '-vframes', '1',
                        '-q:v', '2',
                        '-y',
                        thumbnail_path
                    ]
                    if DEBUG:
                        print(f"DEBUG: generate_missing_thumbnails: Trying ffmpeg without seek: {' '.join(cmd_no_seek)}")
                    result_no_seek = subprocess.run(cmd_no_seek, capture_output=True, text=True, timeout=60)
                    if result_no_seek.returncode == 0:
                        self.progress.emit(f"Generated thumbnail: {video['title']}")
                        if DEBUG:
                            print(f"DEBUG: generate_missing_thumbnails: Thumbnail generated (no seek): {thumbnail_path}")
                    else:
                        if DEBUG:
                            print(f"DEBUG: generate_missing_thumbnails: ffmpeg failed (no seek): {result_no_seek.stderr}")

            except subprocess.TimeoutExpired:
                if DEBUG:
                    print(f"DEBUG: generate_missing_thumbnails: ffmpeg timeout for {filename}")
            except FileNotFoundError:
                if DEBUG:
                    print("DEBUG: generate_missing_thumbnails: ffmpeg not found. Please install FFmpeg.")
                self.progress.emit("FFmpeg not found - cannot generate thumbnails")
                break
            except Exception as e:
                if DEBUG:
                    print(f"DEBUG: generate_missing_thumbnails: Exception: {e}")

# Example function to read and decode CSV data
def read_metadata_csv(csv_path):
    """Read metadata CSV and decode HTML entities"""
    with open(csv_path, 'r', encoding='utf-8') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            # Decode HTML entities back to Unicode
            for field in row:
                row[field] = html_decode_text(row[field])
            yield row

