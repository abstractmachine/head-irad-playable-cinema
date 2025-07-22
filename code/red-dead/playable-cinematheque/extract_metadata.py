import re

with open('movielist.txt', 'r') as f:
    for line in f:
        line = line.strip()
        match = re.match(r'^([^-]+(?:-[^-]+)*?)\((\d{4})\)\{tmdb-(\d+)\}', line)
        if match:
            name = match.group(1).replace('-', ' ')
            year = match.group(2)
            tmdb_id = match.group(3)
            print(f"Name: {name}, Year: {year}, TMDB: {tmdb_id}")
