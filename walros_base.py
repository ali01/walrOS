import copy
import datetime

import click

import data_util
from data_util import memoize
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
    self.reduce_formula = None

  @property
  def row_margin(self):
    return len(self.header_rows)

  @property
  def last_day_row_index(self):
    return self.row_margin + 1

  @property
  def week_column_indices(self):
    return [ x + 1 for x in self.day_column_indices ]

  @property
  def month_column_indices(self):
    return [ x + 2 for x in self.day_column_indices ]

  @property
  def quarter_column_indices(self):
    return [ x + 3 for x in self.day_column_indices ]

  def row_index(self, row_name):
    return self.header_rows.index(row_name) + 1


def build_init_requests(tracker_data, spreadsheet, worksheet):
  # Relevant ranges to fetch from time sheet.
  ranges = []
  ranges.append("A%d" % tracker_data.last_day_row_index)  # Last date tracked.

  # Columns needed to resize merges.
  merge_columns = (tracker_data.week_column_indices +
                   tracker_data.month_column_indices +
                   tracker_data.quarter_column_indices)
  for x in merge_columns:
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
  week_merge_ranges = extract_merge_ranges(worksheet, response,
                                           tracker_data.week_column_indices,
                                           tracker_data.last_day_row_index)
  month_merge_ranges = extract_merge_ranges(worksheet, response,
                                            tracker_data.month_column_indices,
                                            tracker_data.last_day_row_index)
  quarter_merge_ranges = (
      extract_merge_ranges(worksheet, response,
                           tracker_data.quarter_column_indices,
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

  # For today's row, write per-column zero counts.
  for i in tracker_data.day_column_indices:
    # Build total count formula.
    requests.append(worksheet.NewUpdateCellBatchRequest(
        tracker_data.last_day_row_index, i, 0, UpdateCellsMode.number))

  # Deal with merges.
  requests += build_new_day_merge_requests(
      tracker_data, worksheet, today, last_date_tracked,
      week_merge_ranges, month_merge_ranges, quarter_merge_ranges)
  return requests


def build_new_day_merge_requests(tracker_data, worksheet, today,
                                 last_date_tracked, week_merge_ranges,
                                 month_merge_ranges, quarter_merge_ranges):
  requests = []
  tmp_date = copy.deepcopy(last_date_tracked)
  while tmp_date != today:
    row_index = tracker_data.last_day_row_index + (today - tmp_date).days - 1
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
        requests.append(build_reduce_formula_update(
            tracker_data, worksheet, range_obj.row_range[0], i,
            range_obj.row_range, i+sum_column_offset))
      while merge_ranges:
        # TODO: don't append if row span is equal to 1
        requests.append(worksheet.NewMergeCellsBatchRequest(
            merge_ranges.pop()))

    # Week column merges.
    if tmp_date.isocalendar()[1] == tmp_next_date.isocalendar()[1]:
      # Same week. Extend merge ranges on weekly columns.
      extend_merge_ranges(week_merge_ranges)
    else:
      # New week. Close out existing merge ranges.
      close_merge_range_requests(week_merge_ranges,
                                 tracker_data.week_column_indices, -1)
      week_merge_ranges += (
          build_new_merge_ranges(worksheet, row_index,
                                 tracker_data.week_column_indices))

    # Month column merges.
    if tmp_date.month == tmp_next_date.month:
      # Same month. Extend merge ranges on monthly columns.
      extend_merge_ranges(month_merge_ranges)
    else:
      # New month. Close out existing merge ranges.
      close_merge_range_requests(month_merge_ranges,
                                 tracker_data.month_column_indices, -2)
      month_merge_ranges += (
          build_new_merge_ranges(worksheet, row_index,
                                 tracker_data.month_column_indices))

    # Quarter column merges.
    if (tmp_date.month - 1) / 3 == (tmp_next_date.month - 1) / 3:
      # Same quarter. Extend merge ranges on quarterly columns.
      extend_merge_ranges(quarter_merge_ranges)
    else:
      # New quarter. Close out existing merge ranges.
      close_merge_range_requests(quarter_merge_ranges,
                                 tracker_data.quarter_column_indices, -3)
      quarter_merge_ranges += (
          build_new_merge_ranges(worksheet, row_index,
                                 tracker_data.quarter_column_indices))

    tmp_date = tmp_next_date

  close_merge_range_requests(week_merge_ranges,
                             tracker_data.week_column_indices, -1)
  close_merge_range_requests(month_merge_ranges,
                             tracker_data.month_column_indices, -2)
  close_merge_range_requests(quarter_merge_ranges,
                             tracker_data.quarter_column_indices, -3)
  return requests


# Helper to build and append update formula requests to a list.
def build_reduce_formula_update(tracker_data, worksheet,
                                target_row, target_column,
                                sum_row_range, sum_column):
  sum_range = "%s%d:%s%d" % (
      col_num_to_letter(sum_column), sum_row_range[0],
      col_num_to_letter(sum_column), sum_row_range[1])
  formula = '=%s(%s)' % (tracker_data.reduce_formula, sum_range)
  return worksheet.NewUpdateCellBatchRequest(
      target_row, target_column, formula,
      UpdateCellsMode.formula)


def col_num_to_letter(column_number):
  letter = ''
  while column_number > 0:
    tmp = (column_number - 1) % 26
    letter = chr(tmp + 65) + letter
    column_number = (column_number - tmp - 1) / 26
  return letter


def build_standard_update_statistics_requests(worksheet, tracker_data):
  requests = []
  cols_for_stats_update = (
      tracker_data.day_column_indices + tracker_data.week_column_indices +
      tracker_data.month_column_indices + tracker_data.quarter_column_indices)

  for i in cols_for_stats_update:
    column_letter = col_num_to_letter(i)
    row_range = "%s%d:%s" % (column_letter, tracker_data.last_day_row_index,
                             column_letter)

    # Medians.
    median_formula = "=MEDIAN(%s)" % row_range
    requests.append(worksheet.NewUpdateCellBatchRequest(
        tracker_data.row_index("MEDIANS"), i, median_formula,
        UpdateCellsMode.formula))

    # Percentile.
    percentile_formula = "=PERCENTILE(%s, %f)"
    requests.append(worksheet.NewUpdateCellBatchRequest(
        tracker_data.row_index("PERCENTILE_75"), i,
        percentile_formula % (row_range, 0.75),
        UpdateCellsMode.formula))
    requests.append(worksheet.NewUpdateCellBatchRequest(
        tracker_data.row_index("PERCENTILE_90"), i,
        percentile_formula % (row_range, 0.90),
        UpdateCellsMode.formula))

    # Max.
    max_formula = "=MAX(%s)" % row_range
    requests.append(worksheet.NewUpdateCellBatchRequest(
        tracker_data.row_index("MAX"), i, max_formula, UpdateCellsMode.formula))

  return requests
