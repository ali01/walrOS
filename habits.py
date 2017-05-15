import walros_base
import data_util
from data_util import UpdateCellsMode

import click

WORKSHEET_NAME = "Habits"
WORKSHEET_ID = 751441428  # Found in URL.
HEADER_ROWS = [
  "TITLES",
  "COLUMN_LABELS",
  "MEDIANS",
  "PERCENTILE_75",
  "PERCENTILE_90",
  "MAX",
  "WEIGHTS",
  "GOAL_PERCENTILE",
  "GOAL_NUMBER",
  "PROGRESS",
]

# Margins
COLUMN_MARGIN = 5

# We currently assume that each day column is immediately followed
# by week, month, and quarter columns.
DAY_COLUMN_INDICES = range(2, 55, 4)

# Aggregate columns that are independently/manually set:
WEEK_COLUMN_INDICES = [ 7, 11, 15 ]
MONTH_COLUMN_INDICES = [ 8, 12, 16 ]
QUARTER_COLUMN_INDICES = [ 9, 13, 17 ]


def init_command():
  tracker_data = walros_base.TrackerData()
  tracker_data.worksheet_id = WORKSHEET_ID
  tracker_data.worksheet_name = WORKSHEET_NAME
  tracker_data.column_margin = COLUMN_MARGIN
  tracker_data.header_rows = HEADER_ROWS
  tracker_data.day_column_indices = DAY_COLUMN_INDICES
  tracker_data.week_column_indices = WEEK_COLUMN_INDICES
  tracker_data.month_column_indices = MONTH_COLUMN_INDICES
  tracker_data.quarter_column_indices = QUARTER_COLUMN_INDICES
  tracker_data.reduce_formula = (
      lambda r: "=IF(SUM(%s) = 0, 0, AVERAGE(%s))" % (r, r))
  tracker_data.init_writes_zeros = False

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
  # Build score formula.
  score_formula = '=SUM('
  weights_row_index = tracker_data.row_index("WEIGHTS")
  for i in tracker_data.day_column_indices[1:]:
    col = walros_base.col_num_to_letter(i)
    score_formula += "%s%d*%s%d," % (col, tracker_data.last_day_row_index,
                                     col, weights_row_index)
  score_formula += ")"

  # Normalize.
  score_formula += " / SUM("
  for i in tracker_data.day_column_indices[1:]:
    col = walros_base.col_num_to_letter(i)
    score_formula += "%s%d," % (col, weights_row_index)
  score_formula += ")"

  requests.append(worksheet.NewUpdateCellBatchRequest(
      tracker_data.last_day_row_index, 2, score_formula,
      UpdateCellsMode.formula))
  return requests
