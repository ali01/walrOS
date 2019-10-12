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
BASE_INTERRUPTION_PENALTY = 0.04 # Time units
SPREADSHEET_KEY_FILEPATH = os.path.expanduser("~/.walros/keys.json")

# Signals.
SIGNALS_SUBDIR = ".signals"
TIMER_RUNNING_SIGNAL = "timer_running"
DISPLAY_UPDATE_SIGNAL = "display_update"


def setup():
  # Initialize timer.
  if not os.path.isdir(_config.timer_dir):
    os.makedirs(_config.timer_dir)

  def sigint_handler(signum, frame):  # TODO: put inside with statement instead.
    clear_signals()
    sys.exit(0)

  signal.signal(signal.SIGINT, sigint_handler)


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

  try:
    with diary.Entry(label):  # Tracks effective time spent and overhead.
      while True:  # Timer loop.
        # end time could have been changed; read again from file
        with timer_db.TimerFileProxy(label) as timer:
          if timer.is_complete:
            util.tlog("Timer `%s` completed" % timer.label)
            break
          if unset_signal(DISPLAY_UPDATE_SIGNAL):
            util.tlog("Currently at %d seconds" % timer.remaining)
        time.sleep(1)
  finally:
    with timer_db.TimerFileProxy(label) as timer:
      if not timer.is_complete:
        remaining = timer.pause()
        util.tlog("Pausing timer at %d seconds" % remaining, prefix='\n')
    unset_signal(TIMER_RUNNING_SIGNAL)

  try:  # Timer complete, notify and record.
    if track:
      with timer_db.TimerFileProxy(label) as timer:
        spreadsheet = data_util.Spreadsheet(walros_base.SPREADSHEET_ID)
        worksheet = spreadsheet.GetWorksheet(tracker_data.worksheet_id)
        latest_date = spreadsheet.GetCellValue(
            worksheet_name=tracker_data.worksheet_name,
            row=tracker_data.row_margin + 1, col=1)
        latest_date = latest_date.split()[0]
        date_today = datetime.datetime.now().strftime("%Y-%m-%d")
        if latest_date != date_today:
          util.tlog("Warning: the latest row in spreadsheet does not correspond "
                    "to today's date")
        credit = 1
        if timer.interruptions > 0:
          # Impose exponential cost to interruptions.
          credit -= BASE_INTERRUPTION_PENALTY * 2 ** (timer.interruptions - 1)
        label_count = timer_increment_label_count(
            spreadsheet, worksheet, tracker_data, label, credit)
        util.tlog("%s count: %.2f" % (label, label_count))
        timer.clear()

  except Exception as ex:
    util.tlog("Error updating spreadsheet count")
    raise ex

  finally:
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


def timer_col_index_for_label(spreadsheet, worksheet, tracker_data, label):
  row_index = tracker_data.row_index("COLUMN_LABELS")
  ranges = ["%s!%d:%d" % (tracker_data.worksheet_name, row_index, row_index)]
  response = spreadsheet.GetRanges(ranges, "sheets/data/rowData")
  row_data = response["sheets"][0]["data"][0]["rowData"][0]["values"]
  row_data = row_data[tracker_data.column_margin:]
  row_labels = [ col["effectiveValue"]["stringValue"] for col in row_data ]
  try:
    col_index = row_labels.index(label)
    col_index += tracker_data.column_margin + 1
  except ValueError:
    raise click.ClickException("Label %s not found in spreadsheet." % label)

  return col_index


def timer_increment_label_count(spreadsheet, worksheet, tracker_data, label,
                                credit):
  row = tracker_data.row_margin + 1
  col = timer_col_index_for_label(spreadsheet, worksheet, tracker_data, label)
  cell_value = spreadsheet.GetCellValue(tracker_data.worksheet_name, row, col)
  cell_value = credit if not cell_value else int(cell_value) + credit

  requests = []
  requests.append(worksheet.NewUpdateCellBatchRequest(
      row, col, cell_value, update_cells_mode=data_util.UpdateCellsMode.number))
  spreadsheet.BatchUpdate(requests)

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

