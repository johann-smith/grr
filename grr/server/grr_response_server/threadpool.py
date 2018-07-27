#!/usr/bin/env python
"""A simple thread pool for the Google Response Rig.

This file defines a simple thread pool that is used throughout this
project for parallelizing data store accesses. This thread pool is
rather lightweight and optimized to be used in combination with the
GRR data_store modules. It is not meant to be general purpose - if you
need a generalized thread pool, you should probably use a better
suited alternative implementation.

If during creation not all new worker threads can be spawned by the
ThreadPool, a log entry will be written but execution will continue
using a smaller pool of workers. In this case, consider reducing the
--threadpool_size.

Example usage:
>>> def PrintMsg(value):
>>>   print "Message: %s" % value
>>> for _ in range(10):
>>>   SharedPool().AddTask(PrintMsg, ("Hello World!", ))
>>> SharedPool().Join()

"""
from __future__ import division

import itertools
import logging
import os
import Queue
import threading
import time


from builtins import range  # pylint: disable=redefined-builtin
from future.utils import itervalues
import psutil

from grr_response_core.lib import stats
from grr_response_core.lib import utils

STOP_MESSAGE = "Stop message"


class Error(Exception):
  pass


class DuplicateThreadpoolError(Error):
  """Raised when a thread pool with the same name already exists."""


class Full(Error):
  """Raised when the threadpool is full."""


class _WorkerThread(threading.Thread):
  """The workers used in the ThreadPool class."""

  def __init__(self, queue, pool):
    """Initializer.

    This creates a new worker object for the ThreadPool class.

    Args:
      queue: A Queue.Queue object that is used by the ThreadPool class to
          communicate with the workers. When a new task arrives, the ThreadPool
          notifies the workers by putting a message into this queue that has the
          format (target, args, name, queueing_time).

          target - A callable, the function to call.
          args - A tuple of positional arguments to target. Keyword arguments
                 are not supported.
          name - A name for this task. If None, it will be unique generated by
                 the threading library.
          queueing_time - The timestamp when this task was queued as returned by
                          time.time().

          Or, alternatively, the message in the queue can be STOP_MESSAGE
          which indicates that the worker should terminate.

      pool: The thread pool this worker belongs to.
    """
    super(_WorkerThread, self).__init__()
    if pool.name:
      self.name = pool.name + "-" + self.name

    self.pool = pool
    self._queue = queue
    self.daemon = True
    self.idle = True
    self.started = time.time()

  def ProcessTask(self, target, args, name, queueing_time):
    """Processes the tasks."""

    if self.pool.name:
      time_in_queue = time.time() - queueing_time
      stats.STATS.RecordEvent(self.pool.name + "_queueing_time", time_in_queue)

      start_time = time.time()
    try:
      target(*args)
    # We can't let a worker die because one of the tasks it has to process
    # throws an exception. Therefore, we catch every error that is
    # raised in the call to target().
    except Exception as e:  # pylint: disable=broad-except
      if self.pool.name:
        stats.STATS.IncrementCounter(self.pool.name + "_task_exceptions")
      logging.exception("Caught exception in worker thread (%s): %s", name,
                        str(e))

    if self.pool.name:
      total_time = time.time() - start_time
      stats.STATS.RecordEvent(self.pool.name + "_working_time", total_time)

  def _RemoveFromPool(self):
    """Remove ourselves from the pool.

    Returns:
      True if removal was possible, and False if it was not possible.
    """
    with self.pool.lock:
      # Keep a minimum number of threads in the pool.
      if len(self.pool) <= self.pool.min_threads:
        return False

      # Remove us from our pool.
      self.pool._RemoveWorker(self.name)  # pylint: disable=protected-access

      return True

  def run(self):
    """This overrides the Thread.run method.

    This method checks in an endless loop if new tasks are available
    in the queue and processes them.
    """
    while True:
      if self.pool.name:
        self.idle = True

      try:
        # Wait 60 seconds for a message, otherwise exit. This ensures that the
        # threadpool will be trimmed down when load is light.
        task = self._queue.get(timeout=60)

        if self.pool.name:
          self.idle = False

        try:
          # The pool told us to quit, likely because it is stopping.
          if task == STOP_MESSAGE:
            return

          self.ProcessTask(*task)
        finally:
          self._queue.task_done()

      except Queue.Empty:
        if self._RemoveFromPool():
          return

      # Try to trim old threads down when they get too old. This helps the
      # thread pool size to shrink, even when it is not idle (e.g. if it is CPU
      # bound) since threads are forced to exit, but new threads will not be
      # created if the utilization is too high - resulting in a reduction of
      # threadpool size under CPU load.
      if time.time() - self.started > 600 and self._RemoveFromPool():
        return


THREADPOOL = None


class ThreadPool(object):
  """A thread pool implementation.

  The thread pool starts with the minimum number of threads. As tasks are added,
  they are added to a queue and once this is full, more threads are added until
  we reach max_threads or this process's CPU utilization approaches 100%. Since
  Python uses a global lock (GIL) it is not possible for the interpreter to use
  more than 100% of a single core. Any additional threads actually reduce
  performance due to thread switching overheads. Therefore we ensure that the
  thread pool is not too loaded at any one time.

  When threads are idle longer than 60 seconds they automatically exit. This
  ensures that our memory footprint is reduced when load is light.

  Note that this class should not be instantiated directly, but the Factory
  should be used.
  """
  # A global dictionary of pools, keyed by pool name.
  POOLS = {}
  factory_lock = threading.Lock()

  @classmethod
  def Factory(cls, name, min_threads, max_threads=None, cpu_check=True):
    """Creates a new thread pool with the given name.

    If the thread pool of this name already exist, we just return the existing
    one. This allows us to have different pools with different characteristics
    used by different parts of the code, at the same time.

    Args:
      name: The name of the required pool.
      min_threads: The number of threads in the pool.
      max_threads: The maximum number of threads to grow the pool to. If not set
        we do not grow the pool.
      cpu_check: If false, don't check CPU load when adding new threads.

    Returns:
      A threadpool instance.
    """
    with cls.factory_lock:
      result = cls.POOLS.get(name)
      if result is None:
        cls.POOLS[name] = result = cls(
            name, min_threads, max_threads=max_threads, cpu_check=cpu_check)

      return result

  def __init__(self, name, min_threads, max_threads=None, cpu_check=True):
    """This creates a new thread pool using min_threads workers.

    Args:
      name: A prefix to identify this thread pool in the exported stats.
      min_threads: The minimum number of worker threads this pool should have.
      max_threads: The maximum number of threads to grow the pool to. If not set
        we do not grow the pool.
      cpu_check: If false, don't check CPU load when adding new threads.


    Raises:
      threading.ThreadError: If no threads can be spawned at all, ThreadError
                             will be raised.
      DuplicateThreadpoolError: This exception is raised if a thread pool with
                                the desired name already exists.
    """
    self.min_threads = min_threads
    if max_threads is None or max_threads < min_threads:
      max_threads = min_threads

    self.max_threads = max_threads
    self.cpu_check = cpu_check
    self._queue = Queue.Queue(maxsize=max_threads)
    self.name = name
    self.started = False
    self.process = psutil.Process(os.getpid())

    # A reference for all our workers. Keys are thread names, and values are the
    # _WorkerThread instance.
    self._workers = {}
    # Read-only copy of self._workers that is thread-safe for reading.
    self._workers_ro_copy = {}
    self.lock = threading.RLock()

    if self.name:
      if self.name in self.POOLS:
        raise DuplicateThreadpoolError(
            "A thread pool with the name %s already exists.", name)

      stats.STATS.RegisterGaugeMetric(self.name + "_outstanding_tasks", int)
      stats.STATS.SetGaugeCallback(self.name + "_outstanding_tasks",
                                   self._queue.qsize)

      stats.STATS.RegisterGaugeMetric(self.name + "_threads", int)
      stats.STATS.SetGaugeCallback(self.name + "_threads", lambda: len(self))

      stats.STATS.RegisterGaugeMetric(self.name + "_cpu_use", float)
      stats.STATS.SetGaugeCallback(self.name + "_cpu_use", self.CPUUsage)

      stats.STATS.RegisterCounterMetric(self.name + "_task_exceptions")
      stats.STATS.RegisterEventMetric(self.name + "_working_time")
      stats.STATS.RegisterEventMetric(self.name + "_queueing_time")

  def __del__(self):
    if self.started:
      self.Stop()

  @property
  def pending_tasks(self):
    # This is thread safe as self._queue is thread safe.
    return self._queue.qsize()

  @property
  def busy_threads(self):
    return len([x for x in itervalues(self._workers_ro_copy) if not x.idle])

  def __len__(self):
    return len(self._workers_ro_copy)

  @utils.Synchronized
  def Start(self):
    """This starts the worker threads."""
    if not self.started:
      self.started = True
      for _ in range(self.min_threads):
        self._AddWorker()

  @utils.Synchronized
  def _AddWorker(self):
    worker = _WorkerThread(self._queue, self)
    worker.start()

    self._workers[worker.name] = worker
    self._workers_ro_copy = self._workers.copy()

  @utils.Synchronized
  def _RemoveWorker(self, key):
    self._workers.pop(key, None)
    self._workers_ro_copy = self._workers.copy()

  @utils.Synchronized
  def Stop(self):
    """This stops all the worker threads."""
    if not self.started:
      logging.warning("Tried to stop a thread pool that was not running.")
      return

    # Remove all workers from the pool.
    workers = list(itervalues(self._workers))
    self._workers = {}
    self._workers_ro_copy = {}

    # Send a stop message to all the workers.
    for _ in workers:
      self._queue.put(STOP_MESSAGE)

    self.started = False
    self.Join()

    # Wait for the threads to all exit now.
    for worker in workers:
      worker.join()

  def AddTask(self,
              target,
              args=(),
              name="Unnamed task",
              blocking=True,
              inline=True):
    """Adds a task to be processed later.

    Args:
      target: A callable which should be processed by one of the workers.

      args: A tuple of arguments to target.

      name: The name of this task. Used to identify tasks in the log.

      blocking: If True we block until the task is finished, otherwise we raise
        Queue.Full

      inline: If set, process the task inline when the queue is full. This
        implies no blocking. Specifying inline helps if the worker tasks are
        blocked because it still ensures some progress is made. However, this
        can generally block the calling thread even after the threadpool is
        available again and therefore decrease efficiency.

    Raises:
      Full() if the pool is full and can not accept new jobs.
    """
    # This pool should have no worker threads - just run the task inline.
    if self.max_threads == 0:
      target(*args)
      return

    if inline:
      blocking = False

    with self.lock:
      while True:
        try:
          # Push the task on the queue but raise if unsuccessful.
          self._queue.put((target, args, name, time.time()), block=False)
          return
        except Queue.Full:
          # We increase the number of active threads if we do not exceed the
          # maximum _and_ our process CPU utilization is not too high. This
          # ensures that if the workers are waiting on IO we add more workers,
          # but we do not waste workers when tasks are CPU bound.
          if len(self) < self.max_threads and self.CPUUsage() < 90:
            try:
              self._AddWorker()
              continue

            # If we fail to add a worker we should keep going anyway.
            except (RuntimeError, threading.ThreadError):
              logging.error("Threadpool exception: "
                            "Could not spawn worker threads.")

          # If we need to process the task inline just break out of the loop,
          # therefore releasing the lock and run the task inline.
          if inline:
            break

          # We should block and try again soon.
          elif blocking:
            try:
              self._queue.put(
                  (target, args, name, time.time()), block=True, timeout=1)
              return
            except Queue.Full:
              continue

          else:
            raise Full()

    # We don't want to hold the lock while running the task inline
    if inline:
      target(*args)

  def CPUUsage(self):
    if self.cpu_check:
      # Do not block this call.
      return self.process.cpu_percent(0)
    else:
      return 0

  def Join(self):
    """Waits until all outstanding tasks are completed."""
    self._queue.join()


class MockThreadPool(object):
  """A mock thread pool which runs all jobs serially."""

  def __init__(self, name, min_threads, max_threads=None, ignore_errors=True):
    _ = name
    _ = min_threads
    _ = max_threads
    self.ignore_errors = ignore_errors

  def AddTask(self, target, args, name="Unnamed task"):
    _ = name
    try:
      target(*args)
      # The real threadpool can not raise from a task. We emulate this here.
    except Exception as e:  # pylint: disable=broad-except
      logging.exception("MockThreadPool worker raised %s", e)
      if not self.ignore_errors:
        raise

  @classmethod
  def Factory(cls, name, min_threads, max_threads=None):
    return cls(name, min_threads, max_threads=max_threads)

  def Start(self):
    pass

  def Stop(self):
    pass

  def Join(self):
    pass


class BatchConverter(object):
  """Generic class that does multi-threaded values conversion.

  BatchConverter converts a set of values to a set of different values in
  batches using a threadpool.
  """

  def __init__(self,
               batch_size=1000,
               threadpool_prefix="batch_processor",
               threadpool_size=10):
    """BatchProcessor constructor.

    Args:
      batch_size: All the values will be processed in batches of this size.
      threadpool_prefix: Prefix that will be used in thread pool's threads
                         names.
      threadpool_size: Size of a thread pool that will be used.
                       If threadpool_size is 0, no threads will be used
                       and all conversions will be done in the current
                       thread.
    """
    super(BatchConverter, self).__init__()
    self.batch_size = batch_size
    self.threadpool_prefix = threadpool_prefix
    self.threadpool_size = threadpool_size

  def ConvertBatch(self, batch):
    """ConvertBatch is called for every batch to do the conversion.

    Args:
      batch: Batch to convert.
    Returns:
      List with converted values.
    """
    raise NotImplementedError()

  def Convert(self, values, start_index=0, end_index=None):
    """Converts given collection to exported values.

    This method uses a threadpool to do the conversion in parallel. It
    blocks until everything is converted.

    Args:
      values: Iterable object with values to convert.
      start_index: Start from this index in the collection.
      end_index: Finish processing on the (index - 1) element of the
                 collection. If None, work till the end of the collection.

    Returns:
      Nothing. ConvertedBatch() should handle the results.
    """
    if not values:
      return

    try:
      total_batch_count = len(values) // self.batch_size
    except TypeError:
      total_batch_count = -1

    pool = ThreadPool.Factory(self.threadpool_prefix, self.threadpool_size)
    val_iterator = itertools.islice(values, start_index, end_index)

    pool.Start()
    try:
      for batch_index, batch in enumerate(
          utils.Grouper(val_iterator, self.batch_size)):
        logging.debug("Processing batch %d out of %d", batch_index,
                      total_batch_count)

        pool.AddTask(
            target=self.ConvertBatch,
            args=(batch,),
            name="batch_%d" % batch_index,
            inline=False)

    finally:
      pool.Stop()
