# Cinemathèque
This is a list of all of the westerns we will be using in this project. We will collect all the available basic metadata (name, year, tmdb, director, actors, description, etc) and place it into a database called `cinematheque.csv`. We can then load all of this movie data at startup and call on films at will from this “cinemathèque”.

## Activate/Deactivate Pyenv
```
$ pyenv activate playable-cinematheque
(playable-cinemathque) $ pyenv deactivate
```

## Create Pyenv
This is how to create the appropriate `pyenv`:
```
$ cd ./code/playable-cinematheque
$ pyenv virtualenv 3.11.9 playable-cinematheque
```

## Requirements
This will use the `requirements.txt` file to install all the required libraries.
```
pip install -r requirements.txt
```