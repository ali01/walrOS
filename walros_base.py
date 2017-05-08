import copy
import datetime

import click

import data_util
from data_util import UpdateCellsMode

SPREADSHEET_ID = "1JvO-sjs2kCFFD2FcX1a7XQ8uYyj-9o-anS9RElrtXYI"
DATE_FORMAT = "%Y-%m-%d %A"


class TrackerData(object):
  def __init__(self):
    self.worksheet_id = None  # Found in spreadsheet URL.
    self.worksheet_name = None
    self.column_margin = None
    self.header_rows = []
    self.day_column_indices = []
    self.week_column_indices = []
    self.month_column_indices = []
    self.quarter_column_indices = []
    self.reduce_formula = None
    self.init_writes_zeros = True

  @property
  def row_margin(self):
    return len(self.header_rows)

  @property
  def last_day_row_index(self):
    return self.row_margin + 1

  @property
  def week_merge_column_indices(self):
    return [ x + 1 for x in self.day_column_indices ]

  @property
  def month_merge_column_indices(self):
    return [ x + 2 for x in self.day_column_indices ]

  @property
  def quarter_merge_column_indices(self):
    return [ x + 3 for x in self.day_column_indices ]

  @property
  def all_column_indices(self):
    return (self.day_column_indices +
            self.week_merge_column_indices +
            self.month_merge_column_indices +
            self.quarter_merge_column_indices)

  @property
  def all_anchor_column_indices(self):
    return (self.day_column_indices +
            self.week_column_indices +
            self.month_column_indices +
            self.quarter_column_indices)

  @property
  def all_merge_column_indices(self):
    return (self.week_merge_column_indices +
            self.month_merge_column_indices +
            self.quarter_merge_column_indices)

  def row_index(self, row_name):
    return self.header_rows.index(row_name) + 1

  def reduce_column_offset(self, col_index):
    if col_index in self.all_anchor_column_indices:
      return 0

    if (col_index in [ x + 1 for x in self.day_column_indices ] or
        col_index in [ x + 1 for x in self.week_column_indices ] or
        col_index in [ x + 1 for x in self.month_column_indices ]):
      return -1

    if (col_index in [ x + 2 for x in self.day_column_indices ] or
        col_index in [ x + 2 for x in self.week_column_indices ]):
      return -2

    if col_index in [ x + 3 for x in self.day_column_indices ]:
      return -3


def build_init_requests(tracker_data, spreadsheet, worksheet):
  # Relevant ranges to fetch from time sheet.
  ranges = []
  ranges.append("A%d" % tracker_data.last_day_row_index)  # Last date tracked.

  for x in tracker_data.all_merge_column_indices:
    ranges.append("R%dC%d" % (tracker_data.last_day_row_index, x))

  # Prepend sheet name to all ranges.
  ranges = ["%s!%s" % (tracker_data.worksheet_name, x) for x in ranges]
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
    return []

  # Exctract cell merge information.
  week_merge_ranges = (
      extract_merge_ranges(worksheet, response,
                           tracker_data.week_merge_column_indices,
                           tracker_data.last_day_row_index))
  month_merge_ranges = (
      extract_merge_ranges(worksheet, response,
                           tracker_data.month_merge_column_indices,
                           tracker_data.last_day_row_index))
  quarter_merge_ranges = (
      extract_merge_ranges(worksheet, response,
                           tracker_data.quarter_merge_column_indices,
                           tracker_data.last_day_row_index))

  # Insert new days.
  init_requests = build_new_day_requests(
      tracker_data, worksheet, today, last_date_tracked,
      week_merge_ranges, month_merge_ranges, quarter_merge_ranges)

  return init_requests


def extract_merge_ranges(worksheet, response_data, column_indices,
                         last_day_row_index):
  merges = response_data['sheets'][0].get("merges", [])
  merge_ranges = [x for i, x in enumerate(merges)
                  if x["endColumnIndex"] in column_indices]
  assert(not merge_ranges or len(merge_ranges) == len(column_indices))
  if not merge_ranges:
    merge_ranges += build_new_merge_ranges(worksheet, last_day_row_index,
                                           column_indices)
  return merge_ranges


def build_new_merge_ranges(worksheet, row, column_indices):
  merge_ranges = []
  for i in column_indices:
    merge_ranges.append(worksheet.NewMergeRange(row, row, i, i))
  return merge_ranges


def build_new_day_requests(tracker_data, worksheet, today, last_date_tracked,
                           week_merge_ranges, month_merge_ranges,
                           quarter_merge_ranges):
  requests = []
  delta_days = (today - last_date_tracked).days

  # Insert new rows.
  requests.append(worksheet.NewInsertRowsBatchRequest(
      tracker_data.row_margin + 1, delta_days))

  # Adjust merge ranges to account for newly inserted rows.
  for merge_range in (week_merge_ranges + month_merge_ranges +
                      quarter_merge_ranges):
    merge_range['startRowIndex'] += delta_days
    merge_range['endRowIndex'] += delta_days

  # Write dates into new rows.
  tmp_date = copy.deepcopy(last_date_tracked)
  while tmp_date != today:
    tmp_date += datetime.timedelta(1)
    row_index = tracker_data.last_day_row_index + (today - tmp_date).days
    requests.append(worksheet.NewUpdateCellBatchRequest(
        row_index, 1, tmp_date.strftime(DATE_FORMAT)))

  # Deal with merges.
  requests += build_new_day_merge_requests(
      tracker_data, worksheet, today, last_date_tracked,
      week_merge_ranges, month_merge_ranges, quarter_merge_ranges)

  # For today's row, write per-column zero counts on anchor columns.
  if tracker_data.init_writes_zeros:
    for i in tracker_data.all_anchor_column_indices:
      requests.append(worksheet.NewUpdateCellBatchRequest(
          tracker_data.last_day_row_index, i, 0, UpdateCellsMode.number))
  return requests


def build_new_day_merge_requests(tracker_data, worksheet, today,
                                 last_date_tracked, week_merge_ranges,
                                 month_merge_ranges, quarter_merge_ranges):
  requests = []
  tmp_date = copy.deepcopy(last_date_tracked)

  # Helper functions inside closure to avoid duplication of tedious code.
  def extend_merge_ranges(merge_ranges):
    """Helper function inside closure to avoid duplication of tedious code.
    """
    for merge_range in merge_ranges:
      merge_range["startRowIndex"] -= 1

  def close_merge_range_requests(merge_ranges, column_indices):
    range_obj = data_util.MergeRange(merge_ranges[0])
    for i in column_indices:
      reduce_column_offset = tracker_data.reduce_column_offset(i)
      if reduce_column_offset != 0:  # Reduce only if non-anchor.
        requests.append(build_reduce_formula_update(
            tracker_data, worksheet, range_obj.row_range[0], i,
            range_obj.row_range, i+reduce_column_offset))
    while merge_ranges:
      # TODO: don't append if row span is equal to 1
      # TODO: return list instead of modifying external variable
      requests.append(worksheet.NewMergeCellsBatchRequest(
          merge_ranges.pop()))

  while tmp_date != today:
    row_index = tracker_data.last_day_row_index + (today - tmp_date).days - 1
    tmp_next_date = tmp_date + datetime.timedelta(1)

    # Week column merges.
    if tmp_date.isocalendar()[1] == tmp_next_date.isocalendar()[1]:
      # Same week. Extend merge ranges on weekly columns.
      extend_merge_ranges(week_merge_ranges)
    else:
      # New week. Close out existing merge ranges.
      close_merge_range_requests(week_merge_ranges,
                                 tracker_data.week_merge_column_indices)
      week_merge_ranges += (
          build_new_merge_ranges(worksheet, row_index,
                                 tracker_data.week_merge_column_indices))

    # Month column merges.
    if tmp_date.month == tmp_next_date.month:
      # Same month. Extend merge ranges on monthly columns.
      extend_merge_ranges(month_merge_ranges)
    else:
      # New month. Close out existing merge ranges.
      close_merge_range_requests(month_merge_ranges,
                                 tracker_data.month_merge_column_indices)
      month_merge_ranges += (
          build_new_merge_ranges(worksheet, row_index,
                                 tracker_data.month_merge_column_indices))

    # Quarter column merges.
    if (tmp_date.month - 1) / 3 == (tmp_next_date.month - 1) / 3:
      # Same quarter. Extend merge ranges on quarterly columns.
      extend_merge_ranges(quarter_merge_ranges)
    else:
      # New quarter. Close out existing merge ranges.
      close_merge_range_requests(quarter_merge_ranges,
                                 tracker_data.quarter_merge_column_indices)
      quarter_merge_ranges += (
          build_new_merge_ranges(worksheet, row_index,
                                 tracker_data.quarter_merge_column_indices))

    tmp_date = tmp_next_date

  close_merge_range_requests(week_merge_ranges,
                             tracker_data.week_merge_column_indices)
  close_merge_range_requests(month_merge_ranges,
                             tracker_data.month_merge_column_indices)
  close_merge_range_requests(quarter_merge_ranges,
                             tracker_data.quarter_merge_column_indices)
  return requests


# Helper to build and append update formula requests to a list.
def build_reduce_formula_update(tracker_data, worksheet,
                                target_row, target_column,
                                formula_row_range, formula_column):
  formula_range = "%s%d:%s%d" % (
      col_num_to_letter(formula_column), formula_row_range[0],
      col_num_to_letter(formula_column), formula_row_range[1])
  return worksheet.NewUpdateCellBatchRequest(
      target_row, target_column, tracker_data.reduce_formula(formula_range),
      UpdateCellsMode.formula)


def col_num_to_letter(column_number):
  letter = ''
  while column_number > 0:
    tmp = (column_number - 1) % 26
    letter = chr(tmp + 65) + letter
    column_number = (column_number - tmp - 1) / 26
  return letter
