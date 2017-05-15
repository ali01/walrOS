import datetime
import json
import os
import os.path
import time


import click

import config
import timer_db
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
  util.tlog("diary entry with label `%s` created" % label)


def done_command(label):
  # TODO(alive): Rewrite with the paradigm used in timer_db.py.
  #              Move this logic into Entry.
  if not os.path.isfile(_resource_path(label)):
    util.tlog("No diary entry with label `%s` exists" % label)
    return

  with util.OpenAndLock(_resource_path(label), 'r') as f:
    entry = json.load(f)

  now = time.time()
  span = now - entry['epoch']
  effective = entry['effective']

  # Handle orderings:
  #   1. __enter__, new, done, __exit__.
  #   2. new, done, __enter__, __exit__.
  #   3. __enter__, __exit__, new, done.
  #
  # If we are in any of the above orderings AND effective is 0.0, then we
  # simply set `effective` to `span`. In these cases, there is no interaction
  # between diary and timer.
  #
  # If, however, the first condition is True, but the second is false, then
  # we must be in case #1 above. The only way for `effective` to be non-zero
  # here is for the user to have called timer.inc(). This is only possible
  # if a timer is running, and therefore, cases #2 and #3 are ruled out. The
  # else block handles this case.
  if (util.isclose(entry['epoch'], entry['interval_start_time']) and
      util.isclose(effective, 0.0)):
    effective = span
  else:
    # Handle orderings:
    #   1. __enter__, new, done, __exit__ (with call to timer.inc()).
    #   5. new, __enter__, done, __exit__.
    # Capture the amount of time elapsed after __enter__.
    timer = timer_db.running_timer()
    if timer:
      with timer:
        if timer.label == label:
          effective += time.time() - entry['interval_start_time']

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
  # TODO(alive): implement
  util.tlog("Wawaaw... Not implemented yet :(")


# TODO(alive): move into Entry
def increment_effective(label, delta):
  if not os.path.isfile(_resource_path(label)):
    return False

  with util.OpenAndLock(_resource_path(label), 'r+') as f:
    entry = json.load(f)
    entry['effective'] += delta  # Can validly result in negative numbers.
    f.truncate(0)
    f.seek(0)
    f.write(util.json_dumps(entry))
  return True


class Entry(object):
  def __init__(self, label):
    self._label = label

  def __enter__(self):
    """Signals this module that the timer is running on the given label.

    If a diary entry for the given label exists, this function sets its
    interval_start_time to the current time.

    Possible interactions with timer:
      Trivial orderings (no interaction):
      In these cases, new and done track all elapsed time.
        1. new, done, __enter__, __exit__
        2. __enter__, __exit__, new, done
        3. __enter__, new, done, __exit__

      In this case, __enter__ and __exit__ track all elapsed time.
        4. new, __enter__, __exit__, done


      Tricky orderings:
        5. new, __enter__, done, __exit__
           In this case, done captures the amount of time elapsed after
           __enter__.

        6. __enter__, new, __exit__, done
           In this case, __exit__ captures the amount of time elapsed after new.
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
    'effective' field by (time.time() - interval_start_time).
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
