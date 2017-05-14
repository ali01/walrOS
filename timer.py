import datetime
import fcntl
import itertools
import json
import os
import os.path
import signal
import subprocess
import sys
import time

import click

# TODO(alive): move away from gspread
import gspread
from oauth2client.service_account import ServiceAccountCredentials

import data_util
import log as log_module
import walros_base

from data_util import UpdateCellsMode
from util import OpenAndLock

WORKSHEET_NAME = "Time"
WORKSHEET_ID = 925912296  # Found in URL.
HEADER_ROWS = [
  "TITLES",
  "COLUMN_LABELS",
  "TOTALS",
  "MEDIANS",
  "PERCENTILE_75",
  "PERCENTILE_90",
  "MAX",
  "WEIGHTS",
  "RELATIVE_VALUE",
  "GOAL_PERCENTILE",
  "GOAL_NUMBER",
  "PROGRESS",
]

# Margins
COLUMN_MARGIN = 5

# We currently assume that each day column is immediately followed
# by week, month, and quarter columns.
DAY_COLUMN_INDICES = [2, 6, 10, 14, 18, 22, 26, 30, 34, 38]

FOCUS_UNIT_DURATION = 1800  # Seconds (30 minutes).
SPREADSHEET_KEY_FILEPATH = os.path.expanduser("~/.walros/keys.json")

DIRECTORY_PATH = os.path.expanduser("~/.walros/timer")
ENDTIME_FILENAME = "endtime"
RESUME_FILE_SUFFIX = "-paused"

# Signals.
SIGNALS_SUBDIR = ".signals"
TIMER_RUNNING_SIGNAL = "timer_running"
DISPLAY_UPDATE_SIGNAL = "display_update"


def setup():
  # Initialize timer.
  if not os.path.isdir(DIRECTORY_PATH):
    os.makedirs(DIRECTORY_PATH)

  endtime_filepath = timer_resource_path(ENDTIME_FILENAME)
  if not os.path.isfile(endtime_filepath):
    with OpenAndLock(endtime_filepath, 'w') as f:
      write_float(f, 0.0)


def init_tracker_data():
  tracker_data = walros_base.TrackerData()
  tracker_data.worksheet_id = WORKSHEET_ID
  tracker_data.worksheet_name = WORKSHEET_NAME
  tracker_data.column_margin = COLUMN_MARGIN
  tracker_data.header_rows = HEADER_ROWS
  tracker_data.day_column_indices = DAY_COLUMN_INDICES
  tracker_data.reduce_formula = lambda r: "=SUM(%s)" % r
  return tracker_data


def init_command():
  tracker_data = init_tracker_data()
  spreadsheet = data_util.Spreadsheet(walros_base.SPREADSHEET_ID)
  worksheet = spreadsheet.GetWorksheet(tracker_data.worksheet_id)
  init_requests = walros_base.build_init_requests(tracker_data, spreadsheet,
                                                  worksheet)
  if len(init_requests) == 0:
    click.echo("%s sheet is already initialized for today." %
               tracker_data.worksheet_name)
    return

  # Update sheet wide statistics.
  init_requests += build_update_statistics_requests(worksheet, tracker_data)

  # Send requests.
  response = spreadsheet.BatchUpdate(init_requests)


def build_update_statistics_requests(worksheet, tracker_data):
  requests = []
  for i in tracker_data.day_column_indices:
    column_letter = walros_base.col_num_to_letter(i)
    row_range = "%s%d:%s" % (column_letter, tracker_data.last_day_row_index,
                             column_letter)
    sum_formula = "=SUM(%s)" % row_range
    requests.append(worksheet.NewUpdateCellBatchRequest(
        tracker_data.row_index("TOTALS"), i, sum_formula,
        UpdateCellsMode.formula))

  # Build total count formula.
  total_count_formula = '='
  for i in tracker_data.day_column_indices[1:]:
    total_count_formula += "%s%d+" % (walros_base.col_num_to_letter(i),
                                      tracker_data.last_day_row_index)

  total_count_formula = total_count_formula[:-1]  # Strip trailing plus sign.
  requests.append(worksheet.NewUpdateCellBatchRequest(
      tracker_data.last_day_row_index, 2, total_count_formula,
      UpdateCellsMode.formula))

  return requests


def start_command(label, seconds, minutes, hours, whitenoise, track, force):
  def sigint_handler(signum, frame):
    with OpenAndLock(timer_resource_path(ENDTIME_FILENAME), 'r') as f:
      endtime = read_float(f)

    with OpenAndLock(timer_resource_path(ENDTIME_FILENAME), 'w') as f:
      write_float(f, 0.0)

    delta = endtime - time.time()
    if delta > 0.0:
      with open(timer_resume_filepath(label), 'w') as f:
        write_float(f, delta)

      click.echo("\n%s: Pausing timer at %d seconds." %
                 (datetime.datetime.strftime(datetime.datetime.now(), "%H:%M"),
                  delta))
    clear_signals()
    sys.exit(0)

  signal.signal(signal.SIGINT, sigint_handler)
  tracker_data = init_tracker_data()

  if not set_signal(TIMER_RUNNING_SIGNAL):
    click.echo("%s: A timer is already running." %
               datetime.datetime.strftime(datetime.datetime.now(), "%H:%M"))
    return

  clear_signals(exclude=[TIMER_RUNNING_SIGNAL])

  if not seconds and not minutes and not hours:
    seconds = FOCUS_UNIT_DURATION

  resume_filepath = timer_resume_filepath(label)
  if not force and os.path.isfile(resume_filepath):
    with open(resume_filepath, 'r') as f:
      delta = read_float(f)

    endtime = time.time() + delta
    os.remove(resume_filepath)
    click.echo("%s: Resuming at %d seconds." %
               (datetime.datetime.strftime(datetime.datetime.now(), "%H:%M"),
                delta))
  else:
    delta = seconds + minutes * 60 + hours * 3600
    endtime = time.time() + delta
    click.echo("%s: Starting at %d seconds." %
               (datetime.datetime.strftime(datetime.datetime.now(), "%H:%M"),
                delta))

  endtime_filepath = timer_resource_path(ENDTIME_FILENAME)
  with OpenAndLock(endtime_filepath, 'w') as f:
    write_float(f, endtime)


  with log_module.Entry(label):  # Tracks effective time spent and overhead.
    while True:  # Timer loop.
      # end time could have been changed; read again from file
      with OpenAndLock(endtime_filepath, 'r') as f:
        endtime = read_float(f)

      delta = endtime - time.time()
      if delta <= 0.0:
        break

      if unset_signal(DISPLAY_UPDATE_SIGNAL):
        click.echo("%s: Currently at %d seconds." %
                   (datetime.datetime.strftime(datetime.datetime.now(), "%H:%M"),
                    delta))
      time.sleep(1)

  try:  # Notify and record.
    if track:
      worksheet = walros_worksheet(tracker_data.worksheet_name)
      latest_date = worksheet.cell(tracker_data.row_margin + 1, 1).value
      latest_date = latest_date.split()[0]
      date_today = datetime.datetime.now().strftime("%Y-%m-%d")
      if latest_date != date_today:
        click.echo("Warning: the latest row in spreadsheet does not correspond "
                   "to today's date.")
      label_count = timer_increment_label_count(tracker_data, label)
      click.echo("%s count: %d" % (label, label_count))

  except Exception as ex:
    click.echo("Error updating spreadsheet count.")
    raise ex

  finally:
    clear_signals()
    timer_notify()


def status_command(data):
  # Running timer.
  with OpenAndLock(timer_resource_path(ENDTIME_FILENAME), 'r') as f:
    endtime = read_float(f)
    delta = max(endtime - time.time(), 0.0)
    if delta > 0:
      click.echo("  current: %f" % delta)

  # Paused timers.
  for timer in timer_paused_filepaths():
    label = os.path.basename(timer[:timer.rfind(RESUME_FILE_SUFFIX)])
    with open(timer, 'r') as f:
      delta = read_float(f)
      click.echo("  %s: %f" % (label, delta))


def clear_command(label):
  try:
    os.remove(timer_resource_path("%s%s" % (label, RESUME_FILE_SUFFIX)))
  except OSError:
    click.echo("No paused timer with label '%s' exists." % label)


def inc_command(delta):
  now = time.time()
  with OpenAndLock(timer_resource_path(ENDTIME_FILENAME), 'r+') as f:
    endtime = read_float(f)
    remaining = endtime - now
    if remaining <= 0.0 or not signal_is_set(TIMER_RUNNING_SIGNAL):
      click.echo("No timer is currently running.")
      return

    endtime += delta
    write_float(f, endtime)

  set_signal(DISPLAY_UPDATE_SIGNAL)
  click.echo("  previous: %f" % remaining)
  click.echo("  current:  %f" % max(endtime - now, 0.0))


def timer_notify():
  time_str = datetime.datetime.strftime(datetime.datetime.now(), "%H:%M")
  click.echo("%s: Notified" % time_str)
  subprocess.call(["osascript -e \'display notification " +
                   "\"%s: notify\" with title \"walrOS timer\"\'" % time_str],
                  shell=True)
  for ix in range(0, 3):
    subprocess.call(["afplay", "/System/Library/Sounds/Blow.aiff"])
    time.sleep(2)


def timer_resource_path(name):
  return os.path.join(DIRECTORY_PATH, name)


def timer_signal_path(signal_name):
  return timer_resource_path(os.path.join(SIGNALS_SUBDIR, signal_name))


def timer_resume_filepath(label):
  resource_name = "%s%s" % (label, RESUME_FILE_SUFFIX)
  return timer_resource_path(resource_name)


def timer_paused_filepaths():
  filenames = ( f for f in os.listdir(DIRECTORY_PATH)
                if os.path.isfile(os.path.join(DIRECTORY_PATH, f)) )
  timer_filenames = ( f for f in filenames
                      if f.endswith(RESUME_FILE_SUFFIX))
  return itertools.imap(timer_resource_path, timer_filenames)


def timer_col_index_for_label(tracker_data, label):
  worksheet = walros_worksheet(tracker_data.worksheet_name)
  row = worksheet.row_values(tracker_data.row_index("COLUMN_LABELS"))
  row_labels = row[tracker_data.column_margin:]
  try:
    col_index = row_labels.index(label)
    col_index += tracker_data.column_margin + 1
  except ValueError:
    raise click.ClickException("Label %s not found in spreadsheet." % label)

  return col_index


def timer_increment_label_count(tracker_data, label):
  worksheet = walros_worksheet(tracker_data.worksheet_name)
  count_cell = worksheet.cell(tracker_data.row_margin + 1,
                              timer_col_index_for_label(tracker_data, label))
  cell_value = 1 if not count_cell.value else int(count_cell.value) + 1
  count_cell.value = str(cell_value)
  worksheet.update_cells([count_cell])
  return cell_value


def read_float(f):
  f.seek(0)
  return float(f.read().strip())


def write_float(f, value):
  f.seek(0)
  f.truncate(0)
  f.write("%f" % value)


def set_signal(signal_name):
  signal_filepath = timer_signal_path(signal_name)
  if os.path.isfile(signal_filepath):
    return False

  with open(signal_filepath, 'w') as f:
    f.flush()

  return True


def unset_signal(signal_name):
  signal_filepath = timer_signal_path(signal_name)
  if os.path.isfile(signal_filepath):
    os.remove(signal_filepath)
    return True
  return False


def signal_is_set(signal_name):
  signal_filepath = timer_signal_path(signal_name)
  if os.path.isfile(signal_filepath):
    return True
  return False


def clear_signals(exclude=[]):
  signals_dirpath = os.path.join(DIRECTORY_PATH, SIGNALS_SUBDIR)
  for signal_name in os.listdir(signals_dirpath):
    if signal_name not in exclude:
      os.remove(timer_signal_path(signal_name))


# -- Authentication --
# TODO: move away from gSpread
def walros_spreadsheet():
  scopes = ['https://spreadsheets.google.com/feeds']
  credentials = ServiceAccountCredentials.from_json_keyfile_name(
      SPREADSHEET_KEY_FILEPATH, scopes=scopes)
  gclient = gspread.authorize(credentials)
  return gclient.open("walrOS")


def walros_worksheet(worksheet_name):
  spreadsheet = walros_spreadsheet()
  return spreadsheet.worksheet(worksheet_name)

