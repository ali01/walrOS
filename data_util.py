import os
import functools
from enum import Enum
import string

from apiclient import discovery
import httplib2
import oauth2client
import oauth2client.file
from oauth2client import client
from oauth2client import tools

# TODO: move to walros_base
APPLICATION_NAME = "walrOS"
PERMISSION_SCOPES = "https://www.googleapis.com/auth/spreadsheets"
CLIENT_SECRET_FILEPATH = "~/.walros/client_secret.json"

TEST_SPREADSHEET_ID = '1P_e-Tu-ZeY4fHluoMEmtg9p5pq7OLddoEEdhNqEvyVQ'
TEST_WORKSHEET_ID = 0

class Spreadsheet(object):

  def __init__(self, spreadsheet_id):
    self.spreadsheet_id_ = spreadsheet_id
    self.sheets_ = GetSpreadsheets()

  def GetWorksheet(self, worksheet_id):
    return Worksheet(self.spreadsheet_id_, worksheet_id)

  def GetRanges(self, ranges, fields):
    return self.sheets_.get(spreadsheetId=self.spreadsheet_id_,
                            includeGridData=False, ranges=ranges,
                            fields=fields).execute()

  def GetCellValue(self, worksheet_name, row, col):
    request = self.sheets_.values().get(
        spreadsheetId=self.spreadsheet_id_,
        range="%s!%s%d" % (worksheet_name, num2col(col), row))
    response = request.execute()
    return response["values"][0][0]

  def BatchUpdate(self, batch_requests):
    return self.sheets_.batchUpdate(spreadsheetId=self.spreadsheet_id_,
                                    body={'requests': batch_requests}).execute()

class Worksheet(object):

  def __init__(self, spreadsheet_id, worksheet_id):
    self.spreadsheet_id_ = spreadsheet_id
    self.worksheet_id_ = worksheet_id
    self.sheets_ = GetSpreadsheets()

  def NewInsertRowsBatchRequest(self, start_index, num_rows):
    return {
      'insertDimension': {
        'range': {
            'sheetId': self.worksheet_id_,
            'dimension': 'ROWS',
            'startIndex': start_index - 1,
            'endIndex': start_index + num_rows - 1,
        },
      },
    }

  def NewMergeRange(self, start_row, end_row, start_col, end_col):
    return {
      "startRowIndex": start_row - 1,
      "endRowIndex": end_row,
      "startColumnIndex": start_col - 1,
      "endColumnIndex": end_col,
      "sheetId": self.worksheet_id_,
    }

  def NewMergeCellsBatchRequest(self, merge_range):
    return {
      'mergeCells': {
        'mergeType': 'MERGE_ALL',
        'range': merge_range
      }
    }

  class UpdateCellsMode(Enum):
    string = 'stringValue'
    number = 'numberValue'
    formula = 'formulaValue'

  def NewUpdateCellBatchRequest(self, row, col, value,
                                update_cells_mode=UpdateCellsMode.string.value):
    return {
      'updateCells': {
        'fields': 'userEnteredValue',
        'start': {  # Zero-based indexing here.
          'rowIndex': row - 1,
          'columnIndex': col - 1,
          'sheetId': self.worksheet_id_,
        },
        'rows': [
          {
            'values': {
              'userEnteredValue': {
                update_cells_mode: value,
              },
            },
          },
        ],
      },
    }

# Expose at the top-level.
UpdateCellsMode = Worksheet.UpdateCellsMode


class MergeRange(object):
  def __init__(self, merge_range):
    self.row_range = (merge_range['startRowIndex'] + 1,
                      merge_range['endRowIndex'])
    self.col_range = (merge_range['startColumnIndex'] + 1,
                      merge_range['endColumnIndex'])

# -- Authentication --

def memoize(init_fn):
  """Decorator to memoize initialization"""
  obj = []
  @functools.wraps(init_fn)
  def wrapper_fn():
    if len(obj) == 0:
      obj.append(init_fn())
    return obj[0]

  return wrapper_fn


@memoize
def GetSpreadsheets():
  credentials = GetCredentials()
  http = credentials.authorize(httplib2.Http())
  discoveryUrl = ('https://sheets.googleapis.com/$discovery/rest?version=v4')
  service = discovery.build('sheets', 'v4', http=http,
                            discoveryServiceUrl=discoveryUrl,
                            num_retries=3)
  return service.spreadsheets()

def GetCredentials():
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
        flow = client.flow_from_clientsecrets(os.path.expanduser(
                                                  CLIENT_SECRET_FILEPATH),
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

# -- Helper Functions --

def col2num(col):
    num = 0
    for c in col:
        if c in string.ascii_letters:
            num = num * 26 + (ord(c.upper()) - ord('A')) + 1
    return num


def num2col(n):
    s = ""
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        s = chr(65 + remainder) + s
    return s


if __name__ == '__main__':
  sheet = Spreadsheet(TEST_SPREADSHEET_ID)
  worksheet = sheet.GetWorksheet(TEST_WORKSHEET_ID)

  requests = []
  requests.append(worksheet.NewUpdateCellBatchRequest(
      1, 1, 42, update_cells_mode=UpdateCellsMode.number.value))
  sheet.BatchUpdate(requests)

  print(sheet.GetCellValue("Sheet1", 1, 1))


