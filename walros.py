#!/usr/bin/env python

import datetime
import time
import itertools
import json
import os
import os.path
import signal
import subprocess
import sys

import click
import gspread
from oauth2client.service_account import ServiceAccountCredentials


FOCUS_UNIT_DURATION = 1800  # seconds (30 minutes)
SPREADSHEET_KEY_FILEPATH = os.path.expanduser("~/.walros/keys.json")

TIMER_DIRECTORY_PATH = os.path.expanduser("~/.walros/timer")
TIMER_ENDTIME_FILENAME = "endtime"
TIMER_RESUME_FILE_SUFFIX = "-paused"

TIMER_WORKSHEET_NAME = "Time"
TIMER_WORKSHEET_COLUMN_MARGIN = 2


@click.group()
def walros():
  pass


@walros.group()
def timer():
  # Initialize timer.
  if not os.path.isdir(TIMER_DIRECTORY_PATH):
    os.makedirs(TIMER_DIRECTORY_PATH)

  endtime_filepath = os.path.join(TIMER_DIRECTORY_PATH, TIMER_ENDTIME_FILENAME)
  if not os.path.isfile(endtime_filepath):
    with open(endtime_filepath, 'w') as f:
      f.write(str(0.0))
      f.flush()

  # TODO(alive): if timer is already running; exit with error.


@timer.command()
@click.option("-l", "--label", default="code")
@click.option("-s", "--seconds", default=0.0)
@click.option("-m", "--minutes", default=0.0)
@click.option("-h", "--hours", default=0.0)
@click.option("-w", "--whitenoise", is_flag=True)
@click.option("--track/--no-track", default=True)
@click.option("--force", is_flag=True)
def start(label, seconds, minutes, hours, whitenoise, track, force):
  # TODO(alive): decompose
  def sigint_handler(signum, frame):
    with open(timer_resource_path(TIMER_ENDTIME_FILENAME), 'r') as f:
      endtime = float(f.read())

    with open(timer_resource_path(TIMER_ENDTIME_FILENAME), 'w') as f:
      f.write(str(0.0))

    delta = endtime - time.time()
    if delta > 0.0:
      with open(timer_resume_filepath(label), 'w') as f:
        f.write(str(delta))

      click.echo("\nPausing timer at %f." % delta)

      # TODO(alive): do not increment if track flag is false
      subprocess.call(["blink -q --rgb=0xff,0xa0,0x00 --blink=10 &"],
                      shell=True)

    cleanup()
    sys.exit(0)

  signal.signal(signal.SIGINT, sigint_handler)

  if not seconds and not minutes and not hours:
    seconds = FOCUS_UNIT_DURATION

  resume_filepath = timer_resume_filepath(label)
  if not force and os.path.isfile(resume_filepath):
    with open(resume_filepath, 'r') as f:
      delta = float(f.read())
      endtime = time.time() + delta
    os.remove(resume_filepath)
    click.echo("Resuming at %f" % delta)
  else:
    delta = seconds + minutes * 60 + hours * 3600
    endtime = time.time() + delta
    click.echo(delta)

  endtime_filepath = timer_resource_path(TIMER_ENDTIME_FILENAME)
  with open(endtime_filepath, 'w') as f:
    f.write(str(endtime))
    f.flush()

  subprocess.call(["blink", "-q", "--red"])

  while True:
    # end time could have been changed; read again from file
    with open(endtime_filepath, 'r') as f:
      endtime = float(f.read())

    if time.time() > endtime:
      break

    time.sleep(1)

  try:
    if track:
      worksheet = walros_worksheet(TIMER_WORKSHEET_NAME)
      latest_date = worksheet.cell(2, 1).value
      latest_date = latest_date.split()[0]
      date_today = datetime.datetime.now().strftime("%Y-%m-%d")
      if latest_date != date_today:
        click.echo("Warning: the latest row in spreadsheet does not correspond "
                   "to today's date.")
      label_count = timer_increment_label_count(label)
      click.echo("%s count: %d" % (label, label_count))

  except Exception as ex:
    click.echo(str(ex))

  finally:
    timer_notify()

@timer.command()
@click.option("-d", "--data", is_flag=True)
def status(data):
  # Running timer.
  with open(timer_resource_path(TIMER_ENDTIME_FILENAME), 'r') as f:
    delta = max(float(f.read()) - time.time(), 0.0)
    if delta > 0:
      click.echo("  current: %f" % delta)

  # Paused timers.
  for timer in timer_paused_filepaths():
    label = os.path.basename(timer[:timer.rfind(TIMER_RESUME_FILE_SUFFIX)])
    with open(timer, 'r') as f:
      delta = float(f.read())
      click.echo("  %s: %f" % (label, delta))

@timer.command()
@click.option("-l", "--label")
def clear(label):
  if label:
    try:
      os.remove(timer_resource_path("%s%s" % (label, TIMER_RESUME_FILE_SUFFIX)))
    except OSError:
      click.echo("No paused timer with label '%s' exists." % label)

  else:
    for timer in timer_paused_filepaths():
      os.remove(timer)


@timer.command()
@click.argument("mod_expression")
def mod(mod_expression):
  click.echo(mod_expression)

# IAR: inc/dec commands?


def timer_notify():
  click.echo("Notified at %s" %
             datetime.datetime.strftime(datetime.datetime.now(), "%H:%M"))
  subprocess.call(["blink -q --blink=20 &"], shell=True)
  for ix in range(0, 3):
    subprocess.call(["afplay", "/System/Library/Sounds/Blow.aiff"])
    time.sleep(2)


def timer_resource_path(name):
  return os.path.join(TIMER_DIRECTORY_PATH, name)

def timer_resume_filepath(label):
  resource_name = "%s%s" % (label, TIMER_RESUME_FILE_SUFFIX)
  return timer_resource_path(resource_name)

def timer_paused_filepaths():
  filenames = ( f for f in os.listdir(TIMER_DIRECTORY_PATH)
                if os.path.isfile(os.path.join(TIMER_DIRECTORY_PATH, f)) )
  timer_filenames = ( f for f in filenames
                      if f.endswith(TIMER_RESUME_FILE_SUFFIX))
  return itertools.imap(timer_resource_path, timer_filenames)


def timer_col_index_for_label(label):
  worksheet = walros_worksheet(TIMER_WORKSHEET_NAME)
  row_labels = worksheet.row_values(1)[TIMER_WORKSHEET_COLUMN_MARGIN:]
  try:
    col_index = row_labels.index(label)
    col_index += TIMER_WORKSHEET_COLUMN_MARGIN + 1
  except ValueError:
    raise click.ClickException("Label %s not found in spreadsheet." % label)

  return col_index


def timer_increment_label_count(label):
  worksheet = walros_worksheet(TIMER_WORKSHEET_NAME)
  count_cell = worksheet.cell(2, timer_col_index_for_label(label))
  cell_value = 1 if not count_cell.value else int(count_cell.value) + 1
  count_cell.value = str(cell_value)
  worksheet.update_cells([count_cell])
  return cell_value


def init_memoize(init_fn):
  """Decorator to memoize initialization"""
  obj = []
  def wrapper_fn():
    if len(obj) == 0:
      obj.append(init_fn())
    return obj[0]

  return wrapper_fn


def walros_spreadsheet():
  scopes = ['https://spreadsheets.google.com/feeds']
  credentials = ServiceAccountCredentials.from_json_keyfile_name(
      SPREADSHEET_KEY_FILEPATH, scopes=scopes)
  gclient = gspread.authorize(credentials)
  return gclient.open("walrOS")


def walros_worksheet(worksheet_name):
  spreadsheet = walros_spreadsheet()
  return spreadsheet.worksheet(worksheet_name)


def cleanup():
  # TODO(alive): write blink wrapper
  subprocess.call(["blink", "-q", "--off"])


if __name__ == "__main__":
  subprocess.call(["killall blink &> /dev/null"], shell=True)
  walros()
  cleanup()
