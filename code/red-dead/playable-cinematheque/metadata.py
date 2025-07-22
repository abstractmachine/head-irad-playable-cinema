import re
import requests
import csv
import os

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
        writer.writerow(row)


if download_posters:
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