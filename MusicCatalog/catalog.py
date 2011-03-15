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
from getopt import getopt, gnu_getopt
import pylast
import threading
from Queue import Queue
import subprocess
import signal
from itertools import permutations
from parameters import *
from urllib import unquote

try:
   from pyinotify import WatchManager, Notifier, ThreadedNotifier, EventsCodes, ProcessEvent, IN_CREATE, IN_MOVED_TO, IN_CLOSE_WRITE, IN_DELETE, IN_ISDIR, IN_MOVED_FROM
except:
   pass

from SocketServer import ThreadingTCPServer
from SimpleHTTPServer import SimpleHTTPRequestHandler
from StringIO import StringIO

DBSCHEMA = ( """
PRAGMA foreign_keys = ON;
""",
"""create table if not exists db (
   version text default "0.3",
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
   genre_id references genre(id) not null default 1,
   album_id references album(id) not null default 1,
   artist_id references artist(id) not null default 1,
   trackno integer,
   puid text,
   bpm real,
   path text unique not null,
   length real default 0.0
);""",
"""create index songgenre on song(genre_id);
""",
"""create index songalbum on song(album_id);
""",
"""create index songartist on song(artist_id);
""",
"""create table if not exists genre (
   id integer primary key asc autoincrement,
   desc text not null,
   weight real not null default 1
);""",
"""create table if not exists tag (
   id integer primary key asc autoincrement,
   name text unique not null,
   nameclean text not null
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
""",
"""insert into genre (desc) values ('unknown') ;""",
"""insert into artist (name) values ('unknown') ;""",
"""insert into album (title, titleclean) values ('unknown', 'unknown') ;"""
)

DECODERS = (MP3, FLAC, MP4, MonkeysAudio, Musepack, WavPack, TrueAudio, OggVorbis, OggTheora, OggSpeex, OggFLAC)
FS_ENCODING = sys.getfilesystemencoding()

class SubtreeListener(ProcessEvent):
   
   def __init__(self, dbpath, md_queue, fd_queue, condition):
      self.db = dbapi.connect(dbpath)
      self.recents = []
      self.recentartists = {}
      self.recentalbums = {}
      self.recentgenres = {}
      self.recentsong = {}
      self.md_queue = md_queue
      self.fd_queue = fd_queue
      self.condition = condition
      self.cookies = {}
      ProcessEvent.__init__(self)

   def process_IN_CREATE(self, evt):
      self.process_event(evt)

   def process_IN_CLOSE_WRITE(self, evt):
      self.process_event(evt)

   def process_IN_MOVED_FROM(self, evt):
      self.cookies[evt.cookie] = "%s/%s" % (evt.path, evt.name)

   def process_IN_MOVED_TO(self, evt):
      oldpath = self.cookies[evt.cookie]
      newpath = "%s/%s" % (evt.path, evt.name)
      self.condition.acquire()
      self.db.execute("update song set path = ? where path = ?;", (oldpath.decode(FS_ENCODING), newpath.decode(FS_ENCODING)))
      self.db.commit()
      self.condition.release()

   def process_IN_ISDIR(self, evt):
      exists = self.db.execute("select id from song where path like = ? limit by 1;", ("%s%%" % evt.path,)).fetchone()
      if not exists:
         start_scan(evt.path, self.db, self.md_queue, self.fd_queue, self.condition, True)

   def process_IN_DELETE(self, evt):
      abspathitem = "%s/%s" % (evt.path, evt.name)
      if abspathitem == dbpath:
         return

      song_id = self.db.execute("select id from song where path = ?", (abspathitem.decode(FS_ENCODING),)).fetchone()[0]
      self.condition.acquire()
      self.db.execute("delete from song where id = ?", (song_id,))
      self.db.execute("delete from song_x_tag where song_id = ?", (song_id,))
      self.db.commit()
      self.condition.release()
      if abspathitem in self.recents:
         self.recents.remove(abspathitem)

   def process_event(self, evt):
      abspathitem = "%s/%s" % (evt.path, evt.name)
      if os.path.isdir(abspathitem):
         start_scan(abspathitem, self.db, self.md_queue, self.fd_queue, self.condition, True)
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
         title = "unknown"
         titleclean = "unknown"
         artist = "unknown"
         album = "unknown"
         albumclean = "unknown"
         genre = "unknown"
         length = 0.0

         try:
            title = " ".join(id3item['TIT2'].text).strip().lower()
            titleclean = re.sub("[^\w]*", "", title)
            artist = " ".join(id3item['TPE1'].text).strip().lower()
            album = " ".join(id3item['TALB'].text).strip().lower()
            albumclean = re.sub("[^\w]*", "", album)
            genre = " ".join(id3item['TCON'].text).strip().lower()
            length = float(id3item['TLEN'])
         except:
            pass

         self.condition.acquire()
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
         
         self.db.execute("insert or ignore into song(title, titleclean, artist_id, genre_id, album_id, path, length) values (?,?,?,?,?,?,?)", (title, titleclean, self.recentartists[artist], self.recentgenres[genre], self.recentalbums[album], abspathitem.decode(FS_ENCODING), length))
         self.db.commit()
         self.condition.release()

         self.md_queue.put((abspathitem, title, artist))
         self.fd_queue.put((abspathitem, title, artist))

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


class FiledataThread(threading.Thread):

   daemon = True

   def __init__(self, queue, condition, dbpath):
      threading.Thread.__init__(self)
      self.queue = queue
      self.condition = condition
      self.dbpath = dbpath

   def run(self):

      self.db = dbapi.connect(self.dbpath)
      while True:
         path, title, artist = self.queue.get()
         soxi_process = subprocess.Popen(["/usr/bin/soxi", "-D", path], stdout = subprocess.PIPE)
         soxi_process.wait()
         soxi_output = float(soxi_process.communicate()[0])
         sox_process = subprocess.Popen(["/usr/bin/sox", path, "-t", "wav", "/tmp/.stretch.wav", "trim", "0", "30"], stdout = None, stderr = None) # well done, dear sox friend. Well done.
         sox_process.wait()
         bpm_process = subprocess.Popen(["/usr/bin/soundstretch", "/tmp/.stretch.wav", "-bpm", "-quick", "-naa"], stdout = subprocess.PIPE, stderr = subprocess.STDOUT)
         bpm_process.wait()
         bpm_output = bpm_process.communicate()
   
         bpm_pattern = re.search ("Detected BPM rate ([0-9]+)", bpm_output[0], re.M)
         if bpm_pattern:
            bpm = float(bpm_pattern.groups()[0])
         else:
            bpm = 0.0

         self.condition.acquire()
         self.db.execute("update song set bpm = ?, length = ? where path = ?", (bpm, soxi_output, path.decode(FS_ENCODING)))
         self.db.commit()
         self.condition.release()

class MetadataThread(threading.Thread):
   
   daemon = True

   def __init__(self, queue, condition, dbpath):
      threading.Thread.__init__(self)
      self.daemon = True
      self.queue = queue
      self.condition = condition
      self.dbpath = dbpath
      self.lastfm = pylast.LastFMNetwork( username = USERNAME)
      self.lastfm.api_key = APIKEY

   def run(self):

      self.db = dbapi.connect(self.dbpath)
      while True:
         path, title, artist = self.queue.get()

         try:
            tags = self.lastfm.get_track(artist, title).get_top_tags()
         except:
            tags = []

         song_id = self.db.execute("select id from song where path = ?", (path.decode(FS_ENCODING), )).fetchone()[0]
         known_tags = self.db.execute("select distinct weight, name, nameclean from song_x_tag left join tag on (tag_id = id) where song_id = ?;", (song_id,)).fetchall()
         
         self.condition.acquire()
         for t in tags:
            alreadytag = False
            for kt in known_tags:
               if t.item.name == kt[1] and t.weight != kt[0]:
                  self.db.execute("update song_x_tag set weight = ? where song_id = ? and tag_id = ?;", (t.weight, song_id, t.item.name))
                  alreadytag = True
            if not alreadytag:
               cleantag = re.sub("[^a-zA-Z0-9 ]", "", t.item.name).strip().lower().split()
               tagcomb = permutations(cleantag, len(cleantag))
               nameclean = "".join(cleantag)
               for tc in tagcomb:
                  similartag = self.db.execute("select nameclean from tag where name like ?;", ("%%%s%%" % "%".join(tc),)).fetchone()
                  if similartag and len(similartag[0]) == len(nameclean):
                     nameclean = similartag[0]
               #print "item.name: ", t.item.name, "nameclean: ", nameclean
               self.db.execute("insert or ignore into tag (name, nameclean) values (?, ?);", (t.item.name, nameclean))
               self.db.commit()
               tagid = self.db.execute("select id from tag where name = ? ", (t.item.name, )).fetchone()[0]
               self.db.execute("insert into song_x_tag (song_id, tag_id, weight) values (?, ?, ?);", (song_id, tagid, t.weight))
         self.db.commit()
         self.condition.release()

class CatalogHTTPRequestHandler(SimpleHTTPRequestHandler):
   """
         a HTTPRequestHandler that perform queries on db
         the main idea is a conversion from URI to SQL:

         GET /<action>/[<subclause>[/<parameter>] ... ] => "SELECT FROM [joined tables] WHERE [clause from parameters]"

         query actions:
            /browse/ -- browse catalog like a listdir (e.g. /browse/genre/indie /browse/year/1979 /browse/genre/indie/year/1980)
            /search/ -- do a free text research (e.g. /search/blitzrieg%20bop)
            /smart/ -- perform a smart playlist (e.g. /smart/shine%20on%20you)
            /aggregate/ -- create a playlist based on a criteria (e.g. /aggregate/genre/indie,electro,indie-pop)
   """

   def do_GET(self):

      items = unquote(self.path).split("/")
      if len(items) < 2 or items == ['', '']:
         self.send_response(500)
         self.end_headers()
         return

      items = items[1:]
      if not items[-1]:
         items = items[:-1]

      db = dbapi.connect(self.server.dbpath)

      songquery = "select distinct s.id as id, s.title as title, a.name as artist, g.desc as genre, al.title as album, s.path as path from song s left join genre g on (s.genre_id = g.id) left join artist a on (s.artist_id = a.id) left join album al on (s.album_id = al.id)"
      results = ""

      try:
         if items[0] == "browse":
            where = []
            args = ()
            if len(items) % 2 == 0:
               query = "select * from %s;" % items[-1]
               results = self.html(db.execute(query).fetchall())
               self.send_response(200)
            else:
               for i in range(1, len(items), 2):
                  where.append("%s = ?" % items[i])
                  args = args + (items[i+1],)
               query = "%s %s order by s.title;" % (songquery, (where and "where %s" % " and ".join(where) or ""))
               results = self.m3u(db.execute(query, args).fetchall())
               self.send_response(200)

         elif items[0] == "search":
               query = "%s where g.desc like ? or a.name like ? or s.title like ? or al.title like ? order by s.title;" % songquery
               args = tuple( '%%%s%%' % " ".join(items[1:]) for i in (1,2,3,4) ) #  --> ('%%%s%%' % ... , '%%%s%%' % ... , '%%%s%%' % ... ) 

         elif items[0] == "smart":
            pass

         elif items[0] == "aggregate":
            pass

      except Exception as e:
         self.send_response(500)
         self.end_headers()
         results = e.__str__()

      data = StringIO()
      data.write(results)
      data.seek(0)

      self.end_headers()
      self.copyfile(data, self.wfile)


   def m3u(self, songtuple):

      extm3u = """#EXTM3U\n%s""" % "\n".join(["""#EXTINF:1,%s - %s\n%s\n""" % (i[2], i[1], i[5]) for i in songtuple])
      return extm3u 

   def html(self, songtuple):

      htmldoc = """<html>\n<body>\n<table>%s</table></body>\n</html>""" % "\n".join(["<tr><td>%s</td></tr>" % "</td><td>".join([str(j) for j in i]) for i in songtuple])
      return htmldoc



class CatalogThreadingTCPServer(ThreadingTCPServer):
   """a threaded tcp server interfaced with a database"""

   allow_reuse_address = True

   def __init__(self, server_address, RequestHandlerClass, dbpath, bind_and_activate=True):
      ThreadingTCPServer.__init__(self, server_address, RequestHandlerClass, bind_and_activate=True)
      self.dbpath = dbpath


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


def start_daemon(path, dbpath, md_queue, fd_queue, condition):
   """ installs a subtree listener and wait for events """
   #daemonize()

   wm_auto = WatchManager()
   subtreemask = IN_CLOSE_WRITE | IN_DELETE | IN_MOVED_TO | IN_CREATE | IN_ISDIR
   notifier_sb = ThreadedNotifier(wm_auto, SubtreeListener(dbpath, md_queue, fd_queue, condition))
   notifier_sb.start()

   wdd_sb = wm_auto.add_watch(path, subtreemask, rec=True)

   CatalogThreadingTCPServer(("localhost", 8080), CatalogHTTPRequestHandler, dbpath).serve_forever()
   

def start_scan(path, db, md_queue, fd_queue, condition, depth = 1):
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
            title = "unknown"
            titleclean = "unknown"
            artist = "unknown"
            album = "unknown"
            albumclean = "unknown"
            genre = "unknown"
            length = 0.0

            try:
               title = " ".join(id3item['TIT2'].text).strip().lower()
               titleclean = re.sub("[^\w]*", "", title)
               artist = " ".join(id3item['TPE1'].text).strip().lower()
               album = " ".join(id3item['TALB'].text).strip().lower()
               albumclean = re.sub("[^\w]*", "", album)
               genre = " ".join(id3item['TCON'].text).strip().lower()
               length = float(id3item['TLEN'])
            except:
               pass

            condition.acquire()
            if not artist in recentartists.keys():
               if not db.execute("select id from artist where name = ?", (artist,)).fetchone():
                  db.execute("insert into artist(name) values(?)", (artist,))
                  db.commit()
               recentartists[artist] = db.execute("select id from artist where name = ?", (artist,)).fetchone()[0]

            if not album in recentalbums.keys():
               if not db.execute("select id from album where titleclean = ?", (album,)).fetchone():
                  db.execute("insert into album(title, titleclean) values(?, ?)", (album, albumclean))
                  db.commit()
               recentalbums[album] = db.execute("select id from album where titleclean = ?", (albumclean,)).fetchone()[0]

            if not genre in recentgenres.keys():
               if not db.execute("select id from genre where desc = ?", (genre,)).fetchone():
                  db.execute("insert into genre(desc) values(?)", (genre,))
                  db.commit()
               recentgenres[genre] = db.execute("select id from genre where desc = ?", (genre,)).fetchone()[0]
            condition.release()

            condition.acquire()
            db.execute("insert or ignore into song(title, titleclean, artist_id, genre_id, album_id, path, length) values (?,?,?,?,?,?,?)", (title, titleclean, recentartists[artist], recentgenres[genre], recentalbums[album], abspathitem.decode(FS_ENCODING), length))
            md_queue.put((abspathitem, title, artist))
            fd_queue.put((abspathitem, title, artist))
            db.commit()
            condition.release()



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
\t-b --no-bpm\tdo not perform bpm detection (faster)
""" % argv)
   sys.exit(1)

def perror(reason):
   print("error %s\ntry -h option for usage" % reason)

def shutdown(signum, stack):
   pass
   

if __name__ == "__main__":

   opts, args = getopt(sys.argv[1:], "hsdnb", ["help", "scan", "daemonize", "no-recursive", "no-bpm"])

   if not opts or not args:
      print_usage(sys.argv[0])
   elif len(args) > 1:
      perror("too much directories")
      sys.exit(1)
   else:
      recursive = True
      scan = False
      daemon = False
      bpmdetect = True
      for opt in opts:
         if opt[0] in ("-h", "--help"):
            print_usage(sys.argv[0])
         elif opt[0] in ("-s", "--scan"):
            scan = True
         elif opt[0] in ("-d", "--daemonize"):
            daemon = True
         elif opt[0] in ("-n", "--no-recursive"):
            recursive = False
         elif opt[0] in ("-b", "--no-bpm"):
            bpmdetect = False
         else:
            perror("reading from command line: %s" % opt[0])
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
      metadataqueue = Queue()
      filedataqueue = Queue()
      condition = threading.Condition()

      MetadataThread(metadataqueue, condition, dbpath).start()
      if bpmdetect:
         FiledataThread(filedataqueue, condition, dbpath).start()

      if scan:
         start_scan(patharg, db, metadataqueue, filedataqueue, condition, recursive)
      if daemon:
         start_daemon(patharg, dbpath, metadataqueue, filedataqueue, condition)
