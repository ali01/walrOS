import functools
import json
import os
import os.path
import sys
import time

import config
import util

_config = config.Config()
_TIMER_FILE_SUFFIX = '-timer'
_DIRECTORY_PATH = _config.timer_dir


def timer_exists(label):
  return os.path.isfile(_timer_filepath(label))


def existing_timers():
  filepaths = (f for f in os.listdir(_DIRECTORY_PATH)
               if os.path.isfile(os.path.join(_DIRECTORY_PATH, f)))
  timer_filenames = (f for f in filepaths if f.endswith(_TIMER_FILE_SUFFIX))
  timer_names = (f[:f.rfind(_TIMER_FILE_SUFFIX)] for f in timer_filenames)
  return (TimerFileProxy(t) for t in timer_names)


def running_timer():
  '''Returns the currently running timer or None if no timer is running.'''
  # TODO(alive): re-implement in terms of signals.
  running_timer = None
  for timer in existing_timers():
    with timer:
      if timer.is_running:
        # There should never be more than one running timer.
        assert running_timer is None
        running_timer = timer
  return running_timer


class _check_preconditions(object):
  def __init__(self, assert_running_is=None):
    self._assert_running_is = assert_running_is

  def __call__(self, method):
    @functools.wraps(method)
    def wrapper(method_self, *args, **kwargs):
      assert method_self._enter_called, (
          'TimerFileProxy must be used within a `with` statement.')
      if self._assert_running_is is not None:
        assert method_self.is_running == self._assert_running_is
      return method(method_self, *args, **kwargs)
    return wrapper


class TimerFileProxy(object):
  def __init__(self, label):
    self._label = label
    self._enter_called = False
    self._clear_called = False

  @property
  @_check_preconditions()
  def label(self):
    return self._label

  @property
  @_check_preconditions()
  def endtime(self):
    return self._timer_obj['endtime']

  @property
  @_check_preconditions()
  def remaining(self):
    if self.is_running:
      return int(round(self.endtime - time.time()))
    else:
      return self._timer_obj['remaining']

  @property
  @_check_preconditions()
  def filepath(self):
    return _timer_filepath(self._label)

  @property
  @_check_preconditions()
  def is_running(self):
    return not util.isclose(self.endtime, 0, abs_tol=1e-3)

  @property
  @_check_preconditions()
  def is_complete(self):
    return self.remaining <= 0

  @_check_preconditions(assert_running_is=False)
  def start(self, seconds, minutes, hours):
    duration = seconds + minutes * 60 + hours * 3600
    self._timer_obj['endtime'] = time.time() + duration

  @_check_preconditions(assert_running_is=False)
  def resume(self):
    self._timer_obj['endtime'] = int(round(time.time() + self.remaining))

  @_check_preconditions()
  def pause(self):
    if self.is_running:
      self._timer_obj['remaining'] = int(round(self.endtime - time.time()))
      self._timer_obj['endtime'] = 0
    return self.remaining

  @_check_preconditions()
  def clear(self):
    if self.is_running:
      self.pause()
    self._clear_called = True

  @_check_preconditions(assert_running_is=True)
  def inc(self, delta):
    self._timer_obj['endtime'] += delta

  def __enter__(self):
    # TODO(alive): Should likely hold the file lock throughout the entire `with`
    #              statement.
    self._enter_called = True
    if not os.path.isfile(self.filepath):
      self._timer_obj = {
        'label': self._label,
        'endtime': 0,  # This field is 0 when the timer is not running.
        'remaining': sys.maxint
      }
      with util.OpenAndLock(self.filepath, 'w') as f:
        f.write(util.json_dumps(self._timer_obj))
    else:
      with util.OpenAndLock(self.filepath, 'r') as f:
        self._timer_obj = json.load(f)
    return self

  def __exit__(self, *args):
    if self._clear_called:
      os.remove(self.filepath)  # Delete timer.
    else:
      with util.OpenAndLock(self.filepath, 'w') as f:
        f.write(util.json_dumps(self._timer_obj))
    self._enter_called = False


def _timer_filepath(label):
  return os.path.join(_DIRECTORY_PATH, label + _TIMER_FILE_SUFFIX)

