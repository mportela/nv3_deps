# -*- coding: utf-8 -*-
"""
    celery.app.base
    ~~~~~~~~~~~~~~~

    Actual App instance implementation.

"""
from __future__ import absolute_import
from __future__ import with_statement

import warnings

from collections import deque
from contextlib import contextmanager
from copy import deepcopy
from functools import wraps
from threading import Lock

from billiard.util import register_after_fork
from kombu.clocks import LamportClock
from kombu.utils import cached_property

from celery import platforms
from celery.exceptions import AlwaysEagerIgnored
from celery.loaders import get_loader_cls
from celery.local import PromiseProxy, maybe_evaluate
from celery._state import _task_stack, _tls, get_current_app, _register_app
from celery.utils.functional import first
from celery.utils.imports import instantiate, symbol_by_name

from .annotations import prepare as prepare_annotations
from .builtins import shared_task, load_shared_tasks
from .defaults import DEFAULTS, find_deprecated_settings
from .registry import TaskRegistry
from .utils import AppPickler, Settings, bugreport, _unpickle_app


def _unpickle_appattr(reverse_name, args):
    """Given an attribute name and a list of args, gets
    the attribute from the current app and calls it."""
    return get_current_app()._rgetattr(reverse_name)(*args)


class Celery(object):
    Pickler = AppPickler

    SYSTEM = platforms.SYSTEM
    IS_OSX, IS_WINDOWS = platforms.IS_OSX, platforms.IS_WINDOWS

    amqp_cls = 'celery.app.amqp:AMQP'
    backend_cls = None
    events_cls = 'celery.events:Events'
    loader_cls = 'celery.loaders.app:AppLoader'
    log_cls = 'celery.app.log:Logging'
    control_cls = 'celery.app.control:Control'
    registry_cls = TaskRegistry
    _pool = None

    def __init__(self, main=None, loader=None, backend=None,
            amqp=None, events=None, log=None, control=None,
            set_as_current=True, accept_magic_kwargs=False,
            tasks=None, broker=None, include=None, **kwargs):
        self.clock = LamportClock()
        self.main = main
        self.amqp_cls = amqp or self.amqp_cls
        self.backend_cls = backend or self.backend_cls
        self.events_cls = events or self.events_cls
        self.loader_cls = loader or self.loader_cls
        self.log_cls = log or self.log_cls
        self.control_cls = control or self.control_cls
        self.set_as_current = set_as_current
        self.registry_cls = symbol_by_name(self.registry_cls)
        self.accept_magic_kwargs = accept_magic_kwargs

        self.finalized = False
        self._finalize_mutex = Lock()
        self._pending = deque()
        self._tasks = tasks
        if not isinstance(self._tasks, TaskRegistry):
            self._tasks = TaskRegistry(self._tasks or {})

        # these options are moved to the config to
        # simplify pickling of the app object.
        self._preconf = {}
        if broker:
            self._preconf['BROKER_URL'] = broker
        if include:
            self._preconf['CELERY_IMPORTS'] = include

        if self.set_as_current:
            self.set_current()
        self.on_init()
        _register_app(self)

    def set_current(self):
        _tls.current_app = self

    def on_init(self):
        """Optional callback called at init."""
        pass

    def start(self, argv=None):
        return instantiate('celery.bin.celery:CeleryCommand', app=self) \
                    .execute_from_commandline(argv)

    def worker_main(self, argv=None):
        return instantiate('celery.bin.celeryd:WorkerCommand', app=self) \
                    .execute_from_commandline(argv)

    def task(self, *args, **opts):
        """Creates new task class from any callable."""

        def inner_create_task_cls(shared=True, filter=None, **opts):

            def _create_task_cls(fun):
                if shared:
                    cons = lambda app: app._task_from_fun(fun, **opts)
                    cons.__name__ = fun.__name__
                    shared_task(cons)
                if self.accept_magic_kwargs:  # compat mode
                    task = self._task_from_fun(fun, **opts)
                    if filter:
                        task = filter(task)
                    return task

                # return a proxy object that is only evaluated when first used
                promise = PromiseProxy(self._task_from_fun, (fun, ), opts)
                self._pending.append(promise)
                if filter:
                    return filter(promise)
                return promise

            return _create_task_cls

        if len(args) == 1 and callable(args[0]):
            return inner_create_task_cls(**opts)(*args)
        return inner_create_task_cls(**opts)

    def _task_from_fun(self, fun, **options):
        base = options.pop('base', None) or self.Task

        T = type(fun.__name__, (base, ), dict({
                'app': self,
                'accept_magic_kwargs': False,
                'run': staticmethod(fun),
                '__doc__': fun.__doc__,
                '__module__': fun.__module__}, **options))()
        task = self._tasks[T.name]  # return global instance.
        task.bind(self)
        return task

    def finalize(self):
        with self._finalize_mutex:
            if not self.finalized:
                self.finalized = True
                load_shared_tasks(self)

                pending = self._pending
                while pending:
                    maybe_evaluate(pending.pop())

                for task in self._tasks.itervalues():
                    task.bind(self)

    def config_from_object(self, obj, silent=False):
        del(self.conf)
        return self.loader.config_from_object(obj, silent=silent)

    def config_from_envvar(self, variable_name, silent=False):
        del(self.conf)
        return self.loader.config_from_envvar(variable_name, silent=silent)

    def config_from_cmdline(self, argv, namespace='celery'):
        self.conf.update(self.loader.cmdline_config_parser(argv, namespace))

    def send_task(self, name, args=None, kwargs=None, countdown=None,
            eta=None, task_id=None, publisher=None, connection=None,
            result_cls=None, expires=None, queues=None, **options):
        if self.conf.CELERY_ALWAYS_EAGER:  # pragma: no cover
            warnings.warn(AlwaysEagerIgnored(
                'CELERY_ALWAYS_EAGER has no effect on send_task'))

        result_cls = result_cls or self.AsyncResult
        router = self.amqp.Router(queues)
        options.setdefault('compression',
                           self.conf.CELERY_MESSAGE_COMPRESSION)
        options = router.route(options, name, args, kwargs)
        with self.default_producer(publisher) as producer:
            return result_cls(producer.publish_task(name, args, kwargs,
                        task_id=task_id,
                        countdown=countdown, eta=eta,
                        expires=expires, **options))

    def connection(self, hostname=None, userid=None,
            password=None, virtual_host=None, port=None, ssl=None,
            insist=None, connect_timeout=None, transport=None,
            transport_options=None, **kwargs):
        conf = self.conf
        return self.amqp.Connection(
                    hostname or conf.BROKER_HOST,
                    userid or conf.BROKER_USER,
                    password or conf.BROKER_PASSWORD,
                    virtual_host or conf.BROKER_VHOST,
                    port or conf.BROKER_PORT,
                    transport=transport or conf.BROKER_TRANSPORT,
                    insist=self.either('BROKER_INSIST', insist),
                    ssl=self.either('BROKER_USE_SSL', ssl),
                    connect_timeout=self.either(
                                'BROKER_CONNECTION_TIMEOUT', connect_timeout),
                    transport_options=dict(conf.BROKER_TRANSPORT_OPTIONS,
                                           **transport_options or {}))
    broker_connection = connection

    @contextmanager
    def default_connection(self, connection=None, *args, **kwargs):
        if connection:
            yield connection
        else:
            with self.pool.acquire(block=True) as connection:
                yield connection

    @contextmanager
    def default_producer(self, producer=None):
        if producer:
            yield producer
        else:
            with self.amqp.producer_pool.acquire(block=True) as producer:
                yield producer

    def with_default_connection(self, fun):
        """With any function accepting a `connection`
        keyword argument, establishes a default connection if one is
        not already passed to it.

        Any automatically established connection will be closed after
        the function returns.

        **Deprecated**

        Use ``with app.default_connection(connection)`` instead.

        """
        @wraps(fun)
        def _inner(*args, **kwargs):
            connection = kwargs.pop('connection', None)
            with self.default_connection(connection) as c:
                return fun(*args, **dict(kwargs, connection=c))
        return _inner

    def prepare_config(self, c):
        """Prepare configuration before it is merged with the defaults."""
        return find_deprecated_settings(c)

    def now(self):
        return self.loader.now(utc=self.conf.CELERY_ENABLE_UTC)

    def mail_admins(self, subject, body, fail_silently=False):
        if self.conf.ADMINS:
            to = [admin_email for _, admin_email in self.conf.ADMINS]
            return self.loader.mail_admins(subject, body, fail_silently, to=to,
                                       sender=self.conf.SERVER_EMAIL,
                                       host=self.conf.EMAIL_HOST,
                                       port=self.conf.EMAIL_PORT,
                                       user=self.conf.EMAIL_HOST_USER,
                                       password=self.conf.EMAIL_HOST_PASSWORD,
                                       timeout=self.conf.EMAIL_TIMEOUT,
                                       use_ssl=self.conf.EMAIL_USE_SSL,
                                       use_tls=self.conf.EMAIL_USE_TLS)

    def select_queues(self, queues=None):
        return self.amqp.queues.select_subset(queues)

    def either(self, default_key, *values):
        """Fallback to the value of a configuration key if none of the
        `*values` are true."""
        return first(None, values) or self.conf.get(default_key)

    def bugreport(self):
        return bugreport(self)

    def _get_backend(self):
        from celery.backends import get_backend_by_url
        backend, url = get_backend_by_url(
                self.backend_cls or self.conf.CELERY_RESULT_BACKEND,
                self.loader)
        return backend(app=self, url=url)

    def _get_config(self):
        s = Settings({}, [self.prepare_config(self.loader.conf),
                             deepcopy(DEFAULTS)])
        if self._preconf:
            for key, value in self._preconf.iteritems():
                setattr(s, key, value)
        return s

    def _after_fork(self, obj_):
        if self._pool:
            self._pool.force_close_all()
            self._pool = None

    def create_task_cls(self):
        """Creates a base task class using default configuration
        taken from this app."""
        return self.subclass_with_self('celery.app.task:Task', name='Task',
                                       attribute='_app', abstract=True)

    def subclass_with_self(self, Class, name=None, attribute='app',
            reverse=None, **kw):
        """Subclass an app-compatible class by setting its app attribute
        to be this app instance.

        App-compatible means that the class has a class attribute that
        provides the default app it should use, e.g.
        ``class Foo: app = None``.

        :param Class: The app-compatible class to subclass.
        :keyword name: Custom name for the target class.
        :keyword attribute: Name of the attribute holding the app,
                            default is 'app'.

        """
        Class = symbol_by_name(Class)
        reverse = reverse if reverse else Class.__name__

        def __reduce__(self):
            return _unpickle_appattr, (reverse, self.__reduce_args__())

        attrs = dict({attribute: self}, __module__=Class.__module__,
                     __doc__=Class.__doc__, __reduce__=__reduce__, **kw)

        return type(name or Class.__name__, (Class, ), attrs)

    def _rgetattr(self, path):
        return reduce(getattr, [self] + path.split('.'))

    def __repr__(self):
        return '<%s %s:0x%x>' % (self.__class__.__name__,
                                 self.main or '__main__', id(self), )

    def __reduce__(self):
        # Reduce only pickles the configuration changes,
        # so the default configuration doesn't have to be passed
        # between processes.
        return (_unpickle_app, (self.__class__, self.Pickler)
                              + self.__reduce_args__())

    def __reduce_args__(self):
        return (self.main, self.conf.changes, self.loader_cls,
                self.backend_cls, self.amqp_cls, self.events_cls,
                self.log_cls, self.control_cls, self.accept_magic_kwargs)

    @cached_property
    def Worker(self):
        return self.subclass_with_self('celery.apps.worker:Worker')

    @cached_property
    def WorkController(self, **kwargs):
        return self.subclass_with_self('celery.worker:WorkController')

    @cached_property
    def Beat(self, **kwargs):
        return self.subclass_with_self('celery.apps.beat:Beat')

    @cached_property
    def TaskSet(self):
        return self.subclass_with_self('celery.task.sets:TaskSet')

    @cached_property
    def Task(self):
        return self.create_task_cls()

    @cached_property
    def annotations(self):
        return prepare_annotations(self.conf.CELERY_ANNOTATIONS)

    @cached_property
    def AsyncResult(self):
        return self.subclass_with_self('celery.result:AsyncResult')

    @cached_property
    def GroupResult(self):
        return self.subclass_with_self('celery.result:GroupResult')

    @cached_property
    def TaskSetResult(self):  # XXX compat
        return self.subclass_with_self('celery.result:TaskSetResult')

    @property
    def pool(self):
        if self._pool is None:
            register_after_fork(self, self._after_fork)
            limit = self.conf.BROKER_POOL_LIMIT
            self._pool = self.connection().Pool(limit=limit)
        return self._pool

    @property
    def current_task(self):
        return _task_stack.top

    @cached_property
    def amqp(self):
        return instantiate(self.amqp_cls, app=self)

    @cached_property
    def backend(self):
        return self._get_backend()

    @cached_property
    def conf(self):
        return self._get_config()

    @cached_property
    def control(self):
        return instantiate(self.control_cls, app=self)

    @cached_property
    def events(self):
        return instantiate(self.events_cls, app=self)

    @cached_property
    def loader(self):
        return get_loader_cls(self.loader_cls)(app=self)

    @cached_property
    def log(self):
        return instantiate(self.log_cls, app=self)

    @cached_property
    def tasks(self):
        self.finalize()
        return self._tasks
App = Celery  # compat
