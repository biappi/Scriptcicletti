#!/usr/bin/env python2.6

import sqlite3 as dbapi
import sys
import os
from mutagen.flac import FLAC
from mutagen.mp3 import MP3, HeaderNotFoundError
from mutagen.monkeysaudio import MonkeysAudio
from mutagen.mp4 import MP4
from mutagen.musepack import Musepack
from mutagen.oggflac import OggFLAC
from mutagen.oggspeex import OggSpeex
from mutagen.oggtheora import OggTheora
from mutagen.oggvorbis import OggVorbis
from mutagen.optimfrog import OptimFROG
from mutagen.trueaudio import TrueAudio
from mutagen.wavpack import WavPack
from collections import defaultdict
import re

from pyinotify import WatchManager, Notifier, ThreadedNotifier, EventsCodes, ProcessEvent, IN_CREATE, IN_MOVED_TO, IN_CLOSE_WRITE, IN_DELETE

DBSCHEMA = ( """
PRAGMA foreign_keys = ON;
""",
"""create table if not exists db (
   version text default "0.1",
   tree text not null
);""",
"""create table if not exists artist (
   id integer primary key asc autoincrement,
   name text not null
); """,
"""create table if not exists song (
   id integer primary key asc autoincrement,
   title text not null,
   titleclean text not null,
   genre_id references genre(id),
   album_id references album(id),
   artist_id references artist(id),
   trackno integer,
   puid text,
   bpm real,
   path text not null
);""",
"""create index songgenre on song(genre_id);
""",
"""create index songalbum on song(album_id);
""",
"""create index songartist on song(artist_id);
""",
"""create table if not exists genre (
   id integer primary key asc autoincrement,
   desc text not null
);""",
"""create table if not exists tag (
   id integer primary key asc autoincrement,
   name text not null
);
""",
"""create table if not exists song_x_tag (
   song_id references song(id),
   tag_id references tag(id),
   weight real not null
);
""",
"""create index songtagsong on song_x_tag(song_id);
""",
"""create index songtagtag on song_x_tag(tag_id);
""",
"""create table if not exists album (
   id integer primary key asc autoincrement,
   title text not null,
   titleclean text not null,
   date integer
);
""")

DECODERS = (MP3, FLAC, MP4, MonkeysAudio, Musepack, WavPack, TrueAudio, OggVorbis, OggTheora, OggSpeex, OggFLAC)
FS_ENCODING = sys.getfilesystemencoding()

class SubtreeListener(ProcessEvent):
   
   def __init__(self, db):
      self.db = db
      self.recentartists = {}
      self.recentalbums = {}
      self.recentgenres = {}
      self.recentsong = {}
      ProcessEvent.__init__(self)

   def process_IN_CREATE(self, evt):
      process_event(evt)

   def process_IN_MOVED_TO(self, evt):
      process_event(evt)

   def process_IN_CLOSE_WRITE(self):
      process_event(evt)

   def process_IN_DELETE(self):
      pass

   def process_event(self, evt):
      abspathitem = "%s/%s" % (evt.path, evt.name)
      if os.isdir(abspathitem):
         start_scan(abspathitem)
      else:
         id3item = None
         for decoder in DECODERS:
            try:
               id3item = decoder(abspathitem)
               break
            except: 
               pass
         if not id3item:
            return
         title = " ".join(id3item['TIT2'].text).strip().lower()
         titleclean = re.sub("[^\w]*", "", title)
         artist = " ".join(id3item['TPE1'].text).strip().lower()
         album = " ".join(id3item['TALB'].text).strip().lower()
         albumclean = re.sub("[^\w]*", "", album)
         genre = " ".join(id3item['TCON'].text).strip().lower()
 
         if not artist in self.recentartists.keys():
            if not db.execute("select id from artist where name = ?", (artist,)).fetchone():
               db.execute("insert into artist(name) values(?)", (artist,))
            self.recentartists[artist] = db.execute("select id from artist where name = ?", (artist,)).fetchone()[0]
 
         if not album in self.recentalbums.keys():
            if not db.execute("select id from album where titleclean = ?", (album,)).fetchone():
               db.execute("insert into album(title, titleclean) values(?, ?)", (album, albumclean))
            self.recentalbums[album] = db.execute("select id from album where titleclean = ?", (albumclean,)).fetchone()[0]
 
         if not genre in self.recentgenres.keys():
            if not db.execute("select id from genre where desc = ?", (genre,)).fetchone():
               db.execute("insert into genre(desc) values(?)", (genre,))
            self.recentgenres[genre] = db.execute("select id from genre where desc = ?", (genre,)).fetchone()[0]
         
         db.execute("insert into song(title, titleclean, artist_id, genre_id, album_id, path) values (?,?,?,?,?,?)", (title, titleclean, self.recentartists[artist], self.recentgenres[genre], self.recentalbums[album], abspathitem.decode(FS_ENCODING)))

         if len(self.recentalbums) > 20:
            self.recentalbums.clear()
         if len(self.recentgenres) > 20:
            self.recentgenres.clear()
         if len(self.recentsongs) > 20:
            self.recentsongs.clear()
         if len(self.recentartists) > 20:
            self.recentartists.clear()


def daemonize (stdin='/dev/null', stdout='/dev/null', stderr='/dev/null'):

   try: 
      pid = os.fork() 
      if pid > 0:
          sys.exit(0)   # Exit first parent.
   except OSError, e: 
      sys.stderr.write ("fork #1 failed: (%d) %s\n" % (e.errno, e.strerror) )
      sys.exit(1)

   os.chdir("/") 
   os.umask(0) 
   os.setsid() 

   try: 
      pid = os.fork() 
      if pid > 0:
          sys.exit(0)   # Exit second parent.
   except OSError, e: 
      sys.stderr.write ("fork #2 failed: (%d) %s\n" % (e.errno, e.strerror) )
      sys.exit(1)

   
   # Redirect standard file descriptors.
   si = open(stdin, 'r')
   so = open(stdout, 'a+')
   se = open(stderr, 'a+', 0)
   os.dup2(si.fileno(), sys.stdin.fileno())
   os.dup2(so.fileno(), sys.stdout.fileno())
   os.dup2(se.fileno(), sys.stderr.fileno())


def start_daemon(path, db):
   """ installs a subtree listener and wait for events """
   daemonize()

   wm_auto = WatchManager()
   subtreemask = IN_CLOSE_WRITE | IN_DELETE | IN_MOVED_TO | IN_CREATE
   notifier_sb = ThreadedNotifier(wm_auto, SubtreeListener(db))
   notifier_sb.start()

   wdd_sb = wm_auto.add_watch(path, subdirmask, rec=True)
   

def start_scan(path, db, depth = 1):
   """ Breadth scan a subtree """

   scanpath = [path,]

   while len(scanpath):
      curdir = scanpath.pop()
      recentartists = {}
      recentsong = {}
      recentalbums = {}
      recentgenres = {}

      for item in os.listdir(curdir):
         abspathitem = "%s/%s" % (curdir, item)
         if os.path.isdir(abspathitem) and depth:
            scanpath.append(abspathitem)
         else:
            id3item = None
            for decoder in DECODERS:
               try:
                  id3item = decoder(abspathitem)
                  break
               except: 
                  pass
            if not id3item:
               continue
            title = " ".join(id3item['TIT2'].text).strip().lower()
            titleclean = re.sub("[^\w]*", "", title)
            artist = " ".join(id3item['TPE1'].text).strip().lower()
            album = " ".join(id3item['TALB'].text).strip().lower()
            albumclean = re.sub("[^\w]*", "", album)
            genre = " ".join(id3item['TCON'].text).strip().lower()

            if not artist in recentartists.keys():
               if not db.execute("select id from artist where name = ?", (artist,)).fetchone():
                  db.execute("insert into artist(name) values(?)", (artist,))
               recentartists[artist] = db.execute("select id from artist where name = ?", (artist,)).fetchone()[0]

            if not album in recentalbums.keys():
               if not db.execute("select id from album where titleclean = ?", (album,)).fetchone():
                  db.execute("insert into album(title, titleclean) values(?, ?)", (album, albumclean))
               recentalbums[album] = db.execute("select id from album where titleclean = ?", (albumclean,)).fetchone()[0]

            if not genre in recentgenres.keys():
               if not db.execute("select id from genre where desc = ?", (genre,)).fetchone():
                  db.execute("insert into genre(desc) values(?)", (genre,))
               recentgenres[genre] = db.execute("select id from genre where desc = ?", (genre,)).fetchone()[0]
            
            db.execute("insert into song(title, titleclean, artist_id, genre_id, album_id, path) values (?,?,?,?,?,?)", (title, titleclean, recentartists[artist], recentgenres[genre], recentalbums[album], abspathitem.decode(FS_ENCODING)))
      db.commit()



def levenshtein(a,b): # Dr. levenshtein, i presume.
   "Calculates the Levenshtein distance between a and b."
   n, m = len(a), len(b)
   if n > m:
      # Make sure n <= m, to use O(min(n,m)) space
      a,b = b,a
      n,m = m,n

   current = range(n+1)
   for i in range(1,m+1):
      previous, current = current, [i]+[0]*n
      for j in range(1,n+1):
         add, delete = previous[j]+1, current[j-1]+1
         change = previous[j-1]
         if a[j-1] != b[i-1]:
            change = change + 1
         current[j] = min(add, delete, change)

   return current[n]


def print_usage(argv):
   print("""
usage: %s [-h|--help] [-s|--scan] [-d|--daemonize] [-n|--no-recursive] path\n
\t-h --help\tprint this help
\t-s --scan\tscan path and prepare db
\t-d --daemonize\tstart daemon on path
\t-n --no-recursive\tdo not scan subfolders
""" % argv)
   sys.exit(1)

if __name__ == "__main__":

   args = []
   args.extend(sys.argv[1:])

   if not args or "--help" in args or "-h" in args:
      print_usage(sys.argv[0])

   patharg = args.pop()

   for i in args:
      if not i.strip('-') in ["s", "scan", "d", "daemonize", "n", "no-recursive"]:
         print("error: %s not a valid option" % i)
         sys.exit(0)

   if not os.path.isdir(patharg):
      print("error: %s not a valid directory" % patharg)
      sys.exit(0)
   else:
      if patharg[-1:] == "/":
         patharg = patharg[:-1]
      dbpath = "%s/.%s.sqlite" % (patharg, os.path.basename(patharg))

   db = None
   prepare = not os.path.exists(dbpath)
   depth = not "--no-recursive" in args and not "-n" in args

   if "--scan" in args or "-s" in args:
      db = dbapi.connect(dbpath)
      if prepare:
         for sql in DBSCHEMA:
            db.execute(sql)
      start_scan(patharg, db, depth)

   if len(args) == 1 or "--daemonize" in args or "-d" in args:
      if not db:
         db = dbapi.connect(dbpath)
         if prepare:
            for sql in DBSCHEMA:
               db.execute(sql)
      start_daemon(patharg, db)
