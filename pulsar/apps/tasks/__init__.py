'''\
An asynchronous task-queue built on top :class:`pulsar.Application` framework.
By creating :class:`Job` classes in a similar way you can do for celery_,
this application gives you all you need for running them with very
little setup effort::

    from pulsar.apps import tasks

    tq = tasks.TaskQueue(tasks_path='path.to.tasks.*')
    tq.start()

.. _tasks-actions:

Tutorial
==============

Actions
~~~~~~~~~~~~~~~

The :class:`Taskqueue` application adds the following
:ref:`remote actions <api-remote_commands>` to its workers:

* **addtask** to add a new task to the task queue::

    send(taskqueue, 'addtask', jobname, task_extra, *args, **kwargs)

 * *jobname*: the name of the :class:`Job` to run.
 * *task_extra*: dictionary of extra parameters to pass to the :class:`Task`
   constructor. Usually a empty dictionary.
 * *args*: positional arguments for the :ref:`job callable <job-callable>`.
 * *kwargs*: key-valued arguments for the :ref:`job callable <job-callable>`.

* **addtask_noack** same as **addtask** but without acknowleding the sender::

    send(taskqueue, 'addtask_noack', jobname, task_extra, *args, **kwargs)
    
* **get_task** retrieve task information. This can be already executed or not.
  The implementation is left to the :meth:`Task.get_task` method::
  
    send(taskqueue, 'get_task', id)
    
* **get_tasks** retrieve information for tasks which satisfy the filtering.
  The implementation is left to the :meth:`Task.get_tasks` method::
  
    send(taskqueue, 'get_tasks', **filters)
  
    
Jobs
~~~~~~~~~~~~~~~~

An application implements several :class:`Job`
classes which specify the way each :class:`Task` is run.
Each job class is a task-factory, therefore, a task is always associated
with one job, which can be of two types:

* standard (:class:`Job`)
* periodic (:class:`PeriodicJob`)

.. _job-callable:

To define a job is simple, subclass from :class:`Job` and implement the
**job callable method**::

    from pulsar.apps import tasks

    class Addition(tasks.Job):

        def __call__(self, consumer, a, b):
            "Add two numbers"
            return a+b
            
    class Sampler(tasks.Job):

        def __call__(self, consumer, sample, size=10):
            ...

The *consumer*, instance of :class:`TaskConsumer`, is passed by the
:class:`TaskQueue` and should always be the first positional argument in the
callable function.
The remaining positional arguments and/or key-valued parameters are needed by
your job implementation.

Task Class
~~~~~~~~~~~~~~~~~

By default, tasks are constructed using an in-memory implementation of
:class:`Task`. To use a different implementation, for example one that
saves tasks on a database, subclass :class:`Task` and pass the new class
to the :class:`TaskQueue` constructor::

    from pulsar.apps import tasks

    class TaskDatabase(tasks.Task):

        def on_created(self):
            return save2db(self)

        def on_received(self):
            return save2db(self)

        def on_start(self):
            return save2db(self)

        def on_finish(self):
            return save2db(self)

        @classmethod
        def get_task(cls, id, remove = False):
            return taskfromdb(id)


    tq = tasks.TaskQueue(task_class=TaskDatabase, tasks_path='path.to.tasks.*')
    tq.start()


.. _tasks-callbacks:

Task callbacks
~~~~~~~~~~~~~~~~~~~

When creating your own :class:`Task` class all you need to override are the four
task callbacks:

* :meth:`Task.on_created` called by the taskqueue when it creates a new task
  instance.
* :meth:`Task.on_received` called by a worker when it receives the task.
* :meth:`Task.on_start` called by a worker when it starts the task.
* :meth:`Task.on_finish` called by a worker when it ends the task.


and :meth:`Task.get_task` classmethod for retrieving tasks instances.

.. _task-state:

Task states
~~~~~~~~~~~~~

A :class:`Task` can have one of the following :attr:`Task.status` string:

* ``PENDING`` A task waiting for execution and unknown.
* ``RETRY`` A task is retrying calculation.
* ``RECEIVED`` when the task is received by the task queue.
* ``STARTED`` task execution has started.
* ``REVOKED`` the task execution has been revoked. One possible reason could be
  the task has timed out.
* ``UNKNOWN`` task execution is unknown.
* ``FAILURE`` task execution has finished with failure.
* ``SUCCESS`` task execution has finished with success.


.. attribute:: FULL_RUN_STATES

    The set of states for which a :class:`Task` has run:
    ``FAILURE`` and ``SUCCESS``

.. attribute:: READY_STATES

    The set of states for which a :class:`Task` has finished:
    ``REVOKED``, ``FAILURE`` and ``SUCCESS``


Queue
~~~~~~~~~~~~~~

By default the queue is implemented using the multiprocessing.Queue
from the standard python library. To specify a different queue you can
use the ``task-queue`` flag from the command line::

    python myserverscript.py --task-queue dotted.path.to.callable

or by setting the ``task_queue_factory`` parameter in the config file
or in the :class:`TaskQueue` constructor.


.. _celery: http://celeryproject.org/
'''
import os
from datetime import datetime

import pulsar
from pulsar import to_string, maybe_async_deco, get_actor
from pulsar.utils.importer import import_modules, module_attribute

from .queue import *
from .exceptions import *
from .task import *
from .models import *
from .scheduler import Scheduler
from .states import *
from .rpc import *


class TaskQueueFactory(pulsar.Setting):
    app = 'cpubound'
    name = "task_queue_factory"
    section = "Task Consumer"
    flags = ["-q", "--task-queue"]
    default = "pulsar.apps.tasks.Queue"
    desc = """The task queue factory to use."""

    def get(self):
        return module_attribute(self.value)


class TaskSetting(pulsar.Setting):
    virtual = True
    app = 'tasks'


class TaskPath(TaskSetting):
    name = "tasks_path"
    section = "Task Consumer"
    meta = "STRING"
    validator = pulsar.validate_list
    cli = ["--tasks-path"]
    default = ['pulsar.apps.tasks.testing']
    desc = """\
        List of python dotted paths where tasks are located.
        """
                
                
class CPUboundServer(pulsar.Application):
    '''A CPU-bound application server, that is an application which
handle events with a task to complete and the time complete it is
determined principally by the speed of the CPU.
This type of application is served by :ref:`CPU bound workers <cpubound>`.'''
    _app_name = 'cpubound'
    cpu_bound_server = None
    
    def __init__(self, *args, **kwargs):
        self.received = 0
        self.concurrent_requests = set()
        super(CPUboundServer, self).__init__(*args, **kwargs)
        
    def io_poller(self, worker):
        self.local.queue = worker.params.ioqueue 
        return IOQueue(self.ioqueue, self)
    
    def can_poll(self):
        if self.local.can_poll:
            return len(self.concurrent_requests) <= self.cfg.backlog
    
    @property
    def concurrent_request(self):
        return len(self.concurrent_requests)
    
    @property
    def ioqueue(self):
        return self.local.queue
    
    def put(self, request):
        '''Put a *request* into the :attr:`ioqueue` if available.'''
        self.ioqueue.put(('request', request))

    def request_instance(self, request):
        '''Build a request class from a *request*. By default it returns the
request. This method is called by the :meth:`on_request` once a new
request has been obtained from the :attr:`ioqueue`.'''
        return request

    def worker_start(self, worker):
        # Set up the cpu bound worker by registering its file descriptor
        # and enabling polling from the queue
        worker.requestloop.add_reader('request', self.on_request, worker)
        self.local.can_poll = True
    
    def monitor_info(self, worker, data):
        tq = self.ioqueue
        if tq is not None:
            if isinstance(tq, Queue):
                tqs = 'multiprocessing.Queue'
            else:
                tqs = str(tq)
            try:
                size = tq.qsize()
            except NotImplementedError: #pragma    nocover
                size = 0
            data['queue'] = {'ioqueue': tqs, 'ioqueue_size': size}
        return data
    
    def actorparams(self, monitor, params):
        if 'queue' not in self.local:
            self.local.queue = self.cfg.task_queue_factory()
        params['ioqueue'] = self.ioqueue
        return params
    
    @maybe_async_deco
    def on_request(self, worker, request):
        request = self.request_instance(request)
        if request is not None:
            self.received += 1
            self.concurrent_requests.add(request)
            try:
                yield request.start(worker)
            finally:
                self.concurrent_requests.discard(request)
        

#################################################    TASKQUEUE COMMANDS
taskqueue_cmnds = set()

@pulsar.command(internal=True, commands_set=taskqueue_cmnds)
def addtask(client, actor, caller, jobname, task_extra, *args, **kwargs):
    kwargs.pop('ack', None)
    return actor.app._addtask(actor, caller, jobname, task_extra, True,
                              args, kwargs)

@pulsar.command(internal=True, ack=False, commands_set=taskqueue_cmnds)
def addtask_noack(client, actor, caller, jobname, task_extra, *args, **kwargs):
    kwargs.pop('ack', None)
    return actor.app._addtask(actor, caller, jobname, task_extra, False,
                              args, kwargs)

@pulsar.command(internal=True, commands_set=taskqueue_cmnds)
def save_task(client, actor, caller, task):
    #import time
    #time.sleep(0.1)
    return actor.app.scheduler.save_task(task)

@pulsar.command(internal=True, commands_set=taskqueue_cmnds)
def delete_tasks(client, actor, caller, ids):
    return actor.app.scheduler.delete_tasks(ids)

@pulsar.command(commands_set=taskqueue_cmnds)
def get_task(client, actor, id):
    return actor.app.scheduler.get_task(id)

@pulsar.command(commands_set=taskqueue_cmnds)
def get_tasks(client, actor, **parameters):
    return actor.app.scheduler.get_tasks(**parameters)

@pulsar.command(commands_set=taskqueue_cmnds)
def job_list(client, actor, jobnames=None):
    return list(actor.app.job_list(jobnames=jobnames))

@pulsar.command(commands_set=taskqueue_cmnds)
def next_scheduled(client, actor, jobnames=None):
    return actor.app.scheduler.next_scheduled(jobnames=jobnames)

@pulsar.command(commands_set=taskqueue_cmnds)
def wait_for_task(client, actor, id, timeout=3600):
    # wait for a task to finish for at most timeout seconds
    scheduler = actor.app.scheduler
    return scheduler.task_class.wait_for_task(scheduler, id, timeout)


class TaskQueue(CPUboundServer):
    '''A :class:`pulsar.CPUboundServer` for consuming
tasks and managing scheduling of tasks.

.. attribute:: registry

    Instance of a :class:`JobRegistry` containing all
    registered :class:`Job` instances.
'''
    _app_name = 'tasks'
    cfg_apps = ('cpubound',)
    cfg = {'timeout': '3600', 'backlog': 1}
    commands_set = taskqueue_cmnds
    task_class = TaskInMemory
    '''The :class:`Task` class for storing information about task execution.

Default: :class:`TaskInMemory`
'''
    '''The scheduler class. Default: :class:`Scheduler`.'''

    @property
    def scheduler(self):
        '''A :class:`Scheduler` which send task to the task queue and
produces of periodic tasks according to their schedule of execution.

At every event loop, the :class:`pulsar.ApplicationMonitor` running
the :class:`TaskQueue` application, invokes the :meth:`Scheduler.tick`
which check for tasks to be scheduled.

Check the :meth:`TaskQueue.monitor_task` callback
for implementation.'''
        return self.local.scheduler

    def request_instance(self, request):
        return self.scheduler.get_task(request)

    def monitor_task(self, monitor):
        '''Override the :meth:`pulsar.Application.monitor_task` callback
to check if the scheduler needs to perform a new run.'''
        s = self.scheduler
        if s:
            if s.next_run <= datetime.now():
                s.tick(monitor)

    def handler(self):
        # Load the application callable, the task consumer
        if self.callable:
            self.callable()
        import_modules(self.cfg.tasks_path)
        self.local.scheduler = Scheduler(self)
        return self

    def monitor_handler(self):
        return self.handler()

    def job_list(self, jobnames=None):
        return self.scheduler.job_list(jobnames=jobnames)

    @property
    def registry(self):
        global registry
        return registry

    # Internals
    def _addtask(self, monitor, caller, jobname, task_extra, ack, args, kwargs):
        task = self.scheduler.queue_task(monitor, jobname, args, kwargs,
                                         **task_extra)
        if ack:
            return task
