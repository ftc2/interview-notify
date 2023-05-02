#!/usr/bin/env python3

import argparse, sys, threading, logging, re, requests
from pathlib import Path
from file_read_backwards import FileReadBackwards
from time import sleep
from hashlib import sha256

VERSION = '1.2.5'

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
parser.add_argument('--server', default='https://ntfy.sh', help='ntfy server to POST notifications to – default: https://ntfy.sh')
parser.add_argument('--log-dir', required=True, dest='path', type=Path, help='path to IRC logs (continuously checks for newest file to parse)')
parser.add_argument('--nick', required=True, help='your IRC nick')
parser.add_argument('--check-bot-nicks', default=True, action=argparse.BooleanOptionalAction, help="attempt to parse bot's nick. disable if your log files are not like '<nick> message' – default: enabled")
parser.add_argument('--bot-nicks', metavar='NICKS', default='Gatekeeper', help='comma-separated list of bot nicks to watch – default: Gatekeeper')
parser.add_argument('--mode', choices=['red', 'orp'], default='red', help='interview mode (affects triggers) – default: red')
parser.add_argument('-v', action='count', default=5, dest='verbose', help='verbose (invoke multiple times for more verbosity)')
parser.add_argument('--version', action='version', version='{} v{}'.format(parser.prog, VERSION))

def log_scan():
  logging.info('scanner: watching logs in "{}"'.format(args.path))
  curr = find_latest_log()
  logging.debug('scanner: current log: "{}"'.format(curr.name))
  parser, parser_stop = spawn_parser(curr)
  parser.start()
  while True:
    sleep(0.5) # polling delay for checking for newer logfile
    latest = find_latest_log()
    if curr != latest:
      curr = latest
      logging.info('scanner: newer log found: "{}"'.format(curr.name))
      parser_stop.set()
      parser.join()
      parser, parser_stop = spawn_parser(curr)
      parser.start()

def find_latest_log():
  files = [f for f in args.path.iterdir() if f.is_file() and f.name not in ['.DS_Store', 'thumbs.db']]
  if len(files) == 0:
    crit_quit('no log files found')
  return max(files, key=lambda f: f.stat().st_mtime)

def spawn_parser(log_path):
  logging.debug('spawning new parser')
  parser_stop = threading.Event()
  thread = threading.Thread(target=log_parse, args=(log_path, parser_stop))
  return thread, parser_stop

def log_parse(log_path, parser_stop):
  logging.info('parser: using "{}"'.format(log_path.name))
  for line in tail(log_path, parser_stop):
    logging.debug(line)
    if check_trigger(line, 'Currently interviewing: {}'.format(args.nick)):
      logging.info('YOUR INTERVIEW IS HAPPENING ❗')
      notify(line, title='Your interview is happening❗', tags='rotating_light', priority=5)
    elif check_trigger(line, 'Currently interviewing:'):
      logging.info('interview detected ⚠️')
      notify(line, title='Interview detected', tags='warning')
    elif check_trigger(line, '{}:'.format(args.nick), disregard_bot_nicks=True):
      logging.info('mention detected ⚠️')
      notify(line, title="You've been mentioned", tags='wave')
    elif check_netsplit(line):
      logging.info('netsplit detected ⚠️')
      notify(line, title="Netsplit detected – requeue within 10min!", tags='electric_plug', priority=5)

def tail(path, parser_stop):
  with FileReadBackwards(path) as f:
    last_line = f.readline()
    if last_line:
      yield last_line
  with open(path) as f:
    f.seek(0, 2) # os.SEEK_END
    while not parser_stop.is_set():
      line = f.readline()
      if not line:
        sleep(0.1) # polling delay for checking for new lines
        continue
      yield line

def check_trigger(line, trigger, disregard_bot_nicks=False):
  if disregard_bot_nicks or not args.check_bot_nicks:
    return trigger in remove_html_tags(line)
  else:
    triggers = bot_nick_prefix(trigger)
    return any(trigger in line for trigger in triggers)

def check_netsplit(line):
  split_triggers = ['quit', 'disconnect', 'part', 'left', 'leave']
  for trigger in split_triggers:
    for nick in args.bot_nicks.split(','):
      if nick in line and trigger in line:
        return True
  return False

def remove_html_tags(text):
  """Remove html tags from a string"""
  clean = re.compile('<.*?>')
  return re.sub(clean, '', text)

def bot_nick_prefix(trigger):
  nicks = args.bot_nicks.split(',')
  return ['{}> {}'.format(nick, trigger) for nick in nicks]

def notify(data, topic=None, **kwargs):
  if topic is None: topic=args.topic
  headers = {k.capitalize():str(v).encode('utf-8') for (k,v) in kwargs.items()}
  requests.post('{}/{}'.format(args.server, topic),
                data=data.encode(encoding='utf-8'),
                headers=headers)

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

scanner = threading.Thread(target=log_scan)
scanner.start()

# anon telemetry – script version + an anon id sent to a server i don't even control
# i can't determine your nick or IP or anything
anon_id = sha256('H6IhIkah11ee1AxnDKClsujZ6gX9zHf8{}'.format(args.nick).encode('utf-8')).hexdigest()
notify('anon_id={}, mode={}, version={}'.format(anon_id, args.mode, VERSION),
        title='Anonymous Telemetry', topic='interview-notify-telemetry', tags='telephone_receiver')
