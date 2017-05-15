import json
import os

_CONFIG_FILEPATH = os.path.expanduser("~/.walros/config.json")

class Config(object):
  def __init__(self, filepath=_CONFIG_FILEPATH):
    with open(filepath) as f:
      self._config_obj = json.load(f)

  @property
  def base_dir(self):
    return os.path.expanduser(self._config_obj['base_dir'])

  @property
  def timer_dir(self):
    return os.path.join(self.base_dir,
                        self._config_obj['timer_subdir'])

  @property
  def diary_dir(self):
    return os.path.join(self.base_dir,
                        self._config_obj['diary_subdir'])
