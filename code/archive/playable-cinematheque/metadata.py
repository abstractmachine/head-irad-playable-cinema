import re
import requests
import csv
import os
import time

download_posters = False  # Set to False if you don't want to download posters

class Movie:
    def __init__(self, name, year, date, tmdb_id, filename):
        self.name = name
        self.year = year
        self.date = date
        self.tmdb_id = tmdb_id
        self.filename = filename
        self.tmdb_data = None

    def __repr__(self):
        return f"Movie(name='{self.name}', year='{self.year}', tmdb_id='{self.tmdb_id}', filename='{self.filename}')"

# Load API key
with open('api_key.txt', 'r') as key_file:
    api_key = key_file.read().strip()

movies = []

print("Reading movielist.txt")

with open('movielist.txt', 'r') as f:
    for line in f:
        line = line.strip()
        match = re.match(r'^([^-]+(?:-[^-]+)*?)\((\d{4})\)\{tmdb-(\d+)\}', line)
        if match:
            name = match.group(1).replace('-', ' ')
            date = match.group(2)
            # format of date is YYYY-MM-DD, but we only need the year
            year = date.split('-')[0] if '-' in date else date
            tmdb_id = match.group(3)
            filename = line
            movie = Movie(name, year, date, tmdb_id, filename)
            # Fetch TMDB data
            url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={api_key}"
            response = requests.get(url)
            if response.status_code == 200:
                movie.tmdb_data = response.json()
            else:
                movie.tmdb_data = None
            movies.append(movie)

print("Fetched TMDB metadata for", len(movies), "movies from movielist.txt")

# Fetch director from credits endpoint
for movie in movies:
    director = ''
    if movie.tmdb_id:
        credits_url = f"https://api.themoviedb.org/3/movie/{movie.tmdb_id}/credits?api_key={api_key}"
        credits_response = requests.get(credits_url)
        if credits_response.status_code == 200:
            credits_data = credits_response.json()
            for crew_member in credits_data.get('crew', []):
                if crew_member.get('job') == 'Director':
                    director = crew_member.get('name', '')
                    break
    movie.director = director

# Only keep these fields
fieldnames = ['name', 'year', 'director', 'tmdb_id', 'imdb_id', 'filename', 'overview', 'tagline']

print("Writing metadata to CSV")

with open('metadata.csv', 'w', newline='', encoding='utf-8') as csvfile:
    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
    writer.writeheader()
    for movie in movies:
        row = {
            'name': movie.name,
            'year': movie.year,
            'director': getattr(movie, 'director', ''),
            'tmdb_id': movie.tmdb_id,
            'imdb_id': movie.tmdb_data.get('imdb_id', '') if movie.tmdb_data else '',
            'filename': movie.filename,
            'overview': movie.tmdb_data.get('overview', '') if movie.tmdb_data else '',
            'tagline': movie.tmdb_data.get('tagline', '') if movie.tmdb_data else ''
        }
        # print(f"{movie.name}")
        writer.writerow(row)


if download_posters:

    print("Downloading posters")
    # Create posters directory if it doesn't exist
    posters_dir = 'posters'
    if not os.path.exists(posters_dir):
        os.makedirs(posters_dir)

    base_url = "https://image.tmdb.org/t/p/original"

    for movie in movies:
        if movie.tmdb_data and movie.tmdb_data.get('poster_path'):
            poster_path = movie.tmdb_data['poster_path']
            poster_url = f"{base_url}{poster_path}"
            poster_filename = os.path.join(posters_dir, f"{movie.filename}.jpg")
            if not os.path.exists(poster_filename):
                response = requests.get(poster_url)
                if response.status_code == 200:
                    with open(poster_filename, 'wb') as img_file:
                        img_file.write(response.content)

# Load OpenSubtitles API key
with open('api_subtitle.txt', 'r') as sub_key_file:
    opensubtitles_api_key = sub_key_file.read().strip()

subtitles_dir = 'subtitles'
if not os.path.exists(subtitles_dir):
    os.makedirs(subtitles_dir)

print("Downloading subtitles")

for movie in movies:
    imdb_id = movie.tmdb_data.get('imdb_id', '') if movie.tmdb_data else ''
    if imdb_id:
        # check to see if the subtitle file already exists
        movie_filename = os.path.splitext(movie.filename)[0]
        subtitle_filename = os.path.join(subtitles_dir, f"{movie_filename}.srt")
        if os.path.exists(subtitle_filename):
            print(f"Subtitle file already exists for {movie.name}")
            continue
        # Fetch subtitles from OpenSubtitles API
        imdb_num = imdb_id.replace('tt', '')
        url = f'https://api.opensubtitles.com/api/v1/subtitles?imdb_id={imdb_num}&languages=en'
        headers = {
            'Api-Key': opensubtitles_api_key,
            'User-Agent': 'PlayableCinema/1.0'
        }
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            # print(f"Found {len(data.get('data', []))} subtitles for {movie.name}")
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
                    print(f"Downloading subtitles for {movie.name} to {subtitle_filename}")
                    subtitle_file = requests.get(subtitle_url)
                    if subtitle_file.status_code == 200:
                        with open(subtitle_filename, 'wb') as f:
                            f.write(subtitle_file.content)
                    else:
                        print(f"Failed to download subtitle file for {movie.name}: {subtitle_file.status_code} - {subtitle_file.text}")
                else:
                    print(f"Failed to get download link for {movie.name}: {download_response.status_code} - {download_response.text}")
            else:
                print(f"No downloadable subtitle files found for {movie.name}")
        else:
            print(f"Failed to fetch subtitles for {movie.name}: {response.status_code} - {response.text}")
        time.sleep(1)  # Be polite to the API