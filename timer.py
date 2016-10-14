import copy
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

# TODO(alive): move away from gspread
import gspread
from oauth2client.service_account import ServiceAccountCredentials

import data_util
from data_util import UpdateCellsMode
import walros

APPLICATION_NAME = "walrOS"
SPREADSHEET_ID = "1JvO-sjs2kCFFD2FcX1a7XQ8uYyj-9o-anS9RElrtXYI"
PERMISSION_SCOPES = "https://www.googleapis.com/auth/spreadsheets"
CLIENT_SECRET_FILEPATH = "./.walros/client_secret.json"

FOCUS_UNIT_DURATION = 1800  # Seconds (30 minutes).
SPREADSHEET_KEY_FILEPATH = os.path.expanduser("~/.walros/keys.json")

DIRECTORY_PATH = os.path.expanduser("~/.walros/timer")
ENDTIME_FILENAME = "endtime"
LOCK_FILENAME = ".lock"
RESUME_FILE_SUFFIX = "-paused"
WORKSHEET_NAME = "Time"
WORKSHEET_ID = 925912296  # Found in URL.
DATE_FORMAT = "%Y-%m-%d %A"

HEADER_ROWS = ["TITLE", "LABELS", "TOTALS", "MEDIANS", "PERCENTILE", "MAX"]

# Margins
ROW_MARGIN = len(HEADER_ROWS)
COLUMN_MARGIN = 2

LAST_DAY_ROW_INDEX = ROW_MARGIN + 1

# We currently assume that each day column is immediately followed
# by a week column and a month column.
ROW_STATS_COLUMN_INDICES = [2]
DAY_COLUMN_INDICES = [3, 7, 11, 15, 19, 23, 27]
WEEK_COLUMN_INDICES = [x + 1 for x in DAY_COLUMN_INDICES]
MONTH_COLUMN_INDICES = [x + 2 for x in DAY_COLUMN_INDICES]
QUARTER_COLUMN_INDICES = [x + 3 for x in DAY_COLUMN_INDICES]


def setup():
  # Kill all instances of blink.
  subprocess.call(["killall blink &> /dev/null"], shell=True)

  # Initialize timer.
  if not os.path.isdir(DIRECTORY_PATH):
    os.makedirs(DIRECTORY_PATH)

  endtime_filepath = os.path.join(DIRECTORY_PATH, ENDTIME_FILENAME)
  if not os.path.isfile(endtime_filepath):
    with open(endtime_filepath, 'w') as f:
      f.write(str(0.0))
      f.flush()

  # TODO(alive): if timer is already running; exit with error.


def cleanup():
  subprocess.call(["blink", "-q", "--off"])


def init_command():
  spreadsheet = data_util.Spreadsheet(SPREADSHEET_ID)
  time_worksheet = spreadsheet.GetWorksheet(WORKSHEET_ID)

  # Relevant ranges to fetch from time sheet.
  ranges = []
  ranges.append("A%d" % LAST_DAY_ROW_INDEX)  # Last date tracked.
  for x in WEEK_COLUMN_INDICES:  # Weekly columns needed to resize merges.
    ranges.append("R%dC%d" % (LAST_DAY_ROW_INDEX, x))
  for x in MONTH_COLUMN_INDICES:  # Monthly columns needed to resize merges.
    ranges.append("R%dC%d" % (LAST_DAY_ROW_INDEX, x))
  for x in QUARTER_COLUMN_INDICES: # Quarterly columns needed to resize merges.
    ranges.append("R%dC%d" % (LAST_DAY_ROW_INDEX, x))

  # Prepend sheet name to all ranges.
  ranges = ["%s!%s" % (WORKSHEET_NAME, x) for x in ranges]
  response = spreadsheet.GetRanges(ranges, fields="sheets(data,merges)")

  # Extract date information.
  data = response['sheets'][0]["data"]
  last_date_tracked_data = data[0]
  last_date_tracked_string = (
      last_date_tracked_data['rowData'][0]['values'][0]['formattedValue'])
  last_date_tracked = datetime.datetime.strptime(
      last_date_tracked_string, DATE_FORMAT).date()
  today = datetime.date.today()
  if today == last_date_tracked:
    click.echo("Timer sheet is already initialized for today.")
    return

  # Exctract cell merge information.
  week_merge_ranges = extract_merge_ranges(time_worksheet, response,
                                           WEEK_COLUMN_INDICES)
  month_merge_ranges = extract_merge_ranges(time_worksheet, response,
                                            MONTH_COLUMN_INDICES)
  quarter_merge_ranges = extract_merge_ranges(time_worksheet, response,
                                              QUARTER_COLUMN_INDICES)

  # Insert new days.
  init_requests = build_new_day_requests(
      time_worksheet, today, last_date_tracked,
      week_merge_ranges, month_merge_ranges, quarter_merge_ranges)

  # Update sheet wide statistics.
  init_requests += build_update_statistics_requests(time_worksheet)

  # Send requests.
  response = spreadsheet.BatchUpdate(init_requests)


def extract_merge_ranges(time_worksheet, response_data, column_indices):
  merges = response_data['sheets'][0].get("merges", [])
  merge_ranges = [x for i, x in enumerate(merges)
                  if x["endColumnIndex"] in column_indices]
  assert(not merge_ranges or len(merge_ranges) == len(column_indices))
  if not merge_ranges:
    merge_ranges += build_new_merge_ranges(time_worksheet, LAST_DAY_ROW_INDEX,
                                           column_indices)
  return merge_ranges


def build_new_merge_ranges(time_worksheet, row, column_indices):
  merge_ranges = []
  for i in column_indices:
    merge_ranges.append(time_worksheet.NewMergeRange(row, row, i, i))
  return merge_ranges


def build_new_day_requests(time_worksheet, today, last_date_tracked,
                           week_merge_ranges, month_merge_ranges,
                           quarter_merge_ranges):
  requests = []
  delta_days = (today - last_date_tracked).days

  # Insert new rows.
  requests.append(time_worksheet.NewInsertRowsBatchRequest(
      ROW_MARGIN + 1, delta_days))

  # Adjust merge ranges to account for newly inserted rows.
  for merge_range in (week_merge_ranges + month_merge_ranges +
                      quarter_merge_ranges):
    merge_range['startRowIndex'] += delta_days
    merge_range['endRowIndex'] += delta_days

  # Write dates into new rows.
  tmp_date = copy.deepcopy(last_date_tracked)
  while tmp_date != today:
    tmp_date += datetime.timedelta(1)
    row_index = LAST_DAY_ROW_INDEX + (today - tmp_date).days
    requests.append(time_worksheet.NewUpdateCellBatchRequest(
        row_index, 1, tmp_date.strftime(DATE_FORMAT)))

  # For today's row, write per-column zero counts and total count formula.
  total_count_formula = '='
  for i in DAY_COLUMN_INDICES:
    # Build total count formula.
    total_count_formula += "%s%d+" % (col_num_to_letter(i), row_index)
    requests.append(time_worksheet.NewUpdateCellBatchRequest(
        row_index, i, 0, UpdateCellsMode.number))  # Per-column zero counts.

  total_count_formula = total_count_formula[:-1]  # Strip trailing plus sign.
  requests.append(time_worksheet.NewUpdateCellBatchRequest(
      row_index, 2, total_count_formula, UpdateCellsMode.formula))

  # Deal with merges.
  requests += build_new_day_merge_requests(
      time_worksheet, today, last_date_tracked,
      week_merge_ranges, month_merge_ranges, quarter_merge_ranges)
  return requests


def build_new_day_merge_requests(time_worksheet, today, last_date_tracked,
                                 week_merge_ranges, month_merge_ranges,
                                 quarter_merge_ranges):
  requests = []
  tmp_date = copy.deepcopy(last_date_tracked)
  while tmp_date != today:
    row_index = LAST_DAY_ROW_INDEX + (today - tmp_date).days - 1
    tmp_next_date = tmp_date + datetime.timedelta(1)

    # Helper functions inside closure to avoid duplication of tedious code.
    def extend_merge_ranges(merge_ranges):
      """Helper function inside closure to avoid duplication of tedious code.
      """
      for merge_range in merge_ranges:
        merge_range["startRowIndex"] -= 1

    def close_merge_range_requests(merge_ranges, column_indices,
                                   sum_column_offset):
      range_obj = data_util.MergeRange(merge_ranges[0])
      for i in column_indices:
        requests.append(build_sum_formula_update(
            time_worksheet, range_obj.row_range[0], i,
            range_obj.row_range, i+sum_column_offset))
      while merge_ranges:
        # TODO: don't append if row span is equal to 1
        requests.append(time_worksheet.NewMergeCellsBatchRequest(
            merge_ranges.pop()))

    # Week column merges.
    if tmp_date.isocalendar()[1] == tmp_next_date.isocalendar()[1]:
      # Same week. Extend merge ranges on weekly columns.
      extend_merge_ranges(week_merge_ranges)
    else:
      # New week. Close out existing merge ranges.
      close_merge_range_requests(week_merge_ranges, WEEK_COLUMN_INDICES, -1)
      week_merge_ranges += build_new_merge_ranges(time_worksheet, row_index,
                                                  WEEK_COLUMN_INDICES)

    # Month column merges.
    if tmp_date.month == tmp_next_date.month:
      # Same month. Extend merge ranges on monthly columns.
      extend_merge_ranges(month_merge_ranges)
    else:
      # New month. Close out existing merge ranges.
      close_merge_range_requests(month_merge_ranges, MONTH_COLUMN_INDICES, -2)
      month_merge_ranges += build_new_merge_ranges(time_worksheet, row_index,
                                                   MONTH_COLUMN_INDICES)

    # Quarter column merges.
    if (tmp_date.month - 1) / 3 == (tmp_next_date.month - 1) / 3:
      # Same quarter. Extend merge ranges on quarterly columns.
      extend_merge_ranges(quarter_merge_ranges)
    else:
      # New quarter. Close out existing merge ranges.
      close_merge_range_requests(quarter_merge_ranges,
                                 QUARTER_COLUMN_INDICES, -3)
      quarter_merge_ranges += build_new_merge_ranges(time_worksheet, row_index,
                                                     QUARTER_COLUMN_INDICES)

    tmp_date = tmp_next_date

  close_merge_range_requests(week_merge_ranges, WEEK_COLUMN_INDICES, -1)
  close_merge_range_requests(month_merge_ranges, MONTH_COLUMN_INDICES, -2)
  close_merge_range_requests(quarter_merge_ranges, QUARTER_COLUMN_INDICES, -3)
  return requests


# Helper to build and append update formula requests to a list.
def build_sum_formula_update(time_worksheet, target_row, target_column,
                             sum_row_range, sum_column):
  sum_range = "%s%d:%s%d" % (
      col_num_to_letter(sum_column), sum_row_range[0],
      col_num_to_letter(sum_column), sum_row_range[1])
  return time_worksheet.NewUpdateCellBatchRequest(
      target_row, target_column, '=SUM(%s)' % sum_range,
      UpdateCellsMode.formula)


def build_update_statistics_requests(time_worksheet):
  requests = []
  cols_for_sums_update = ROW_STATS_COLUMN_INDICES + DAY_COLUMN_INDICES
  cols_for_other_stats_update = (ROW_STATS_COLUMN_INDICES + DAY_COLUMN_INDICES +
                                 WEEK_COLUMN_INDICES + MONTH_COLUMN_INDICES +
                                 QUARTER_COLUMN_INDICES)
  # Totals.
  for i in cols_for_sums_update:
    column_letter = col_num_to_letter(i)
    row_range = "%s%d:%s" % (column_letter, LAST_DAY_ROW_INDEX, column_letter)
    sum_formula = "=SUM(%s)" % row_range
    requests.append(time_worksheet.NewUpdateCellBatchRequest(
        row_index("TOTALS"), i, sum_formula, UpdateCellsMode.formula))

  # Other stats.
  for i in cols_for_other_stats_update:
    column_letter = col_num_to_letter(i)
    row_range = "%s%d:%s" % (column_letter, LAST_DAY_ROW_INDEX, column_letter)

    # Medians.
    median_formula = "=MEDIAN(%s)" % row_range
    requests.append(time_worksheet.NewUpdateCellBatchRequest(
        row_index("MEDIANS"), i, median_formula, UpdateCellsMode.formula))

    # Percentile.
    percentile_formula = "=PERCENTILE(%s, %f)" % (row_range, 0.90)
    requests.append(time_worksheet.NewUpdateCellBatchRequest(
        row_index("PERCENTILE"), i, percentile_formula,
        UpdateCellsMode.formula))

    # Max.
    max_formula = "=MAX(%s)" % row_range
    requests.append(time_worksheet.NewUpdateCellBatchRequest(
        row_index("MAX"), i, max_formula, UpdateCellsMode.formula))

  return requests


def start_command(label, seconds, minutes, hours, whitenoise, track, force):
  def sigint_handler(signum, frame):
    with open(timer_resource_path(ENDTIME_FILENAME), 'r') as f:
      endtime = float(f.read())

    with open(timer_resource_path(ENDTIME_FILENAME), 'w') as f:
      f.write(str(0.0))

    delta = endtime - time.time()
    if delta > 0.0:
      with open(timer_resume_filepath(label), 'w') as f:
        f.write(str(delta))

      click.echo("\n%s: Pausing timer at %d seconds." %
                 (datetime.datetime.strftime(datetime.datetime.now(), "%H:%M"),
                  delta))

      # TODO(alive): do not increment if track flag is false
      subprocess.call(["blink -q --rgb=0xff,0xa0,0x00 --blink=10 &"],
                      shell=True)
    unlock_timer()
    sys.exit(0)

  signal.signal(signal.SIGINT, sigint_handler)

  if not lock_timer():
    click.echo("%s: A timer is already running." %
               datetime.datetime.strftime(datetime.datetime.now(), "%H:%M"))
    return

  if not seconds and not minutes and not hours:
    seconds = FOCUS_UNIT_DURATION

  resume_filepath = timer_resume_filepath(label)
  if not force and os.path.isfile(resume_filepath):
    with open(resume_filepath, 'r') as f:
      delta = float(f.read())
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
      worksheet = walros_worksheet(WORKSHEET_NAME)
      latest_date = worksheet.cell(ROW_MARGIN + 1, 1).value
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
    unlock_timer()
    timer_notify()


def status_command(data):
  # Running timer.
  with open(timer_resource_path(ENDTIME_FILENAME), 'r') as f:
    delta = max(float(f.read()) - time.time(), 0.0)
    if delta > 0:
      click.echo("  current: %f" % delta)

  # Paused timers.
  for timer in timer_paused_filepaths():
    label = os.path.basename(timer[:timer.rfind(RESUME_FILE_SUFFIX)])
    with open(timer, 'r') as f:
      delta = float(f.read())
      click.echo("  %s: %f" % (label, delta))


def clear_command(label):
  if label:
    try:
      os.remove(timer_resource_path("%s%s" % (label, RESUME_FILE_SUFFIX)))
    except OSError:
      click.echo("No paused timer with label '%s' exists." % label)

  else:
    click.echo("Please specify a label to clear.")


def mod_command(mod_expression):
  click.echo(mod_expression)


# IAR: inc/dec commands?


def timer_notify():
  time_str = datetime.datetime.strftime(datetime.datetime.now(), "%H:%M")
  click.echo("%s: Notified" % time_str)
  subprocess.call(["blink -q --blink=20 &"], shell=True)
  subprocess.call(["osascript -e \'display notification " +
                   "\"%s: notify\" with title \"walrOS timer\"\'" % time_str],
                  shell=True)
  for ix in range(0, 3):
    subprocess.call(["afplay", "/System/Library/Sounds/Blow.aiff"])
    time.sleep(2)


def timer_resource_path(name):
  return os.path.join(DIRECTORY_PATH, name)


def timer_resume_filepath(label):
  resource_name = "%s%s" % (label, RESUME_FILE_SUFFIX)
  return timer_resource_path(resource_name)


def timer_paused_filepaths():
  filenames = ( f for f in os.listdir(DIRECTORY_PATH)
                if os.path.isfile(os.path.join(DIRECTORY_PATH, f)) )
  timer_filenames = ( f for f in filenames
                      if f.endswith(RESUME_FILE_SUFFIX))
  return itertools.imap(timer_resource_path, timer_filenames)


def timer_col_index_for_label(label):
  worksheet = walros_worksheet(WORKSHEET_NAME)
  row = worksheet.row_values(row_index("LABELS"))
  row_labels = row[COLUMN_MARGIN:]
  try:
    col_index = row_labels.index(label)
    col_index += COLUMN_MARGIN + 1
  except ValueError:
    raise click.ClickException("Label %s not found in spreadsheet." % label)

  return col_index


def timer_increment_label_count(label):
  worksheet = walros_worksheet(WORKSHEET_NAME)
  count_cell = worksheet.cell(ROW_MARGIN + 1,
                              timer_col_index_for_label(label))
  cell_value = 1 if not count_cell.value else int(count_cell.value) + 1
  count_cell.value = str(cell_value)
  worksheet.update_cells([count_cell])
  return cell_value


def col_num_to_letter(column_number):
  letter = ''
  while column_number > 0:
    tmp = (column_number - 1) % 26
    letter = chr(tmp + 65) + letter
    column_number = (column_number - tmp - 1) / 26
  return letter


def row_index(row_name):
  return HEADER_ROWS.index(row_name) + 1


def lock_timer():
  lock_filepath = timer_resource_path(LOCK_FILENAME)
  if os.path.isfile(lock_filepath):
    return False

  with open(lock_filepath, 'w') as f:
    f.flush()

  return True

def unlock_timer():
  lock_filepath = timer_resource_path(LOCK_FILENAME)
  if os.path.isfile(lock_filepath):
    os.remove(lock_filepath)


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

