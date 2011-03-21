query = 'select artist.name, album.title, song.title from song, album, artist where song.album_id = album.id and song.artist_id = artist.id'

from pysqlite2 import dbapi2 as sqlite
import json

connection = sqlite.connect('netlabels.sqlite')
cursor = connection.cursor()
cursor.execute(query)

dict = {'aaData': [(row[0], row[1], row[2]) for row in cursor]};

print json.dumps(dict)