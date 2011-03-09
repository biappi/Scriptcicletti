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
from getopt import getopt

try:
   from pyinotify import WatchManager, Notifier, ThreadedNotifier, EventsCodes, ProcessEvent, IN_CREATE, IN_MOVED_TO, IN_CLOSE_WRITE, IN_DELETE
except:
   pass

from SocketServer import ThreadingTCPServer
from SimpleHTTPServer import SimpleHTTPRequestHandler
from StringIO import StringIO

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
      self.recents = []
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
      abspathitem = "%s/%s" % (evt.path, evt.name)
      self.db.execute("delete from song where path = ?", abspathitem.decode(FS_ENCODING))
      if abspathitem in self.recents:
         self.recents.remove(abspathitem)

   def process_event(self, evt):
      abspathitem = "%s/%s" % (evt.path, evt.name)
      if os.isdir(abspathitem):
         start_scan(abspathitem, self.db, True)
      else:
         if abspathitem in self.recents:
            return
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
            if not self.db.execute("select id from artist where name = ?", (artist,)).fetchone():
               self.db.execute("insert into artist(name) values(?)", (artist,))
            self.recentartists[artist] = self.db.execute("select id from artist where name = ?", (artist,)).fetchone()[0]
 
         if not album in self.recentalbums.keys():
            if not self.db.execute("select id from album where titleclean = ?", (album,)).fetchone():
               self.db.execute("insert into album(title, titleclean) values(?, ?)", (album, albumclean))
            self.recentalbums[album] = self.db.execute("select id from album where titleclean = ?", (albumclean,)).fetchone()[0]
 
         if not genre in self.recentgenres.keys():
            if not self.db.execute("select id from genre where desc = ?", (genre,)).fetchone():
               self.db.execute("insert into genre(desc) values(?)", (genre,))
            self.recentgenres[genre] = self.db.execute("select id from genre where desc = ?", (genre,)).fetchone()[0]
         
         self.db.execute("insert into song(title, titleclean, artist_id, genre_id, album_id, path) values (?,?,?,?,?,?)", (title, titleclean, self.recentartists[artist], self.recentgenres[genre], self.recentalbums[album], abspathitem.decode(FS_ENCODING)))

         if len(self.recents) >= 20:
            self.recents.pop(0)
         self.recents.append(abspathitem)

         if len(self.recentalbums) > 20:
            self.recentalbums.clear()
         if len(self.recentgenres) > 20:
            self.recentgenres.clear()
         if len(self.recentsongs) > 20:
            self.recentsongs.clear()
         if len(self.recentartists) > 20:
            self.recentartists.clear()



class CatalogHTTPRequestHandler(SimpleHTTPRequestHandler):

   #TODO: return html/m3u files instead of queries

   def do_GET(self):
      text = self.path_to_query()
      if (text):
         mem = StringIO(text)
         self.send_response(200)
         self.end_headers()
         self.copyfile(mem, self.wfile)
      else:
         self.send_response(500)
         self.end_headers()

   def path_to_query(self):
      """ query:
         /browse/ -- browse catalog like a listdir
         /search/ -- do a free text research
         /smart/ -- perform a smart playlist
      """

      items = self.path.split("/")
      if len(items) < 2 or items == ['', '']:
         return None

      items = items[1:]
      if not items[-1]:
         items = items[:-1]

      if items[0] == "browse":
         where = []
         args = ()
         if len(items) % 2 == 0:
            query = "select * from %s;" % items[-1]
         else:
            for i in range(1, len(items), 2):
               where.append("%s = ?" % items[i])
               args = args + (items[i+1],)

            query = "select s.* from song s left join genre g on (s.id = g.id) left join artist a on (a.id = ) %s;" % (where and "where %s" % " and ".join(where) or "")
      elif items[0] == "search":
         pass
      elif items[0] == "smart":
         pass

      return query


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

   ThreadingTCPServer(("localhost", 8080), CatalogHTTPRequestHandler).serve_forever()
   

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

def perror(reason):
   print("error %s\ntry -h option for usage" % reason)

if __name__ == "__main__":

   opts, args = getopt(sys.argv[1:], "hsdn", ["help", "scan", "daemonize", "no-recursive"])

   if not opts or not args:
      print_usage(sys.argv[0])
   elif len(args) > 1:
      perror("too much directories")
      sys.exit(1)
   else:
      recursive = True
      for opt in opts:
         if opt in ("-s", "--help"):
            print_usage(sys.argv[0])
         elif opt in ("-s", "--scan"):
            scan = True
         elif opt in ("-h", "--daemonize"):
            daemon = True
         elif opt in ("-n", "--no-recursive"):
            recursive = False
         else:
            perror("reading from command line: %s" % sys.argv[0])
            sys.exit(1)
      for arg in args:
         if not os.path.isdir(arg):
            perror("%s is not a valid directory." % arg)
            sys.exit(1)
         elif arg[-1:] == "/":
            patharg = arg[:-1]
            break

      dbpath = "%s/.%s.sqlite" % (patharg, os.path.basename(patharg))
      prepare = not os.path.exists(dbpath)
      db = dbapi.connect(dbpath)
      if prepare:
         for sql in DBSCHEMA:
            db.execute(sql)
      if scan:
         start_scan(patharg, db, recursive)
      if daemon:
         start_daemon(patharg, db)
