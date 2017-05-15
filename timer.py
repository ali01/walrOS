import datetime
import fcntl
import itertools
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

import config
import data_util
import diary
import timer_db
import util
import walros_base

from data_util import UpdateCellsMode
from util import OpenAndLock

_config = config.Config()

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

# Signals.
SIGNALS_SUBDIR = ".signals"
TIMER_RUNNING_SIGNAL = "timer_running"
DISPLAY_UPDATE_SIGNAL = "display_update"


def setup():
  # Initialize timer.
  if not os.path.isdir(_config.timer_dir):
    os.makedirs(_config.timer_dir)


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
    util.tlog("%s sheet is already initialized for today" %
              tracker_data.worksheet_name)
    return

  # Update sheet wide statistics.
  init_requests += build_update_statistics_requests(worksheet, tracker_data)

  # Send requests.
  response = spreadsheet.BatchUpdate(init_requests)

# TODO(alive): move sheets logic into separate module.
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
  def sigint_handler(signum, frame):  # TODO: put inside with statement instead.
    with timer_db.TimerFileProxy(label) as timer:
      remaining = timer.pause()
      if not timer.is_complete:
        util.tlog("Pausing timer at %d seconds" % timer.remaining, prefix='\n')
    clear_signals()
    sys.exit(0)

  signal.signal(signal.SIGINT, sigint_handler)
  tracker_data = init_tracker_data()

  if not set_signal(TIMER_RUNNING_SIGNAL):
    util.tlog("A timer is already running")
    return

  clear_signals(exclude=[TIMER_RUNNING_SIGNAL])

  if not seconds and not minutes and not hours:
    seconds = FOCUS_UNIT_DURATION

  if force and timer_db.timer_exists(label):
    with timer_db.TimerFileProxy(label) as timer:
      timer.clear()

  if timer_db.timer_exists(label):
    with timer_db.TimerFileProxy(label) as timer:
      timer.resume()
      util.tlog("Resuming at %d seconds" % timer.remaining)

  else:
    with timer_db.TimerFileProxy(label) as timer:
      timer.start(seconds, minutes, hours)
      util.tlog("Starting at %d seconds" % timer.remaining)

  with diary.Entry(label):  # Tracks effective time spent and overhead.
    while True:  # Timer loop.
      # end time could have been changed; read again from file
      with timer_db.TimerFileProxy(label) as timer:
        if timer.is_complete:
          util.tlog("Timer `%s` completed" % timer.label)
          timer.clear()
          break
        if unset_signal(DISPLAY_UPDATE_SIGNAL):
          util.tlog("Currently at %d seconds" % timer.remaining)
      time.sleep(1)

  try:  # Notify and record.
    if track:
      worksheet = walros_worksheet(tracker_data.worksheet_name)
      latest_date = worksheet.cell(tracker_data.row_margin + 1, 1).value
      latest_date = latest_date.split()[0]
      date_today = datetime.datetime.now().strftime("%Y-%m-%d")
      if latest_date != date_today:
        util.tlog("Warning: the latest row in spreadsheet does not correspond "
                  "to today's date")
      label_count = timer_increment_label_count(tracker_data, label)
      util.tlog("%s count: %d" % (label, label_count))

  except Exception as ex:
    util.tlog("Error updating spreadsheet count")
    raise ex

  finally:
    clear_signals()
    timer_notify()


def status_command(data):
  def timer_status_str(timer):
    return '  %s: %d' % (timer.label, timer.remaining)
  running_timer = timer_db.running_timer()
  if running_timer:
    with running_timer:
      click.secho(timer_status_str(running_timer), fg='green')
  for timer in timer_db.existing_timers():
    with timer:
      if timer.is_running:
        continue
      click.echo(timer_status_str(timer))


def clear_command(label):
  if timer_db.timer_exists(label):
    with timer_db.TimerFileProxy(label) as timer:
      if timer.is_running:
        util.tlog("The timer with label `%s` is currently running" %
                   timer.label)
        return
      timer.clear()
  else:
    util.tlog("No paused timer with label '%s' exists" % label)


def inc_command(delta):
  timer = timer_db.running_timer()
  if not timer:
    util.tlog("No timer is currently running")
    return
  with timer:
    remaining = timer.remaining
    timer.inc(delta)
    click.echo("  previous: %f" % remaining)
    click.echo("  current:  %f" % timer.remaining)
    if diary.increment_effective(timer.label, -1 * delta):
      click.echo("  (diary updated)")

  set_signal(DISPLAY_UPDATE_SIGNAL)


def timer_notify():
  util.tlog("Notified")
  time_str = datetime.datetime.strftime(datetime.datetime.now(), "%H:%M")
  subprocess.call(["osascript -e \'display notification " +
                   "\"%s: notify\" with title \"walrOS timer\"\'" % time_str],
                  shell=True)
  for ix in range(0, 3):
    subprocess.call(["afplay", "/System/Library/Sounds/Blow.aiff"])
    time.sleep(2)


def timer_signal_path(signal_name):
  return os.path.join(_config.timer_dir, SIGNALS_SUBDIR, signal_name)


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


# TODO(alive): move signals into separate module.
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
  signals_dirpath = os.path.join(_config.timer_dir, SIGNALS_SUBDIR)
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

