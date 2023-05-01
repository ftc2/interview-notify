#!/usr/bin/env python3

import argparse, sys, os, threading, logging, requests
from pathlib import Path
from time import sleep

VERSION = '1.0'

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

# logging.critical('50 im a CRITICAL message')
# logging.error('40 im a ERROR message')
# logging.warning('30 im a WARNING message')
# logging.info('20 im a INFO message')
# logging.debug('10 im a DEBUG message')

def log_scan():
  curr = find_latest_log()
  logging.debug('current log: {}'.format(curr.name))
  parser, stop_event = spawn_parser(curr)
  parser.start()
  while True:
    latest = find_latest_log()
    if curr != latest:
      curr = latest
      logging.info('newer log found: {}'.format(curr.name))
      stop_event.set()
      parser.join()
      parser, stop_event = spawn_parser(curr)
      parser.start()
    sleep(2) # scan for new files every 2s

def find_latest_log():
  files = [f for f in args.path.iterdir() if f.is_file() and f.name != '.DS_Store']
  if len(files) == 0:
    crit_quit('no log files found')
  return max(files, key=lambda f: f.stat().st_ctime)

def spawn_parser(log_path):
  logging.debug('spawning new parser')
  stop_event = threading.Event()
  thread = threading.Thread(target=log_parse, args=(log_path, stop_event))
  return thread, stop_event

def log_parse(log_path, stop_event):
  logging.info('parsing log file: {}'.format(log_path.name))
  log = open(log_path, 'r')
  for line in tail(log, stop_event):
    logging.debug(line)
    if check_trigger(line, 'Currently interviewing: {}'.format(args.nick)):
      logging.info('YOUR INTERVIEW IS HAPPENING ❗')
      notify(line, title='Your interview is happening❗', tags='rotating_light', priority=5)
    elif check_trigger(line, 'Currently interviewing:'):
      logging.info('interview detected ⚠️')
      notify(line, title='Interview Detected', tags='warning')

def tail(f, stop_event):
  f.seek(0, os.SEEK_END)
  while not stop_event.is_set():
    line = f.readline()
    if not line:
      sleep(1) # check for new lines every 1s
      continue
    yield line
  yield ''

def check_trigger(line, trigger):
  if args.check_bot_nicks:
    triggers = bot_nick_prefix(trigger)
    return any(trigger in line for trigger in triggers)
  return trigger in line

def bot_nick_prefix(trigger):
  nicks = args.bot_nicks.split(',')
  return ['{}> {}'.format(nick, trigger) for nick in nicks]

def notify(data, **kwargs):
  headers = {k.capitalize():str(v).encode('utf-8') for (k,v) in kwargs.items()}
  requests.post('{}/{}'.format(args.server, args.topic),
                data=data.encode(encoding='utf-8'),
                headers=headers)

def crit_quit(msg):
  logging.critical(msg)
  sys.exit()

# ----------

args = parser.parse_args()

args.verbose = 70 - (10*args.verbose) if args.verbose > 0 else 0
logging.basicConfig(level=args.verbose, format='%(asctime)s %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

logging.info('parsing logs in "{}"'.format(args.path))

if args.mode != 'red':
  crit_quit('"{}" mode not implemented'.format(args.mode))

if args.path.is_file():
  crit_quit('log path invalid: dir expected, got file')
elif not args.path.is_dir():
  crit_quit('log path invalid')

log_scan = threading.Thread(target=log_scan)
log_scan.start()
