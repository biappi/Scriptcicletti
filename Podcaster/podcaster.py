#!/usr/bin/env python2.5
#
# podcaster.py

import re
import sys
import subprocess
import os
import shlex
import commands
import datetime
import shutil
from mutagen.mp4 import MP4

MAPMONTH=( 'Jan', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec' )

if __name__ == "__main__":

   args = []
   args.extend(sys.argv)
   dryrun = False
   verbose = False

   if "-d" in args:
      dryrun = True
      args.pop(args.index("-d"))
   if "-v" in args:
      verbose = True
      args.pop(args.index("-v"))

   if verbose:
      print "verbose mode active"
      if dryrun:
         print "dryrun mode active"

   if len(args) < 4:
      sys.exit(1)

   filename = args[1]
   talksheet = args[2]
   targetdir = args[3]

   # 1. extract datetime from cuesheet
   start_date, start_time = re.match("[^\[]*\[([0-9\-]*)\]\[([0-9\:]*)\].*", filename).groups()
   start_datetime = datetime.datetime(*([int(i) for i in start_date.split('-') + start_time.split(':')]))

   # 1a. extract length
   length = float(commands.getoutput("/usr/bin/soxi -D %s" % filename))
   stop_datetime = start_datetime + datetime.timedelta(0, length)
   if verbose:
      print "analying period %s - %s" % (start_datetime, stop_datetime)

   # 2. extract cuepoints from talk.log
   talksheetfile = open(talksheet, "r")
   splits = []
   splitstart = []
   pad = datetime.timedelta(0, 15, 0)
   for line in talksheetfile:
      match = re.search(" ([a-zA-Z ]{3} %s %d (%s):[0-9:]* [A-Z]{1,4} %d) \] - mic_" % (MAPMONTH[start_datetime.month], start_datetime.day, "|".join(["%02d" % i for i in range(start_datetime.hour, stop_datetime.hour + 1)]), start_datetime.year), line)
      if match:
         cue = datetime.datetime.strptime(match.groups()[0], "%a %b %d %H:%M:%S %Z %Y")
         if not splits: # init
            if "up" in line:
               ismicopen = True
            elif "down" in line:
               ismicopen = False

         if cue >= start_datetime and cue <= stop_datetime:
            if not splits and ismicopen: 
               splits.append(start_datetime)
               splitstart.append(start_datetime)
               if verbose:
                  print "mic open @", start_datetime

            if len(splits) and splits[len(splits) -1 ] >= cue:
               continue
            if "up" in line and not ismicopen:
               ismicopen = True
               if cue - pad <= start_datetime:
                  cue = start_datetime
               else:
                  cue = cue - pad
               if not len(splits) or len(splits) and cue > splits[len(splits) - 1]:
                  splits.append(cue)
                  splitstart.append(cue)
                  if verbose:
                     print "mic open @", cue
            elif "down" in line and ismicopen:
               ismicopen = False
               if cue + pad >= stop_datetime:
                  cue = stop_datetime
               else:
                  cue = cue + pad
               splits.append(cue)
               if verbose:
                  print "mic closed @", cue
   talksheetfile.close()


   if len(splits) > 0 and splits[0] != start_datetime:
      splits.insert(0, start_datetime)
   elif len(splits) == 1 and splits[0] == start_datetime:
      splits.pop()

   # 3. generate cuesheet
   xlist=[]
   newcuesheet = """TITLE podcast
PERFORMER "radiocicletta"
FILE "%s" FLAC\n""" % os.path.basename(filename)

   for i in xrange(1, len(splits) + 1):
      delta = splits[i - 1] - start_datetime
      newcuesheet = newcuesheet + """  TRACK %02d AUDIO
    TITLE "untitled"
    PERFORMER "Noone"
    INDEX 01 %d:%02d\n""" % (i, delta.seconds // 3600 * 60 + (delta.seconds % 3600) // 60, delta.seconds % 60)
      if splits[i - 1] in splitstart:
         xlist.append("%02d" % i)
   # 4. shnsplit
   tempdir = "%s.tmp" % filename
   if verbose:
      print newcuesheet
   if not dryrun:
      if os.path.exists(tempdir):
         shutil.rmtree(tempdir)
      os.mkdir(tempdir)
      newcuesheetfile = open("%s/cuesheet.cue" % tempdir, "w")
      newcuesheetfile.write(newcuesheet)
      newcuesheetfile.close()
   
      split_process = subprocess.Popen(["/usr/bin/shnsplit", "-f", "%s/cuesheet.cue" % tempdir, "-d", tempdir, "-x", ",".join(xlist), filename])
      split_process.wait()

   # 5. sox fadein fadeout
      for i in xlist:
         token_length = float(commands.getoutput("/usr/bin/soxi -D %s/split-track%s.wav" % (tempdir, i)))
         if xlist.index(i) == 0:
            sox_proc = subprocess.check_call(["/usr/bin/sox", "--temp", tempdir, "--norm", "-t", "wav", "%s/split-track%s.wav" % (tempdir, i),  "%s/split-track%s-fade.wav" % (tempdir, i), "fade", "p", "0", str(token_length), "5"])
         elif xlist.index(i) == len(xlist) - 1:
            sox_proc = subprocess.check_call(["/usr/bin/sox", "--temp", tempdir, "--norm", "-t", "wav", "%s/split-track%s.wav" % (tempdir, i),  "%s/split-track%s-fade.wav" %(tempdir, i), "fade", "p", "5", str(token_length), "0"])
         else:
            soc_proc = subprocess.check_call(["/usr/bin/sox", "--temp", tempdir, "--norm", "-t", "wav", "%s/split-track%s.wav" % (tempdir, i),  "%s/split-track%s-fade.wav" % (tempdir, i), "trim", "0", str(token_length), "fade", "p", "5", str(token_length), "5"])

   # 6. Encoding aac
      if not splits:
         cat_proc = subprocess.Popen(["/usr/bin/shncat", filename], stdout=subprocess.PIPE)
      else:
         cat_proc = subprocess.Popen(["/usr/bin/sox"] + " ".join(["-t wav -r 44100 -e signed-integer -L  %s/split-track%s-fade.wav" % (tempdir, i) for i in xlist]).split() + ["-t", "wav", "-"], stdout=subprocess.PIPE)
      podfilename = "%s_%s.mp4" % (start_date, start_time.replace(':', '-'))
      enc_proc = subprocess.check_call(["/usr/bin/neroAacEnc", "-ignorelength", "-hev2", "-q", "0.8", "-if", "-", "-of", "%s/%s" % (tempdir, podfilename)], stdin=cat_proc.stdout)

   #id3 = MP4("%s/%s" % (tempdir, podfilename))
   #id3["?nam"] = "Podcast - "
   #id3["?ART"] = "Radiocicletta"
   #id3["purl"] = "http://www.radiocicletta.it"
   #id3["pcst"] = True

   # 8. cleaning
      shutil.move("%s/%s" % (tempdir, podfilename), "%s/%s" % (targetdir, podfilename))
      shutil.rmtree(tempdir)

   
