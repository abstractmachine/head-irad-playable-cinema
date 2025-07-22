import re

class Movie:
    def __init__(self, name, year, tmdb_id, filename):
        self.name = name
        self.year = year
        self.tmdb_id = tmdb_id
        self.filename = filename

    def __repr__(self):
        return f"Movie(name='{self.name}', year='{self.year}', tmdb_id='{self.tmdb_id}', filename='{self.filename}')"

movies = []

with open('movielist.txt', 'r') as f:
    for line in f:
        line = line.strip()
        match = re.match(r'^([^-]+(?:-[^-]+)*?)\((\d{4})\)\{tmdb-(\d+)\}', line)
        if match:
            name = match.group(1).replace('-', ' ')
            year = match.group(2)
            tmdb_id = match.group(3)
            filename = line
            movie = Movie(name, year, tmdb_id, filename)
            movies.append(movie)