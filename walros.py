#!/usr/bin/env python
import traceback

import click

import habits as habits_module
import diary as diary_module
import timer as timer_module


@click.group()
def walros():
  pass


@walros.command()
@click.pass_context
def init(ctx):
  timer_module.init_command()
  habits_module.init_command()


# -- Timer --

@walros.group()
def timer():
  timer_module.setup()

@timer.command()
def init():
  timer_module.init_command()


@timer.command()
@click.argument("label")
@click.option("-s", "--seconds", default=0.0)
@click.option("-m", "--minutes", default=0.0)
@click.option("-h", "--hours", default=0.0)
@click.option("-w", "--whitenoise", is_flag=True)
@click.option("-d", "--diary", is_flag=True, default=False)
@click.option("-c", "--count", default=1)
@click.option("--track/--no-track", default=True)
@click.option("--force", is_flag=True)
def start(label, seconds, minutes, hours, whitenoise, diary, count, track,
          force):
  if diary:
    diary_module.new_command(label)
  timer_module.start_command(
      label, seconds, minutes, hours, whitenoise, count, track, force)


@timer.command()
@click.option("-d", "--data", is_flag=True)
def status(data):
  timer_module.status_command(data)


@timer.command()
@click.argument("label")
def clear(label):
  timer_module.clear_command(label)


@timer.command()
@click.argument("delta", type=float)
def inc(delta):
  timer_module.inc_command(delta)


@timer.command()
@click.argument("delta", type=float)
def dec(delta):
  timer_module.inc_command(-1 * delta)


# -- Diary --

@walros.group()
def diary():
  diary_module.setup()

@diary.command()
@click.argument("label")
def new(label):
  diary_module.new_command(label)

@diary.command()
@click.argument("label")
def done(label):
  diary_module.done_command(label)

@diary.command()
@click.argument("label")
def rm(label):
  diary_module.remove_command(label)

@diary.command()
def status():
  diary_module.status_command()


# -- Habits --

@walros.group()
def habits():
  pass

@habits.command()
def init():
  habits_module.init_command()


if __name__ == "__main__":
  try:
    walros()

  except Exception as ex:
    click.echo(traceback.format_exc())
