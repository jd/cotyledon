# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import collections
import contextlib
import errno
import fcntl
import logging
import os
import random
import select
import signal
import socket
import sys
import threading
import time
import uuid

import setproctitle

LOG = logging.getLogger(__name__)

SIGNAL_TO_NAME = dict((getattr(signal, name), name) for name in dir(signal)
                      if name.startswith("SIG") and name not in ('SIG_DFL',
                                                                 'SIG_IGN'))


class _ServiceConfig(object):
    def __init__(self, service_id, service, workers, args, kwargs):
        self.service = service
        self.workers = workers
        self.args = args
        self.kwargs = kwargs
        self.service_id = service_id


def _spawn(target):
    t = threading.Thread(target=target)
    t.daemon = True
    t.start()
    return t


def get_process_name():
    return os.path.basename(sys.argv[0])


@contextlib.contextmanager
def _exit_on_exception():
    try:
        yield
    except SystemExit as exc:
        os._exit(exc.code)
    except BaseException:
        LOG.exception('Unhandled exception')
        os._exit(2)


class Service(object):
    """Base class for a service

    This class will be executed in a new child process of a
    :py:class:`ServiceRunner`. It registers signals to manager the reloading
    and the ending of the process.

    Methods :py:meth:`run`, :py:meth:`terminate` and :py:meth:`reload` are
    optional.
    """

    name = None
    """Service name used in the process title and the log messages in additionnal
    of the worker_id."""

    graceful_shutdown_timeout = 60
    """Timeout after which a gracefully shutdown service will exit. zero means
    endless wait."""

    def __init__(self, worker_id):
        """Create a new Service

        :param worker_id: the identifier of this service instance
        :type worker_id: int
        """
        super(Service, self).__init__()
        self._initialize(worker_id)

    def _initialize(self, worker_id):
        if getattr(self, '_initialized', False):
            return
        self._initialized = True

        if self.name is None:
            self.name = self.__class__.__name__
        self.worker_id = worker_id
        self.pid = os.getpid()

        self._signal_lock = threading.Lock()

    def terminate(self):
        """Gracefully shutdown the service

        This method will be executed when the Service has to shutdown cleanly.

        If not implemented the process will just end with status 0.

        To customize the exit code, the :py:class:`SystemExit` exception can be
        used.

        """

    def reload(self):
        """Reloading of the service

        This method will be executed when the Service receives a SIGHUP.

        If not implemented the process will just end with status 0 and
        :py:class:`ServiceRunner` will start a new fresh process for this
        service with the same worker_id.
        """
        os.kill(os.getpid(), signal.SIGTERM)

    def run(self):
        """Method representing the service activity

        If not implemented the process will just wait to receive an ending
        signal.
        """

    # Helper to run application methods in a safety way when signal are
    # received

    def _reload(self):
        with _exit_on_exception():
            if self._signal_lock.acquire(False):
                try:
                    self.reload()
                finally:
                    self._signal_lock.release()

    def _terminate(self):
        with _exit_on_exception(), self._signal_lock:
            self.terminate()
            sys.exit(0)

    def _run(self):
        with _exit_on_exception():
            self.run()


class _SignalManager(object):
    def __init__(self, wakeup_interval=None):
        self._wakeup_interval = wakeup_interval
        # Setup signal fd, this allows signal to behave correctly
        self.signal_pipe_r, self.signal_pipe_w = os.pipe()
        self._set_nonblock(self.signal_pipe_r)
        self._set_nonblock(self.signal_pipe_w)
        signal.set_wakeup_fd(self.signal_pipe_w)

        self._signals_received = collections.deque()

        signal.signal(signal.SIGHUP, self._signal_catcher)
        signal.signal(signal.SIGTERM, self._signal_catcher)
        signal.signal(signal.SIGALRM, self._signal_catcher)

    @staticmethod
    def _set_nonblock(fd):
        flags = fcntl.fcntl(fd, fcntl.F_GETFL, 0)
        flags = flags | os.O_NONBLOCK
        fcntl.fcntl(fd, fcntl.F_SETFL, flags)

    def _signal_catcher(self, sig, frame):
        if sig in [signal.SIGALRM, signal.SIGTERM]:
            self._signals_received.appendleft(sig)
        else:
            self._signals_received.append(sig)

    def _wait_forever(self):
        # Wait forever
        while True:
            # Check if signals have been received
            self._empty_signal_pipe()
            self._run_signal_handlers()

            # Run Wakeup hook
            self._on_wakeup()

            # NOTE(sileht): we cannot use threading.Event().wait(),
            # threading.Thread().join(), or time.sleep() because signals
            # can be missed when received by non-main threads
            # (https://bugs.python.org/issue5315)
            # So we use select.select() alone, we will receive EINTR or will
            # read data from signal_r when signal is emitted and cpython calls
            # PyErr_CheckSignals() to run signals handlers That looks perfect
            # to ensure handlers are run and run in the main thread
            try:
                select.select([self.signal_pipe_r], [], [],
                              self._wakeup_interval)
            except select.error as e:
                if e.args[0] != errno.EINTR:
                    raise

    def _empty_signal_pipe(self):
        try:
            while os.read(self.signal_pipe_r, 4096) == 4096:
                pass
        except (IOError, OSError):
            pass

    def _run_signal_handlers(self):
        while True:
            try:
                sig = self._signals_received.popleft()
            except IndexError:
                return
            self._on_signal_received(sig)

    def _on_wakeup(self):
        pass

    def _on_signal_received(self, sig):
        pass


class _ChildProcess(_SignalManager):
    """This represent a child process

    All methods implemented here, must run in the main threads
    """

    def __init__(self, config, worker_id):
        super(_ChildProcess, self).__init__()

        # Initialize the service process
        args = tuple() if config.args is None else config.args
        kwargs = dict() if config.kwargs is None else config.kwargs
        self._service = config.service(worker_id, *args, **kwargs)
        self._service._initialize(worker_id)

        self.title = "%(name)s(%(worker_id)d) [%(pid)d]" % dict(
            name=self._service.name, worker_id=worker_id, pid=os.getpid())

        # Set process title
        setproctitle.setproctitle(
            "%(pname)s: %(name)s worker(%(worker_id)d)" % dict(
                pname=get_process_name(), name=self._service.name,
                worker_id=worker_id))

    def _on_signal_received(self, sig):
        # Code below must not block to return to select.select() and catch
        # next signals
        if sig == signal.SIGALRM:
            LOG.info('Graceful shutdown timeout (%d) exceeded, '
                     'exiting %s now.' %
                     (self._service.graceful_shutdown_timeout,
                      self.title))
            os._exit(1)

        elif sig == signal.SIGTERM:
            LOG.info('Caught SIGTERM signal, '
                     'graceful exiting of service %s' % self.title)
            if self._service.graceful_shutdown_timeout > 0:
                signal.alarm(self._service.graceful_shutdown_timeout)
            _spawn(self._service._terminate)
        elif sig == signal.SIGHUP:
            _spawn(self._service._reload)

    def wait_forever(self):
        # FIXME(sileht) useless public interface, application
        # can run threads themself.
        LOG.debug("Run service %s" % self.title)
        _spawn(self._service._run)
        super(_ChildProcess, self)._wait_forever()


class ServiceManager(_SignalManager):
    """Manage lifetimes of services

    :py:class:`ServiceManager` acts as a master process that controls the
    lifetime of children processes and restart them if they die unexpectedly.
    It also propagate some signals (SIGTERM, SIGALRM, SIGINT and SIGHUP) to
    them.

    Each child process runs an instance of a :py:class:`Service`.

    An application must create only one :py:class:`ServiceManager` class and
    use :py:meth:`ServiceManager.run()` as main loop of the application.



    Usage::

        class MyService(Service):
            def __init__(self, worker_id, myconf):
                super(MyService, self).__init__(worker_id)
                preparing_my_job(myconf)
                self.running = True

            def run(self):
                while self.running:
                    do_my_job()

            def terminate(self):
                self.running = False
                gracefully_stop_my_jobs()

            def reload(self):
                restart_my_job()

        conf = {'foobar': 2}
        sr = ServiceManager()
        sr.add(MyService, 5, conf)
        sr.run()

    This will create 5 children processes running the service MyService.

    """

    _process_runner_already_created = False

    def __init__(self, wait_interval=0.01):
        """Creates the ServiceManager object

        :param wait_interval: time between each new process spawn
        :type wait_interval: float

        """

        if self._process_runner_already_created:
            raise RuntimeError("Only one instance of ProcessRunner per "
                               "application is allowed")
        ServiceManager._process_runner_already_created = True
        super(ServiceManager, self).__init__(wait_interval)

        # We use OrderedDict to start services in adding order
        self._services = collections.OrderedDict()
        self._running_services = collections.defaultdict(dict)
        self._forktimes = []
        self._current_process = None

        setproctitle.setproctitle("%s: master process [%s]" %
                                  (get_process_name(), " ".join(sys.argv)))

        # Try to create a session id if possible
        try:
            os.setsid()
        except OSError:
            pass

        self.readpipe, self.writepipe = os.pipe()

        signal.signal(signal.SIGINT, self._fast_exit)

    def add(self, service, workers=1, args=None, kwargs=None):
        """Add a new service to the ServiceManager

        :param service: callable that return an instance of :py:class:`Service`
        :type service: callable
        :param workers: number of processes/workers for this service
        :type workers: int
        :param args: additional positional arguments for this service
        :type args: tuple
        :param kwargs: additional keywoard arguments for this service
        :type kwargs: dict

        :return: a service id
        :type return: uuid.uuid4
        """
        service_id = uuid.uuid4()
        self._services[service_id] = _ServiceConfig(service_id,
                                                    service, workers,
                                                    args, kwargs)
        return service_id

    def reconfigure(self, service_id, workers):
        """Reconfigure a service registered in ServiceManager

        :param service_id: the service id
        :type service_id: uuid.uuid4
        :param workers: number of processes/workers for this service
        :type workers: int
        """
        try:
            sc = self._services[service_id]
        except IndexError:
            raise ValueError("%s service id doesn't exists" % service_id)
        else:
            sc.workers = workers
            # Reset forktimes to respawn services quickly
            self._forktimes = []

    def run(self):
        """Start and supervise services

        This method will start and supervise all children processes
        until the master process asked to shutdown by a SIGTERM.

        All spawned processes are part of the same unix process group.
        """

        self._systemd_notify_once()
        self._wait_forever()

    def _on_wakeup(self):
        dead_pid = self._get_last_pid_died()
        while dead_pid is not None:
            self._restart_dead_worker(dead_pid)
            dead_pid = self._get_last_pid_died()
        self._adjust_workers()

    def _on_signal_received(self, sig):
        if sig == signal.SIGALRM:
            self._fast_exit(reason='Graceful shutdown timeout exceeded, '
                            'instantaneous exiting of master process')
        elif sig == signal.SIGTERM:
            self._shutdown()
        elif sig == signal.SIGHUP:
            self._reload()

    def _reload(self):
        # Reset forktimes to respawn services quickly
        self._forktimes = []
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        os.killpg(0, signal.SIGHUP)
        signal.signal(signal.SIGHUP, self._signal_catcher)

    def _shutdown(self):
        LOG.info('Caught SIGTERM signal, graceful exiting of master process')
        LOG.debug("Killing services with signal SIGTERM")
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.killpg(0, signal.SIGTERM)

        LOG.debug("Waiting services to terminate")
        # NOTE(sileht): We follow the termination of our children only
        # so we can't use waitpid(0, 0)
        for pids in self._running_services.values():
            for pid in pids:
                try:
                    os.waitpid(pid, 0)
                except OSError as e:
                    if e.errno == errno.ECHILD:
                        pass
                    else:
                        raise

        LOG.debug("Shutdown finish")
        sys.exit(0)

    def _adjust_workers(self):
        for service_id, conf in self._services.items():
            running_workers = len(self._running_services[service_id])
            if running_workers < conf.workers:
                for worker_id in range(running_workers, conf.workers):
                    self._start_worker(service_id, worker_id)
            elif running_workers > conf.workers:
                for worker_id in range(running_workers, conf.workers):
                    self._stop_worker(service_id, worker_id)

    def _restart_dead_worker(self, dead_pid):
        for service_id in self._running_services:
            service_info = list(self._running_services[service_id].items())
            for pid, worker_id in service_info:
                if pid == dead_pid:
                    del self._running_services[service_id][pid]
                    self._start_worker(service_id, worker_id)
                    return
        LOG.error('pid %d not in service known pids list', dead_pid)

    def _get_last_pid_died(self):
        """Return the last died service or None"""
        try:
            # Don't block if no child processes have exited
            pid, status = os.waitpid(0, os.WNOHANG)
            if not pid:
                return None
        except OSError as exc:
            if exc.errno not in (errno.EINTR, errno.ECHILD):
                raise
            return None

        if os.WIFSIGNALED(status):
            sig = SIGNAL_TO_NAME.get(os.WTERMSIG(status))
            LOG.info('Child %(pid)d killed by signal %(sig)s',
                     dict(pid=pid, sig=sig))
        else:
            code = os.WEXITSTATUS(status)
            LOG.info('Child %(pid)d exited with status %(code)d',
                     dict(pid=pid, code=code))
        return pid

    def _fast_exit(self, signo=None, frame=None,
                   reason='Caught SIGINT signal, instantaneous exiting'):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGALRM, signal.SIG_IGN)
        LOG.info(reason)
        os.killpg(0, signal.SIGINT)
        os._exit(1)

    def _slowdown_respawn_if_needed(self):
        # Limit ourselves to one process a second (over the period of
        # number of workers * 1 second). This will allow workers to
        # start up quickly but ensure we don't fork off children that
        # die instantly too quickly.

        expected_children = sum(s.workers for s in self._services.values())
        if len(self._forktimes) > expected_children:
            if time.time() - self._forktimes[0] < expected_children:
                LOG.info('Forking too fast, sleeping')
                time.sleep(1)
                self._forktimes.pop(0)
                self._forktimes.append(time.time())

    def _start_worker(self, service_id, worker_id):
        self._slowdown_respawn_if_needed()

        pid = os.fork()
        if pid != 0:
            self._running_services[service_id][pid] = worker_id
            return

        # reset parent signals
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGHUP, signal.SIG_DFL)

        # Close write to ensure only parent has it open
        os.close(self.writepipe)
        os.close(self.signal_pipe_r)
        os.close(self.signal_pipe_w)

        _spawn(self._watch_parent_process)

        # Reseed random number generator
        random.seed()

        # Create and run a new service
        with _exit_on_exception():
            self._current_process = _ChildProcess(self._services[service_id],
                                                  worker_id)
            self._current_process.wait_forever()

    def _stop_worker(self, service_id, worker_id):
        for pid, _id in self._running_services[service_id].items():
            if _id == worker_id:
                os.kill(pid, signal.SIGTERM)

    def _watch_parent_process(self):
        # NOTE(sileht): This is the only method that located in this class but
        # run into the child process. We do this to be able to stop the process
        # before the service have started.

        # This will block until the write end is closed when the parent
        # dies unexpectedly
        try:
            os.read(self.readpipe, 1)
        except EnvironmentError:
            pass

        # FIXME(sileht): accessing self._current_process is not really
        # thread-safe.
        if self._current_process is not None:
            LOG.info('Parent process has died unexpectedly, %s exiting'
                     % self._current_process.title)
            os.kill(os.getpid(), signal.SIGTERM)
        else:
            os._exit(0)

    @staticmethod
    def _systemd_notify_once():
        """Send notification once to Systemd that service is ready.

        Systemd sets NOTIFY_SOCKET environment variable with the name of the
        socket listening for notifications from services.
        This method removes the NOTIFY_SOCKET environment variable to ensure
        notification is sent only once.
        """

        notify_socket = os.getenv('NOTIFY_SOCKET')
        if notify_socket:
            if notify_socket.startswith('@'):
                # abstract namespace socket
                notify_socket = '\0%s' % notify_socket[1:]
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            with contextlib.closing(sock):
                try:
                    sock.connect(notify_socket)
                    sock.sendall(b'READY=1')
                    del os.environ['NOTIFY_SOCKET']
                except EnvironmentError:
                    LOG.debug("Systemd notification failed", exc_info=True)
