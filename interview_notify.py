#!/usr/bin/env python3

import argparse, sys, threading, logging, re, requests
from pathlib import Path
from time import sleep
from datetime import datetime
from file_read_backwards import FileReadBackwards
from hashlib import sha256
from urllib.parse import urljoin

VERSION = '1.3.1'
default_server = 'https://ntfy.sh/'
ACTIVE_WINDOW = 600 # seconds: also watch logs modified this close to the newest one
position_lock = threading.Lock()
position_armed = True # eligible to fire the position alert on the next drop to/below the threshold

parser = argparse.ArgumentParser(prog='interview_notify.py',
  description='IRC Interview Notifier v{}\nhttps://github.com/ftc2/interview-notify'.format(VERSION),
  epilog='''Sends a push notification with https://ntfy.sh/ when it's your turn to interview.
They have a web client and mobile clients. You can have multiple clients subscribed to this.
Wherever you want notifications: open the client, 'Subscribe to topic', pick a unique topic
  name for this script, and use that everywhere.
On mobile, I suggest enabling the 'Instant delivery' feature as well as 'Keep alerting for
  highest priority'. These will enable fastest and most reliable delivery of the
  notification, and your phone will continuously alarm when your interview is ready.''',
  formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument('--topic', required=True, help='ntfy topic name to POST notifications to')
parser.add_argument('--server', default=default_server, help='ntfy server to POST notifications to – default: {}'.format(default_server))
parser.add_argument('--log-dir', required=True, dest='path', type=Path, help='path to IRC logs (continuously checks for recently-active files to parse)')
parser.add_argument('--nick', required=True, help='your IRC nick')
parser.add_argument('--notify-others', default=True, action=argparse.BooleanOptionalAction, help='notify when someone else is called to interview – default: enabled')
parser.add_argument('--position-alert', metavar='N', type=int, default=10, help='notify the first time your queue position reaches N or below; 0 to disable – default: 10')
parser.add_argument('--poll-position', default=False, action='store_true', help='periodically PM !position to the bot via a running HexChat client (Linux/HexChat only, uses your existing connection) – default: disabled')
parser.add_argument('--poll-interval', metavar='MIN,MAX', default='30,60', help='random minutes between !position sends – default: 30,60')
parser.add_argument('--check-bot-nicks', default=True, action=argparse.BooleanOptionalAction, help="attempt to parse bot's nick. disable if your log files are not like '<nick> message' – default: enabled")
parser.add_argument('--bot-nicks', metavar='NICKS', default='Gatekeeper', help='comma-separated list of bot nicks to watch – default: Gatekeeper')
parser.add_argument('--mode', choices=['red', 'ops'], default='red', help='interview mode (affects triggers) – default: red')
parser.add_argument('-v', action='count', default=5, dest='verbose', help='verbose (invoke multiple times for more verbosity)')
parser.add_argument('--version', action='version', version='{} v{}'.format(parser.prog, VERSION))

def log_scan():
  """Poll dir for recently-active log files and run a parser thread for each"""
  logging.info('scanner: watching logs in "{}"'.format(args.path))
  parsers = {} # log path -> (thread, stop_event)
  while True:
    active = active_logs()
    for path in active:
      entry = parsers.get(path)
      if entry is None or not entry[0].is_alive():
        if entry is None:
          logging.info('scanner: watching log "{}"'.format(path.name))
        else:
          logging.warning('scanner: parser for "{}" stopped; restarting'.format(path.name))
        parser, parser_stop = spawn_parser(path)
        parser.start()
        parsers[path] = (parser, parser_stop)
    for path in list(parsers):
      if path not in active:
        logging.debug('scanner: log went idle: "{}"'.format(path.name))
        parser, parser_stop = parsers.pop(path)
        parser_stop.set()
        parser.join()
    sleep(0.5) # polling delay for checking for log activity

def active_logs():
  """Find log files modified close to the most recent activity"""
  files = [f for f in args.path.iterdir() if f.is_file() and f.name not in ['.DS_Store', 'thumbs.db']]
  if len(files) == 0:
    crit_quit('no log files found')
  newest = max(f.stat().st_mtime for f in files)
  return {f for f in files if newest - f.stat().st_mtime <= ACTIVE_WINDOW}

def spawn_parser(log_path):
  """Spawn new parser thread"""
  logging.debug('spawning new parser')
  parser_stop = threading.Event()
  thread = threading.Thread(target=log_parse, args=(log_path, parser_stop))
  return thread, parser_stop

def line_age(line):
  """Seconds since the line's log timestamp; 0 if unparseable (treat as live)."""
  t = re.match(r'(\w{3} \d{2} \d{2}:\d{2}:\d{2})', line)
  if not t:
    return 0
  try:
    when = datetime.strptime('{} {}'.format(datetime.now().year, t.group(1)), '%Y %b %d %H:%M:%S')
  except ValueError:
    return 0
  return (datetime.now() - when).total_seconds()

def check_position(line):
  """Notify the first time our queue position reaches the alert threshold.

  Re-arms once the position climbs back above the threshold, so a netsplit that
  resets the queue and a later re-approach will alert again.
  """
  global position_armed
  if args.position_alert <= 0:
    return
  m = re.search(r'You are in position (\d+) of \d+', line)
  if not m:
    return
  if not any(bot in line for bot in args.bot_nicks.split(',')): # must be the bot's reply, not chatter
    return
  if line_age(line) > 300: # ignore the historical last line replayed when a parser (re)starts
    return
  pos = int(m.group(1))
  fire = False
  with position_lock:
    if pos > args.position_alert:
      position_armed = True
    elif position_armed:
      position_armed = False
      fire = True
  if fire:
    logging.info('position alert: now #{} in the queue ❗'.format(pos))
    notify(line, title="You're #{} in the queue!".format(pos), tags='checkered_flag', priority=5)

def log_parse(log_path, parser_stop):
  """Parse log file and notify on triggers (parser thread)"""
  logging.info('parser: using "{}"'.format(log_path.name))
  for line in tail(log_path, parser_stop):
    logging.debug(line)
    check_position(line)
    if check_trigger(line, 'Currently interviewing: {}'.format(args.nick)):
      logging.info('YOUR INTERVIEW IS HAPPENING ❗')
      notify(line, title='Your interview is happening❗', tags='rotating_light', priority=5)
    elif check_trigger(line, 'gives voice to {}'.format(args.nick), disregard_bot_nicks=True):
      logging.info('YOUR INTERVIEW IS HAPPENING ❗')
      notify(line, title='Your interview is happening❗', tags='rotating_light', priority=5)
    elif args.nick in remove_html_tags(line) and ('say my name' in line.lower() or 'type my name' in line.lower()):
      logging.info('YOUR INTERVIEW IS HAPPENING ❗')
      notify(line, title='Your interview is happening❗', tags='rotating_light', priority=5)
    elif args.notify_others and check_trigger(line, 'Currently interviewing:'):
      logging.info('interview detected ⚠️')
      notify(line, title='Interview detected', tags='warning')
    elif check_trigger(line, '{}:'.format(args.nick), disregard_bot_nicks=True):
      logging.info('mention detected ⚠️')
      notify(line, title="You've been mentioned", tags='wave')
    elif check_words(line, triggers=['quit', 'disconnect', 'part', 'left', 'leave']) and not check_trigger(line, "'Soon' does not imply any particular date"):
      logging.info('netsplit detected ⚠️')
      notify(line, title="Netsplit detected – requeue within 10min!", tags='electric_plug', priority=5)
    elif check_words(line, triggers=['kick'], check_nick=True) or 'You have been kicked' in line:
      logging.info('kick detected ⚠️')
      notify(line, title="You've been kicked – rejoin & requeue ASAP!", tags='anger', priority=5)

def tail(path, parser_stop):
  """Poll file and yield lines as they appear"""
  try:
    with FileReadBackwards(path) as f:
      last_line = f.readline()
      if last_line:
        yield last_line
  except Exception as e:
    logging.warning('tail: could not read last line of "{}" ({})'.format(path.name, e))
  with open(path, encoding='utf-8', errors='replace') as f: # errors='replace': survive stray bytes in IRC chat
    f.seek(0, 2) # os.SEEK_END
    while not parser_stop.is_set():
      line = f.readline()
      if not line:
        sleep(0.1) # polling delay for checking for new lines
        continue
      yield line

def check_trigger(line, trigger, disregard_bot_nicks=False):
  """Check for a trigger in a line"""
  if disregard_bot_nicks or not args.check_bot_nicks:
    return trigger in remove_html_tags(line)
  else:
    triggers = bot_nick_prefix(trigger)
    line = line.replace('\t', ' ') # HexChat delimits '<nick>\tmessage' with a tab
    return any(trigger in line for trigger in triggers)

def check_words(line, triggers, check_nick=False):
  """Check if a trigger & a bot nick & (optionally) user nick all appear in a string"""
  for trigger in triggers:
    for bot in args.bot_nicks.split(','):
      if check_nick:
        if args.nick in line and bot in line and trigger.lower() in line.lower():
          return True
      else:
        if bot in line and trigger.lower() in line.lower():
          return True
  return False

def remove_html_tags(text):
  """Remove html tags from a string"""
  clean = re.compile('<.*?>')
  return re.sub(clean, '', text)

def bot_nick_prefix(trigger):
  """Prefix a trigger with bot nick(s) to reduce false positives"""
  nicks = args.bot_nicks.split(',')
  return ['{}> {}'.format(nick, trigger) for nick in nicks]

def notify(data, topic=None, server=None, **kwargs):
  """Send notification via ntfy"""
  if topic is None: topic=args.topic
  if server is None: server=args.server
  if server[-1] != '/': server += '/'
  target = urljoin(server, topic, allow_fragments=False)
  headers = {k.capitalize():str(v).encode('utf-8') for (k,v) in kwargs.items()}
  try:
    requests.post(target,
                  data=data.encode(encoding='utf-8'),
                  headers=headers)
  except Exception as e: # a transient network error must not kill the parser thread
    logging.warning('notify: POST to {} failed ({})'.format(target, e))

def anon_telemetry():
  """Send anonymous telemetry

  Why? I won't bother working on it if I don't see people using it!
  I can't get your nick or IP or anything.

  sends: anon id based on nick, script mode, script version
  """
  seed = 'H6IhIkah11ee1AxnDKClsujZ6gX9zHf8'
  nick_sha = sha256(args.nick.encode('utf-8')).hexdigest()
  anon_id = sha256('{}{}'.format(nick_sha, seed).encode('utf-8')).hexdigest()
  notify('anon_id={}, mode={}, version={}'.format(anon_id, args.mode, VERSION),
          server=default_server,
          title='Anonymous Telemetry', topic='interview-notify-telemetry', tags='telephone_receiver')

def crit_quit(msg):
  logging.critical(msg)
  sys.exit()

# ----------

args = parser.parse_args()

args.verbose = 70 - (10*args.verbose) if args.verbose > 0 else 0
logging.basicConfig(level=args.verbose, format='%(asctime)s %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

if args.mode != 'red':
  crit_quit('"{}" mode not implemented'.format(args.mode))

if args.path.is_file():
  crit_quit('log path invalid: dir expected, got file')
elif not args.path.is_dir():
  crit_quit('log path invalid')

if args.poll_position:
  import position_poller
  try:
    lo, hi = sorted(float(m) * 60 for m in args.poll_interval.split(',')) # sorted: tolerate MAX,MIN
  except ValueError:
    crit_quit('--poll-interval must be two numbers "MIN,MAX" in minutes, e.g. 30,60')
  if lo < 60:
    crit_quit('--poll-interval minimum is 1 minute, to avoid flooding the bot')
  position_poller.start(threading.Event(), target=args.bot_nicks.split(',')[0],
                        interval_min=lo, interval_max=hi, context_channel='#red-invites')

scanner = threading.Thread(target=log_scan)
scanner.start()

anon_telemetry()
