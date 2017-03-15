import fcntl

class OpenAndLock(object):
  def __init__(self, filepath, open_mode):
    self.filepath_ = filepath
    self.file_ = None
    self.open_mode_ = open_mode
    self.lock_mode_ = fcntl.LOCK_EX
    if self.open_mode_ == 'r':
      self.lock_mode_ = fcntl.LOCK_SH

  def __enter__(self):
    self.file_ = open(self.filepath_, self.open_mode_)
    fcntl.lockf(self.file_.fileno(), self.lock_mode_)
    return self.file_

  def __exit__(self, *args):
    self.file_.flush()
    fcntl.lockf(self.file_.fileno(), fcntl.LOCK_UN)
    self.file_.close()
