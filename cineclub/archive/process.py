# file handling
# csv file creation
import csv
# time stuff
import datetime
# we need math stuff
import math
import os
# regex
import re
# command subprocess (for ffmpeg)
import subprocess
# for downloading images
import urllib.request
from gettext import translation

# access The Movie Database API
from tmdbv3api import Movie, Search, TMDb

# get access to The Movie Database
tmdb = TMDb()
tmdb.api_key = 'e9e71332f53844b1198a517973dadd69'
tmdb.language = 'en'
tmdb.debug = True

# create an empty text file to write to
text_file = open('output.md', 'w')
# write the header
text_file.write('# Cineclub\n\n')

# load text file
with open('input.md') as f:
    content = f.readlines()
    # go through each line
    for line in content:
        # extract the movie year from the line using parentheses
        year = re.search(r'\((.*?)\)', line).group(1)
        # extract the movie name before the parentheses
        name = line.split('(')[0]
        # remove ending space from the name
        name = name.strip()
        # look for this movie on TMDB
        search = Search()
        # search for the movie name using the year as a filter
        results = search.movies(name.strip())

        # find the release date
        print(year + '\t' + name)
        # if we have no results
        if not results:
            # print an error
            print('No results for ' + name)
            # move on to the next movie
        # in case of error try to print the first result
        try:
            # print the type of the first result
            result_type = type(results[0])
        except:
            print('No results for ' + name)
            # add title, year, TMDB link, IMDB link and wikipedia link to the text file
            text_file.write('- [' + name + ']() (' + year + ')\n')
            continue

        release_date = results[0].release_date
        # try to get the release date
        try:
            # get the release date
            release_date = datetime.datetime.strptime(release_date, '%Y-%m-%d')
            # get the year
            release_year = release_date.year
            # if the year is not the same as the one we are looking for
            if release_year != int(year):
                # remove the movie from the results
                results = [movie for movie in results if movie.release_date != year]
        # if we can't get the release date
        except:
            # print an error
            print('Error getting release date for ' + name)
            # move on to the next movie
            continue

        # make sure there is a release date
        results = [movie for movie in results if movie.release_date]

        # if we have results
        if results:
            # get the first result
            movie = results[0]
            # get the movie details
            details = Movie().details(movie['id'])
            # get TMDB id
            tmdb_id = details.id
            # create a link to the movie on TMDB
            tmdb_link = 'https://www.themoviedb.org/movie/' + str(tmdb_id)
            # get the IMDB id
            imdb_id = details.imdb_id
            # create a link to the movie on IMDB
            imdb_link = 'https://www.imdb.com/title/' + str(imdb_id)
            # get the wikipedia page
            wikipedia_page = details.homepage
            # create a link to the wikipedia page
            wikipedia_link = wikipedia_page
            # add title, year, TMDB link, IMDB link and wikipedia link to the text file
            text_file.write('- [' + name + '](' + tmdb_link + ') (' + year + ')\n')

        else:
            print('No results for ' + name)

# close the text file
text_file.close()
# print done
print('Done')