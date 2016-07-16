#!/usr/bin/env python

from datetime import datetime
import httplib2
import time
import itertools
import json
import os
import os.path
import signal
import subprocess
import sys

import click

# gData API
from apiclient import discovery
import oauth2client
from oauth2client import client
from oauth2client import tools

# gspread
# TODO(alive): move away from this
import gspread
from oauth2client.service_account import ServiceAccountCredentials


APPLICATION_NAME = "walrOS"
SPREADSHEET_ID = "1JvO-sjs2kCFFD2FcX1a7XQ8uYyj-9o-anS9RElrtXYI"
PERMISSION_SCOPES = "https://www.googleapis.com/auth/spreadsheets"
CLIENT_SECRET_FILEPATH = "./.walros/client_secret.json"

FOCUS_UNIT_DURATION = 1800  # Seconds (30 minutes).
SPREADSHEET_KEY_FILEPATH = os.path.expanduser("~/.walros/keys.json")

TIMER_DIRECTORY_PATH = os.path.expanduser("~/.walros/timer")
TIMER_ENDTIME_FILENAME = "endtime"
TIMER_RESUME_FILE_SUFFIX = "-paused"
TIMER_SHEET_NAME = "Time"
TIMER_SHEET_ID = 925912296  # Found in URL.
TIMER_SHEET_ROW_MARGIN = 4
TIMER_SHEET_LABEL_ROW_INDEX = 2
TIMER_SHEET_COLUMN_MARGIN = 2
TIMER_SHEET_DATE_FORMAT = "%Y-%m-%d %A"

TIMER_SHEET_ROW_STATS_COLUMN = 2
TIMER_SHEET_RUNNING_SUMS_ROW = 3
TIMER_SHEET_AVERAGES_ROW = 4

TIMER_SHEET_DAY_COLUMN_INDICES = [3, 6, 7, 10]

# The following tuples are of the following form (indices are inclusive):
#   (<weekly_col>, (<start_sum_col>, <end_sum_col>))
TIMER_SHEET_WEEK_COLUMN_INDICES = [
  (4, (3, 3)),  # Weekly learning column.
  (8, (6, 7)),  # Weekly code and team columns.
  (11, (10, 10))  # Weekly metawork column.
]

# The following tuples are of the following form (indices are inclusive):
#   (<monthly_col>, (<start_sum_col>, <end_sum_col>))
TIMER_SHEET_MONTH_COLUMN_INDICES = [
  (5, (3, 3)),  # Monthly learning column.
  (9, (6, 7)),  # Monthly code and team columns.
  (12, (10, 10))  # Monthly metawork column.
]



@click.group()
def walros():
  pass


# -- Init --

@walros.command()
def init():
  spreadsheets = get_spreadsheets()

  # -- Time Sheet --
  # Get all necessary state.

  last_row_index = TIMER_SHEET_ROW_MARGIN + 1
  week_col_indices = [x[0] for x in TIMER_SHEET_WEEK_COLUMN_INDICES]
  month_col_indices = [x[0] for x in TIMER_SHEET_MONTH_COLUMN_INDICES]

  # Relevant ranges to fetch from time sheet.
  ranges = []
  ranges.append("A%d" % last_row_index)  # Last date tracked.
  for x in week_col_indices:  # Weekly columns needed to resize merges.
    ranges.append("R%dC%d" % (last_row_index, x))
  for x in month_col_indices:  # Monthly columns needed to resize merges.
    ranges.append("R%dC%d" % (last_row_index, x))

  # Prepend sheet name.
  ranges = ["%s!%s" % (TIMER_SHEET_NAME, x) for x in ranges]
  request = spreadsheets.get(spreadsheetId=SPREADSHEET_ID,
                             includeGridData=False,
                             ranges=ranges,
                             fields="sheets(data,merges)")
  response = request.execute()

  # Data.
  data = response['sheets'][0]["data"]
  last_date_tracked_data = data[0]
  last_date_tracked_string = (
      last_date_tracked_data['rowData'][0]['values'][0]['formattedValue'])
  last_date_tracked = datetime.strptime(last_date_tracked_string,
                                        TIMER_SHEET_DATE_FORMAT)
  today = datetime.now()
  if last_date_tracked.date() == today.date():
    print "Already initialized."
    return


  # Merges.
  merges = response['sheets'][0].get("merges", [])
  week_merge_ranges = [x for i, x in enumerate(merges)
                       if x["endColumnIndex"] in week_col_indices]
  month_merge_ranges = [x for i, x in enumerate(merges)
                        if x["endColumnIndex"] in month_col_indices]
  assert(len(week_merge_ranges) == 0 or
         len(week_merge_ranges) == len(week_col_indices))
  assert(len(month_merge_ranges) == 0 or
         len(month_merge_ranges) == len(month_col_indices))
  if len(week_merge_ranges) == 0:  # Populate with ranges of size one.
    for i in week_col_indices:
      append_merge_range(week_merge_ranges,
                         last_row_index, last_row_index, i, i)
  if len(month_merge_ranges) == 0:  # Populate with ranges of size one.
    for i in month_col_indices:
      append_merge_range(month_merge_ranges,
                         last_row_index, last_row_index, i, i)

  post_requests = []
  post_requests.append({  # Insert new row for today.
    'insertDimension': {
      'range': {
          'sheetId': TIMER_SHEET_ID,
          'dimension': 'ROWS',
          'startIndex': 4,
          'endIndex': 5,
      },
    },
  })

  if last_date_tracked.isocalendar()[1] == today.isocalendar()[1]:
    # Same week. Merge rows on weekly columns.
    for merge in week_merge_ranges:
      merge["endRowIndex"] += 1
      append_merge(post_requests, merge)
  else:
    print "New week!"
    for merge in week_merge_ranges:
      merge["endRowIndex"] = last_row_index

  if last_date_tracked.month == today.month:
    # Same month. Merge rows on monthly columns.
    for merge in month_merge_ranges:
      merge["endRowIndex"] += 1
      append_merge(post_requests, merge)
  else:
    print "New month!"
    for merge in month_merge_ranges:
      merge["endRowIndex"] = last_row_index

  # Compute new merge ranges (indices are all inclusive).
  week_merge_row_range = (last_row_index, week_merge_ranges[0]["endRowIndex"])
  month_merge_row_range = (last_row_index, month_merge_ranges[0]["endRowIndex"])

  for x in TIMER_SHEET_WEEK_COLUMN_INDICES:
    append_sum_formula_update(post_requests, last_row_index, x[0],
                              week_merge_row_range, x[1])
  for x in TIMER_SHEET_MONTH_COLUMN_INDICES:
    append_sum_formula_update(post_requests, last_row_index, x[0],
                              month_merge_row_range, x[1])

  # Insert today's date into the new row.
  append_set(post_requests, last_row_index, 1,
             today.strftime(TIMER_SHEET_DATE_FORMAT), 'stringValue')

  total_count_formula = '='
  for i in TIMER_SHEET_DAY_COLUMN_INDICES:
    total_count_formula += "%s%d+" % (col_num_to_letter(i), last_row_index)

    # Insert zero counts in new day cells.
    append_set(post_requests, last_row_index, i, 0, 'numberValue')

  # Insert today's total count formula into new row.
  total_count_formula = total_count_formula[:-1]  # Strip final plus sign.
  append_set(post_requests, last_row_index, 2, total_count_formula,
             'formulaValue')

  cols_for_sums_update = ([TIMER_SHEET_ROW_STATS_COLUMN] +
                          TIMER_SHEET_DAY_COLUMN_INDICES)
  cols_for_averages_update = ([TIMER_SHEET_ROW_STATS_COLUMN] +
                              TIMER_SHEET_DAY_COLUMN_INDICES +
                              week_col_indices + month_col_indices)
  for i in cols_for_sums_update:
    column_letter = col_num_to_letter(i)
    row_range = "%s%d:%s" % (column_letter, last_row_index, column_letter)
    sum_formula = "=SUM(%s)" % row_range
    append_set(post_requests, TIMER_SHEET_RUNNING_SUMS_ROW, i,
               sum_formula, 'formulaValue')
  for i in cols_for_averages_update:
    column_letter = col_num_to_letter(i)
    row_range = "%s%d:%s" % (column_letter, last_row_index, column_letter)
    average_formula = "=AVERAGE(%s)" % row_range
    append_set(post_requests, TIMER_SHEET_AVERAGES_ROW, i,
               average_formula, 'formulaValue')

  # Send request.
  request = spreadsheets.batchUpdate(spreadsheetId=SPREADSHEET_ID,
                                     body={'requests': post_requests})
  response = request.execute()


def append_set(lst, row, col, value, mode):
  lst.append({
    'updateCells': {
      'fields': 'userEnteredValue',
      'start': {  # Zero-based indexing here.
        'rowIndex': row - 1,
        'columnIndex': col - 1,
        'sheetId': TIMER_SHEET_ID
      },
      'rows': [
        {
          'values': {
            'userEnteredValue': {
              mode: value
            }
          }
        }
      ]
    }
  })


def col_num_to_letter(column_number):
  letter = ''
  while column_number > 0:
    tmp = (column_number - 1) % 26
    letter = chr(tmp + 65) + letter
    column_number = (column_number - tmp - 1) / 26
  return letter


# Helper to build and append merge requests to a list.
def append_merge(lst, merge):
  lst.append({
    'mergeCells': {
      'mergeType': 'MERGE_ALL',
      'range': merge
    }
  })

# Helper to build and append merge ranges to a list.
def append_merge_range(lst, start_row, end_row, start_col, end_col):
  lst.append({
    "startRowIndex": start_row - 1,
    "endRowIndex": end_row,
    "startColumnIndex": start_col - 1,
    "endColumnIndex": end_col,
    "sheetId": TIMER_SHEET_ID
  })

# Helper to build and append update formula requests to a list.
def append_sum_formula_update(lst, target_row, target_column,
                              sum_row_range, sum_column_range):
  sum_range = "%s%d:%s%d" % (
      col_num_to_letter(sum_column_range[0]), sum_row_range[0],
      col_num_to_letter(sum_column_range[1]), sum_row_range[1])
  append_set(lst, target_row, target_column, '=SUM(%s)' % sum_range,
             'formulaValue')


# -- Timer --

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
      worksheet = walros_worksheet(TIMER_SHEET_NAME)
      latest_date = worksheet.cell(TIMER_SHEET_ROW_MARGIN + 1, 1).value
      latest_date = latest_date.split()[0]
      date_today = datetime.now().strftime("%Y-%m-%d")
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
  click.echo("Notified at %s" % datetime.strftime(datetime.now(), "%H:%M"))
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
  worksheet = walros_worksheet(TIMER_SHEET_NAME)
  row = worksheet.row_values(TIMER_SHEET_LABEL_ROW_INDEX)
  row_labels = row[TIMER_SHEET_COLUMN_MARGIN:]
  try:
    col_index = row_labels.index(label)
    col_index += TIMER_SHEET_COLUMN_MARGIN + 1
  except ValueError:
    raise click.ClickException("Label %s not found in spreadsheet." % label)

  return col_index


def timer_increment_label_count(label):
  worksheet = walros_worksheet(TIMER_SHEET_NAME)
  count_cell = worksheet.cell(TIMER_SHEET_ROW_MARGIN + 1,
                              timer_col_index_for_label(label))
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

# -- gData API --
# Eventually migrate all gspread calls to gData calls.

def get_credentials():
    """Gets valid user credentials from storage.

    If nothing has been stored, or if the stored credentials are invalid,
    the OAuth2 flow is run to obtain the new credentials.

    Returns:
        The obtained credentials.
    """
    credential_dir = os.path.join(os.path.expanduser('~'), '.credentials')
    if not os.path.exists(credential_dir):
        os.makedirs(credential_dir)
    credential_path = os.path.join(credential_dir,
                                   'sheets.googleapis.com-walros.json')

    store = oauth2client.file.Storage(credential_path)
    credentials = store.get()
    if not credentials or credentials.invalid:
        flow = client.flow_from_clientsecrets(CLIENT_SECRET_FILEPATH,
                                              PERMISSION_SCOPES)
        flow.user_agent = APPLICATION_NAME

        import argparse
        flags_namespace = argparse.Namespace()
        setattr(flags_namespace, 'auth_host_name', 'localhost')
        setattr(flags_namespace, 'logging_level', 'ERROR')
        setattr(flags_namespace, 'noauth_local_webserver', False)
        setattr(flags_namespace, 'auth_host_port', [8080, 8090])
        credentials = tools.run_flow(flow, store, flags_namespace)
        print('Storing credentials to ' + credential_path)
    return credentials

def get_spreadsheets():
  credentials = get_credentials()
  http = credentials.authorize(httplib2.Http())
  discoveryUrl = ('https://sheets.googleapis.com/$discovery/rest?version=v4')
  service = discovery.build('sheets', 'v4', http=http,
                            discoveryServiceUrl=discoveryUrl)
  return service.spreadsheets()


def cleanup():
  # TODO(alive): write blink wrapper
  subprocess.call(["blink", "-q", "--off"])


if __name__ == "__main__":
  subprocess.call(["killall blink &> /dev/null"], shell=True)
  walros()
  cleanup()
