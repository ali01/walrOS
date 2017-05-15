import datetime
import json
import os
import os.path
import time


import click

import config
import util


_config = config.Config()
_TIME_EPSILON = 1.0  # In seconds.


def setup():
  # Initialize task manager.
  if not os.path.isdir(_config.diary_dir):
    os.makedirs(_config.diary_dir)


def new_command(label):
  if os.path.isfile(_resource_path(label)):
    util.tlog("A diary entry with label `%s` already exists" % label)
    return
  now = time.time()
  entry = {
    'label': label,
    'epoch': now,
    'interval_start_time': now,
    'effective': 0.0,
  }
  with util.OpenAndLock(_resource_path(label), 'w') as f:
    f.write(util.json_dumps(entry))
  util.tlog("Diary entry with label `%s` created" % label)


def done_command(label):
  if not os.path.isfile(_resource_path(label)):
    util.tlog("No diary entry with label `%s` exists" % label)
    return

  with util.OpenAndLock(_resource_path(label), 'r') as f:
    entry = json.load(f)
    now = time.time()
    span = now - entry['epoch']
    effective = entry['effective']

    if util.isclose(effective, 0.0, abs_tol=_TIME_EPSILON):
      effective = span

    if util.isclose(span - effective, 0.0, abs_tol=_TIME_EPSILON):
      overhead = 0.0
    else:
      overhead = (span - effective) / span

    click.echo(" Start time:    %s" % _format_timestamp(entry['epoch']))
    click.echo(" End time:      %s" % _format_timestamp(now))
    click.echo(" Span (m):      %.2f" % (span / 60.0))
    click.echo(" Effective (m): %.2f" % (effective / 60.0))
    click.echo(" Overhead (%%):  %.1f%%" % (overhead * 100.0))

  os.remove(_resource_path(label))


def remove_command(label):
  if not os.path.isfile(_resource_path(label)):
    util.tlog("No diary entry with label `%s` exists" % label)
    return
  os.remove(_resource_path(label))


def status_command():
  util.tlog("Wawaaw... Not implemented yet :(")


class Entry(object):

  def __init__(self, label):
    self._label = label

  def __enter__(self):
    """Signals this module that the timer is running on the given label.

    If a diary entry for the given label exists, this function sets its
    interval_start_time to the current time.
    """
    # TODO(alive): rewrite with the paradigm used in timer_db.py.
    if os.path.isfile(_resource_path(self._label)):
      # TODO(alive): there's a harmless and unlikely race condition here.
      with util.OpenAndLock(_resource_path(self._label), 'r+') as f:
        entry = json.load(f)
        entry['interval_start_time'] = time.time()
        f.seek(0)
        f.truncate(0)
        f.write(util.json_dumps(entry))

    return self

  def __exit__(self, *args):
    """Signals this module that the timer is running on the given label.

    If a diary entry for the given label exists, this function increments its
    'effective' field by (time.time() - interval_start_time) and then resets
    interval_start_time.
    """
    if os.path.isfile(_resource_path(self._label)):
      # TODO(alive): there's a harmless and unlikely race condition here.
      with util.OpenAndLock(_resource_path(self._label), 'r+') as f:
        entry = json.load(f)
        entry['effective'] += time.time() - entry['interval_start_time']
        f.seek(0)
        f.truncate(0)
        f.write(util.json_dumps(entry))


def _resource_path(name):
  return os.path.join(_config.diary_dir, name)


def _format_timestamp(timestamp):
  datetime_obj = datetime.datetime.fromtimestamp(timestamp)
  return datetime.datetime.strftime(datetime_obj, "%H:%M:%S")
