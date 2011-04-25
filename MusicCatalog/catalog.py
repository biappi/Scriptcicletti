#!/usr/bin/env python2.6
#
# A music library managaer and smart aggregator
# featuring http-based playlist request and automatic generation
#
# requirements: pysqlite, pylast, mutagen, pyinotify
# optional requirements: sox, soundstretch

__version__ = '0.5'
__author__ = 'radiocicletta <radiocicletta@gmail.com>'

from parameters import * # a file parameters.py containing last.fm variables USERNAME and APIKEY
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
from ID3 import ID3
from collections import defaultdict
import re
from getopt import getopt, gnu_getopt
import pylast
import threading
from Queue import Queue, Empty
import subprocess
import signal
from itertools import permutations
from urllib import unquote
import traceback
from time import sleep
import logging
from collections import deque

try: #TODO: add FSEvents support
   from pyinotify import WatchManager, Notifier, ThreadedNotifier, EventsCodes, ProcessEvent, ExcludeFilter, IN_CREATE, IN_MOVED_TO, IN_CLOSE_WRITE, IN_DELETE, IN_ISDIR, IN_MOVED_FROM
except:
   pass

from SocketServer import ThreadingTCPServer, ForkingTCPServer
from SimpleHTTPServer import SimpleHTTPRequestHandler
from StringIO import StringIO

DBSCHEMA = ( """
PRAGMA foreign_keys = ON;
""",
"""create table if not exists db (
   version text default "0.5",
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
   descclean text not null,
   weight real not null default 1,
   bpm real,
   bpmavg real
);""",
"""create table if not exists genre_x_genre (
   id_genre integer not null,
   id_related_genre integer not null,
   similarity real not null default 0.0
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
"""insert into genre (desc, descclean) values ('unknown', 'unknown') ;""",
"""insert into artist (name) values ('unknown') ;""",
"""insert into album (title, titleclean) values ('unknown', 'unknown') ;"""
)

DECODERS = (MP3, FLAC, MP4, MonkeysAudio, Musepack, WavPack, TrueAudio, OggVorbis, OggTheora, OggSpeex, OggFLAC)
FS_ENCODING = sys.getfilesystemencoding()
THREADS = []
logging.basicConfig(filename="/tmp/cata.log",level=logging.DEBUG)

class SubtreeListener(ProcessEvent):
   """ handler for inotify thread events """
   
   def __init__(self, dbpath, md_queue, fd_queue, condition):
      self.dbpath = dbpath
      self.recents = []
      self.recentartists = {}
      self.recentalbums = {}
      self.recentgenres = {}
      self.recentsongs = {}
      self.md_queue = md_queue
      self.fd_queue = fd_queue
      self.condition = condition
      self.cookies = {}
      ProcessEvent.__init__(self)

   def process_IN_CLOSE_WRITE(self, evt):
      if evt.name[0] == '.':
         return
      logging.debug( "IN_CLOSE_WRITE")
      self.process_event(evt)

   def process_IN_MOVED_FROM(self, evt):
      if evt.name[0] == '.':
         return
      logging.debug( "IN_MOVED_FROM")
      self.process_IN_DELETE(evt)

   def process_IN_MOVED_TO(self, evt):
      if evt.name[0] == '.':
         return
      logging.debug( "IN_MOVED_TO")
      newpath = "%s/%s" % (evt.path, evt.name)
      if os.path.isdir(newpath):
         self.process_IN_ISDIR(evt)
      else:
         self.process_event(evt)

   def process_IN_ISDIR(self, evt):
      if evt.name[0] == '.':
         return
      logging.debug( "IN_ISDIR")
      newpath = "%s/%s" % (evt.path, evt.name)
      db = dbapi.connect(dbpath)
      #exists = db.execute("select id from song where path like ? limit 1;", ("%s%%" % evt.path,)).fetchone()
      #if not exists:
      start_scan(newpath, db, self.md_queue, self.fd_queue, self.condition, True)
      db.close()

   def process_IN_DELETE(self, evt):
      if evt.name[0] == '.':
         return
      logging.debug( "IN_DELETE")
      abspathitem = "%s/%s" % (evt.path, evt.name)
      if abspathitem in (dbpath, "%s-journal" % dbpath):
         return

      self.condition.acquire()
      db = dbapi.connect(dbpath)
      try:
         if evt.dir: #os.path.isdir(abspathitem):
            songs = db.execute("select id from song where path like ?;", ("%s%%" % abspathitem.decode(FS_ENCODING),))
            song_id = songs.fetchall()
            self.recents = []
         else:
            songs = db.execute("select id from song where path = ?;", (abspathitem.decode(FS_ENCODING),))
            song_id = songs.fetchone()
         for s_i in song_id:
            db.execute("delete from song where id = ?;", s_i)
            db.execute("delete from song_x_tag where song_id = ?;", s_i)
         db.commit()
      except Exception as e:
         logging.debug( "OH CRAP CRAP %s " % e)
         pass
      finally:
         db.close()
         self.condition.release()
      if abspathitem in self.recents:
         self.recents.remove(abspathitem)

   def process_event(self, evt):
      if evt.pathname == self.dbpath:
         return
      db = dbapi.connect(dbpath)
      abspathitem = "%s/%s" % (evt.path, evt.name)
      if os.path.isdir(abspathitem):
         start_scan(abspathitem, db, self.md_queue, self.fd_queue, self.condition, True)
      else:
         if abspathitem in self.recents:
            db.close()
            return
         collect_metadata(abspathitem, db, self.recentartists, self.recentalbums, self.recentgenres, self.md_queue, self.fd_queue, self.condition)

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
      db.close()


class FiledataThread(threading.Thread):
   """ slow file analyzer thread. Retrieve song's length and bpm where availble """

   daemon = True

   def __init__(self, queue, condition, dbpath):
      threading.Thread.__init__(self)
      self.queue = queue
      self.condition = condition
      self.dbpath = dbpath
      self.running = True

   def stop(self):
      self.running = False

   def run(self):

      logging.debug("FiledataThread Started")
      self.db = dbapi.connect(self.dbpath)
      while self.running:
         try:
            logging.debug("getting queue...")
            path, title, artist = self.queue.get()
            logging.debug("done.\n")
         except Empty:
            logging.debug("fail.\n")
            continue
         except Exception as e:
            logging.debug("%s.\n" % e)
            pass
         if not path:
            continue
         for i in ('30', '60', '90', '120'):
            logging.debug("soxi_process...")
            soxi_process = subprocess.Popen(["/usr/bin/soxi", "-D", path], stdout = subprocess.PIPE, stderr = subprocess.PIPE)
            soxi_output = float(soxi_process.communicate()[0])
            logging.debug("sox_process...")
            sox_process = subprocess.Popen(["/usr/bin/sox", path, "-t", "wav", "/tmp/.stretch.wav", "trim", "0", i], stdout = subprocess.PIPE, stderr = subprocess.PIPE) # well done, dear sox friend. Well done.
            #sox_process.wait()
            sox_process.communicate()
            logging.debug("bpm_process...")
            bpm_process = subprocess.Popen(["/usr/bin/soundstretch", "/tmp/.stretch.wav", "-bpm", "-quick", "-naa"], stdout = subprocess.PIPE, stderr = subprocess.PIPE)
            bpm_output = bpm_process.communicate()
            logging.debug("done.\n")
   
            bpm_pattern = re.search ("Detected BPM rate ([0-9]+)", bpm_output[0], re.M)
            if bpm_pattern:
               bpm = float(bpm_pattern.groups()[0])
               break
            else:
               bpm = 0.0

         self.condition.acquire()
         self.db.execute("update song set bpm = ?, length = ? where path = ?", (bpm, soxi_output, path.decode(FS_ENCODING)))
         self.db.commit()
         self.condition.release()
      self.db.close()

class MetadataThread(threading.Thread):
   """ last.fm client Thread. Collect top tags for each song """
   
   daemon = True

   def __init__(self, queue, condition, dbpath):
      threading.Thread.__init__(self)
      self.daemon = True
      self.queue = queue
      self.condition = condition
      self.dbpath = dbpath
      self.lastfm = pylast.LastFMNetwork( username = USERNAME)
      self.lastfm.api_key = APIKEY
      self.running = True 

   def stop(self):
      self.running = False

   def run(self):

      self.db = dbapi.connect(self.dbpath)
      while self.running:
         try:
            path, title, artist = self.queue.get()
         except Empty:
            continue
         except:
            pass
         if not path:
            continue

         try:
            tags = self.lastfm.get_track(artist, title).get_top_tags()
         except:
            tags = []
         
         if not tags:
            continue

         self.condition.acquire()
         song_id = self.db.execute("select id from song where path = ?", (path.decode(FS_ENCODING), )).fetchone()[0]
         known_tags = self.db.execute("select distinct weight, name, nameclean from song_x_tag left join tag on (tag_id = id) where song_id = ?;", (song_id,)).fetchall()
         
         for t in tags:
            alreadytag = False
            for kt in known_tags:
               if t.item.name == kt[1] and t.weight != kt[0]:
                  self.db.execute("update song_x_tag set weight = ? where song_id = ? and tag_id = ?;", (t.weight, song_id, t.item.name))
                  alreadytag = True
            if not alreadytag:
               if re.match("^([a-zA-Z0-9] )+[a-zA-Z0-9]$", t.item.name): # you damned "e l e c t r o n i c" tag.
                  cleantag = [re.sub("[^a-zA-Z0-9]+", "", t.item.name)]
               else:
                  cleantag = re.sub("[^a-zA-Z0-9 ]+", "", t.item.name).strip().lower().split()

               savedcleantag = []
               savedcleantag.extend(cleantag)
               for i in xrange(len(cleantag), 0):
                  if len(cleantag[i]) < 3:
                     cleantag.pop(i)
                     
               if len(cleantag) > 6:
                  cleantag = [" ".join(savedcleantag)] # as a single phrase

               tagcomb = permutations(cleantag, len(cleantag))
               nameclean = "".join(cleantag)
               for tc in tagcomb:
                  similartag = self.db.execute("select nameclean from tag where name like ?;", ("%%%s%%" % "%".join(tc),)).fetchone()
                  if similartag and len(similartag[0]) == len(nameclean):
                     nameclean = similartag[0]
               self.db.execute("insert or ignore into tag (name, nameclean) values (?, ?);", (t.item.name, nameclean))
               self.db.commit()
               tagid = self.db.execute("select id from tag where name = ? ", (t.item.name, )).fetchone()[0]
               self.db.execute("insert into song_x_tag (song_id, tag_id, weight) values (?, ?, ?);", (song_id, tagid, t.weight))
         self.db.commit()
         self.condition.release()
      self.db.close()

class PollAnalyzer(threading.Thread):
   """ Analyzer for genres. Collect informations about (lexicographically) similar genres """

   daemon = True

   def __init__(self, condition, dbpath):
      threading.Thread.__init__(self)
      self.condition = condition
      self.dbpath = dbpath
      self.running = True

   def stop(self):
      self.running = False

   def run(self):

      sleeptime = 600
      complete_genres = 0
      while self.running:
         sleep(max(300, sleeptime))
         db = dbapi.connect(dbpath)
         self.condition.acquire()
         collected_genres = db.execute("select count(id) from genre;").fetchall()
         self.condition.release()

         genres_count = complete_genres - collected_genres[0][0]

         if genres_count == 0:
            sleeptime = (sleeptime + 6) % 36
         else:
            sleeptime = sleeptime / 2
            self.condition.acquire()
            known_genres = db.execute("select distinct id, desc, descclean from genre").fetchall()
            self.condition.release()
            commit = False
            for i in xrange(0, len(known_genres)):
               if not self.running:
                  break
               splitted_genre = re.split("[/,;]", known_genres[i][1])
               self.condition.acquire()
               for j in xrange(0, len(known_genres)):
                  if i == j:
                     continue
                  if known_genres[i][2] == known_genres[j][2]:
                     similarity = 1.0
                  else:
                     compared_genre = re.split("[/,;]", known_genres[j][1])
                     distance = {}
                     sametags = 0
                     for a in splitted_genre:
                        if not a:
                           continue
                        for b in compared_genre:
                           if not b or b in distance:
                              continue
                           if len(a) == len(b):
                              h = hamming(a, b) / float(len(a))
                              if h:
                                 distance[b] = h
                              else:
                                sametags = sametags + 1
                           else:
                              distance[b] = levenshtein(a, b) / float(max(len(a), len(b)))
                     if distance:
                        # geometric mean + weighted equal tags
                        similarity =  1.0 - (reduce(lambda x, y: x*y, distance.values()))**(1.0/len(distance)) + (sametags / (sametags + len(distance)))
                     else:
                        similarity = 0.0
                  if similarity > 0.33:
                     if not db.execute("select * from genre_x_genre where id_genre = ? and id_related_genre = ?", (known_genres[i][0], known_genres[j][0])).fetchall():
                        db.execute("insert into genre_x_genre (id_genre, id_related_genre, similarity) values ( ?, ?, ?)", (known_genres[i][0], known_genres[j][0], similarity))
                        commit = True
               if commit:
                  db.commit()
               complete_genres = db.execute("select count(id) from genre;").fetchall()[0][0]
               self.condition.release()
            sleep(sleeptime)
         db.close()

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

      songquery = "select distinct s.id as id, s.title as title, a.name as artist, g.desc as genre, al.title as album, s.path as path, s.length as length, s.bpm as bpm from song s left join genre g on (s.genre_id = g.id) left join artist a on (s.artist_id = a.id) left join album al on (s.album_id = al.id)"
      results = ""

      try:
         if items[0] == "browse":
            where = []
            args = ()
            if len(items) % 2 == 0:
               query = "select * from %s;" % items[-1]
               results = self.text(db.execute(query).fetchall())
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

         elif items[0] == "aggregate" and len(items) == 3:
            requests = items[2].split(',')
            if items[1] == 'genre':
               ids = [i[0] for i in db.execute("select id from genre where desc in ( %s )" %  ",".join(['?' for i in xrange(0,len(requests))]), requests).fetchall()]
               query = "%s where s.genre_id in ( %s ) order by random();" % (songquery, ",".join(['?' for i in xrange(0,len( ids ))]))
               primary_results = db.execute(query, ids).fetchall()

               related_requests = "select distinct id_related_genre from genre_x_genre where id_genre in ( %s ) and similarity > 0.98" %  ",".join(['?' for i in xrange(0,len( ids ))])
               related_ids = [i[0] for i in db.execute(related_requests, ids).fetchall()]
               
               if related_ids:
                  related_query = "%s where s.genre_id in ( %s ) order by random();" % (songquery, ",".join(['?' for i in xrange(0,len( related_ids ))]))
                  secondary_results = db.execute(related_query, related_ids).fetchall()
               else:
                  secondary_results = []

               results = self.m3u(primary_results + secondary_results)

               self.send_response(200)
            elif items[1] == 'bpm':
               query = "%s where bpm  between ? - 10 and ? + 10 ;"
               results = self.m3u(db.execute(query, requests).fetchall())
               self.send_response(200)
            elif items[1] == 'date':
               pass
            else:
               self.send_response(404)

         elif items[0] == "smart":
            pass

      except Exception as e:
         self.send_response(500)
         self.end_headers()
         results = str(e)

      data = StringIO()
      data.write(results)
      data.seek(0)

      self.send_header("Content-Type", "text/plain")
      self.end_headers()
      self.copyfile(data, self.wfile)

      db.close()

   def do_HEAD(self):
      pass

   def m3u(self, songtuple):

      extm3u = """#EXTM3U\n%s\n""" % "\n".join(["""#EXTINF:%d,%s - %s\n%s""" % (i[6], i[2], i[1], i[5]) for i in songtuple])
      return unicode(extm3u).encode('utf-8')

   def text(self, songtuple):

      textdoc = "\n".join(["| %s |" % " | ".join([unicode(j).encode("utf-8") for j in i]) for i in songtuple])
      return textdoc



#class CatalogThreadingTCPServer(ForkingTCPServer):
class CatalogThreadingTCPServer(ThreadingTCPServer):
   """a threaded tcp server interfaced with a database"""

   allow_reuse_address = True

   def stop(self):
      self.shutdown()

   def __init__(self, server_address, RequestHandlerClass, dbpath, bind_and_activate=True):
      ThreadingTCPServer.__init__(self, server_address, RequestHandlerClass, bind_and_activate=True)
      #ForkingTCPServer.__init__(self, server_address, RequestHandlerClass, bind_and_activate=True)
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
   os.nice(19)

   wm_auto = WatchManager()
   subtreemask = IN_CLOSE_WRITE | IN_DELETE | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_ISDIR
   excludefilter = ExcludeFilter(["(.*)sqlite"])
   notifier_sb = ThreadedNotifier(wm_auto, SubtreeListener(dbpath, md_queue, fd_queue, condition))
   notifier_sb.start()
   THREADS.append(notifier_sb)

   wdd_sb = wm_auto.add_watch(path, subtreemask, auto_add=True, rec=True, exclude_filter=excludefilter)

   THREADS.append(PollAnalyzer(condition, dbpath))
   THREADS[-1].start()
#   THREADS.append(CatalogThreadingTCPServer(("localhost", 8080), CatalogHTTPRequestHandler, dbpath))
#   THREADS[-1].serve_forever()

   THREADS[-1].join()
   

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
            collect_metadata(abspathitem, db, recentartists, recentalbums, recentgenres, md_queue, fd_queue, condition)


def collect_metadata(abspathitem, db, recentartists, recentalbums, recentgenres, md_queue, fd_queue, condition):
   """ id3 tags retriever """
   
   id3item = None
   id3v1item = {'TITLE':'', 'ARTIST':'', 'ALBUM':'', 'GENRE':''}
   for decoder in DECODERS:
      try:
         id3item = decoder(abspathitem)
         id3v1item = ID3(abspathitem).as_dict()
         break
      except Exception as e:
         pass
   if not id3item:
     return 
   if 'TITLE' in id3v1item:
      title = id3v1item['TITLE'].strip().lower()
      titleclean = re.sub("[^\w]*", "", title)
   else:
      title = "unknown"
      titleclean ="unknown"
   if 'ARTIST' in id3v1item:
      artist = id3v1item['ARTIST'].strip().lower()
   else:
      artist = "unknown"
   if 'ALBUM' in id3v1item:
      album = id3v1item['ALBUM'].strip().lower()
      albumclean = re.sub("[^\w]*", "", album)
   else:
      album = "unknown"
      albumclean = "unknown"
   if 'GENRE' in id3v1item:
      genre = id3v1item['GENRE'].strip().lower()
      genreclean = re.sub("[^\w]+", "", genre).strip().lower()
   else:
      genre = "unknown"
      genreclean = "unknown"
   length = 0.0

   try:
      title = " ".join(id3item['TIT2'].text).strip().lower()
      titleclean = re.sub("[^\w]*", "", title)
      artist = " ".join(id3item['TPE1'].text).strip().lower()
      album = " ".join(id3item['TALB'].text).strip().lower()
      albumclean = re.sub("[^\w]*", "", album)
      genre = " ".join(id3item['TCON'].text).strip().lower()
      genreclean = re.sub("[^\w]+", "", genre).strip().lower()
      length = float(id3item['TLEN'])
   except:
      pass

   condition.acquire()
   ar = artist if artist else 'unknown'
   if not artist in recentartists.keys():
      if not db.execute("select id from artist where name = ?", (ar,)).fetchone():
         db.execute("insert into artist(name) values(?)", (ar,))
         db.commit()
      recentartists[artist] = db.execute("select id from artist where name = ?", (ar,)).fetchone()[0]
   condition.release()

   condition.acquire()
   al = albumclean if albumclean else 'unknown'
   if not album in recentalbums.keys():
      if not db.execute("select id from album where titleclean = ?", (al,)).fetchone():
         db.execute("insert into album(title, titleclean) values(?, ?)", (album, albumclean))
         db.commit()
      recentalbums[album] = db.execute("select id from album where titleclean = ?", (al,)).fetchone()[0]
   condition.release()

   condition.acquire()
   ge = genre if genre else 'unknown'
   if not genre in recentgenres.keys():
      if not db.execute("select id from genre where desc = ?", (ge,)).fetchone():
         db.execute("insert into genre(desc, descclean) values(?, ?)", (genre, genreclean))
         db.commit()
      recentgenres[genre] = db.execute("select id from genre where desc = ?", (ge,)).fetchone()[0]
   condition.release()

   condition.acquire()
   db.execute("insert or replace into song(title, titleclean, artist_id, genre_id, album_id, path, length) values (?,?,?,?,?,?,?)", (title, titleclean, recentartists[artist], recentgenres[genre], recentalbums[album], abspathitem.decode(FS_ENCODING), length))
   logging.debug("collect_metadata putting new artist on queue")
   try:
      md_queue.put((abspathitem, title, artist))
      fd_queue.put((abspathitem, title, artist))
   except:
      pass
   finally:
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

def hamming(a, b):
   """ calculate the Hamming distance between a and b """
   return sum(c_a != c_b for c_a, c_b in zip(a, b))

def print_usage(argv):
   print("""
usage: %s [-h|--help] [-s|--scan] [-d|--daemonize] [-n|--no-recursive] path\n
\t-h --help           print this help
\t-s --scan           scan path and prepare db
\t-d --daemonize      start daemon on path
\t-n --no-recursive   do not scan subfolders
\t-b --no-bpm         do not perform bpm detection (faster)
""" % argv)
   sys.exit(1)

def perror(reason):
   print("error %s\ntry -h option for usage" % reason)

def shutdown(signum, stack):
   for t in THREADS:
      try:
         t.stop()
      except:
         pass
   sys.exit(0)
   

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
         else:
            patharg = arg
            break

      dbpath = "%s/.%s.sqlite" % (patharg, os.path.basename(patharg))
      prepare = not os.path.exists(dbpath)
      db = dbapi.connect(dbpath)
      if prepare:
         for sql in DBSCHEMA:
            db.execute(sql)
      metadataqueue = Queue() # deque() #SyncQueue()
      filedataqueue = Queue() # deque() #SyncQueue()
      condition = threading.Condition()

      THREADS.append(MetadataThread(metadataqueue, condition, dbpath))
      if bpmdetect:
         THREADS.append(FiledataThread(filedataqueue, condition, dbpath))

      if scan:
         THREADS[-1].start()
         THREADS[-2].start()
         start_scan(patharg, db, metadataqueue, filedataqueue, condition, recursive)
      if daemon:
         signal.signal(signal.SIGTERM, shutdown)
         try:
            daemonize()
            THREADS[-1].start()
            THREADS[-2].start()
            start_daemon(patharg, dbpath, metadataqueue, filedataqueue, condition)
         except KeyboardInterrupt:
            os.kill(os.getpid(), signal.SIGTERM)
