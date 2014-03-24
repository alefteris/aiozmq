"""ZeroMQ RPC"""

import abc
import asyncio
import builtins
import os
import random
import struct
import time
import sys

import msgpack
import zmq

from functools import partial
from types import MethodType

from . import interface
from .log import logger
from .util import _Packer, _Unpacker


__all__ = [
    'method',
    'open_client',
    'start_server',
    'Error',
    'GenericError',
    'NotFoundError',
    'AbstractHandler',
    'AttrHandler'
    ]


class Error(Exception):
    """Base RPC exception"""


class GenericError(Error):
    """Error used for all untranslated exceptions from rpc method calls."""

    def __init__(self, exc_type, args):
        super().__init__(exc_type, args)
        self.exc_type = exc_type
        self.arguments = args


class NotFoundError(Error):
    """Error raised by server if RPC namespace/method lookup failed."""


class AbstractHandler(metaclass=abc.ABCMeta):
    """Abstract class for server-side RPC handlers."""

    __slots__ = ()

    @abc.abstractmethod
    def __getitem__(self, key):
        raise KeyError

    @classmethod
    def __subclasshook__(cls, C):
        if cls is AbstractHandler:
            if any("__getitem__" in B.__dict__ for B in C.__mro__):
                return True
        return NotImplemented


class AttrHandler(AbstractHandler):
    """Base class for RPC handlers via attribute lookup."""

    def __getitem__(self, key):
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError


def method(func):
    """Marks method as RPC endpoint handler.

    Also validates function params using annotations.
    """
    # TODO: fun with flag;
    #       parse annotations and create(?) checker;
    #       (also validate annotations);
    func.__rpc__ = {}  # TODO: assign to trafaret?
    return func


@asyncio.coroutine
def open_client(*, connect=None, bind=None, loop=None):
    """A coroutine that creates and connects/binds RPC client.

    Return value is a client instance.
    """
    # TODO: describe params
    # TODO: add a way to pass exception translator
    # TODO: add a way to pass value translator
    if loop is None:
        loop = asyncio.get_event_loop()

    transp, proto = yield from loop.create_zmq_connection(
        lambda: _ClientProtocol(loop), zmq.DEALER, connect=connect, bind=bind)
    return RPCClient(loop, proto)


@asyncio.coroutine
def start_server(handler, *, connect=None, bind=None, loop=None):
    """A coroutine that creates and connects/binds RPC server instance."""
    # TODO: describe params
    # TODO: add a way to pass value translator
    if loop is None:
        loop = asyncio.get_event_loop()

    transp, proto = yield from loop.create_zmq_connection(
        lambda: _ServerProtocol(loop, handler),
        zmq.ROUTER, connect=connect, bind=bind)

    return _RPCServer(loop, proto)


class _BaseProtocol(interface.ZmqProtocol):

    def __init__(self, loop):
        self.loop = loop
        self.transport = None
        self.done_waiters = []
        self.packer = _Packer()
        self.unpacker = _Unpacker()

    def connection_made(self, transport):
        self.transport = transport

    def connection_lost(self, exc):
        self.transport = None
        for waiter in self.done_waiters:
            waiter.set_result(None)


class _RPCServer(asyncio.AbstractServer):

    def __init__(self, loop, proto):
        self._loop = loop
        self._proto = proto

    def close(self):
        if self._proto.transport is None:
            return
        self._proto.transport.close()

    @asyncio.coroutine
    def wait_closed(self):
        if self._proto.transport is None:
            return
        waiter = asyncio.Future(loop=self._loop)
        self._proto.done_waiters.append(waiter)
        yield from waiter


class _ClientProtocol(_BaseProtocol):
    """Client protocol implementation."""

    REQ_PREFIX = struct.Struct('=HH')
    REQ_SUFFIX = struct.Struct('=Ld')
    RESP = struct.Struct('=HHLd?')

    def __init__(self, loop):
        super().__init__(loop)
        self.calls = {}
        self.prefix = self.REQ_PREFIX.pack(os.getpid() % 0x10000,
                                           random.randrange(0x10000))
        self.counter = 0
        self.error_table = self._fill_error_table()

    def _fill_error_table(self):
        # Fill error table with standard exceptions
        error_table = {}
        for name in dir(builtins):
            val = getattr(builtins, name)
            if isinstance(val, type) and issubclass(val, Exception):
                error_table['builtins.'+name] = val
        error_table[__name__ + '.GenericError'] = GenericError
        error_table[__name__ + '.NotFoundError'] = NotFoundError
        return error_table

    def msg_received(self, data):
        try:
            header, banswer = data
            pid, rnd, req_id, timestamp, is_error = self.RESP.unpack(header)
            self.unpacker.feed(banswer)
            answer = self.unpacker.unpack()
        except Exception:
            logger.critical("Cannot unpack %r", data, exc_info=sys.exc_info())
            return
        call = self.calls.pop(req_id, None)
        if call is None:
            logger.critical("Unknown answer id: %d (%d %d %f %d) -> %s",
                            req_id, pid, rnd, timestamp, is_error, answer)
            return
        if is_error:
            call.set_exception(self._translate_error(*answer))
        else:
            call.set_result(answer)

    def _translate_error(self, exc_type, exc_args):
        found = self.error_table.get(exc_type)
        if found is None:
            return GenericError(exc_type, tuple(exc_args))
        else:
            return found(*exc_args)

    def _new_id(self):
        self.counter += 1
        if self.counter > 0xffffffff:
            self.counter = 0
        return (self.prefix + self.REQ_SUFFIX.pack(self.counter, time.time()),
                self.counter)

    def call(self, name, args, kwargs):
        bname = name.encode('utf-8')
        bargs = self.packer.pack(args)
        bkwargs = self.packer.pack(kwargs)
        header, req_id = self._new_id()
        assert req_id not in self.calls, (req_id, self.calls)
        fut = asyncio.Future(loop=self.loop)
        self.calls[req_id] = fut
        self.transport.write([header, bname, bargs, bkwargs])
        return fut


class RPCClient(_RPCServer):

    def __init__(self, loop, proto):
        super().__init__(loop, proto)

    @property
    def rpc(self):
        """Return object for dynamic RPC calls.

        The usage is:
        ret = yield from client.rpc.ns.func(1, 2)
        """
        return _MethodCall(self._proto)


class _MethodCall:

    __slots__ = ('_proto', '_names')

    def __init__(self, proto, names=()):
        self._proto = proto
        self._names = names

    def __getattr__(self, name):
        return self.__class__(self._proto, self._names + (name,))

    def __call__(self, *args, **kwargs):
        if not self._names:
            raise ValueError('RPC method name is empty')
        return self._proto.call('.'.join(self._names), args, kwargs)


class _ServerProtocol(_BaseProtocol):

    REQ = struct.Struct('=HHLd')
    RESP_PREFIX = struct.Struct('=HH')
    RESP_SUFFIX = struct.Struct('=Ld?')

    def __init__(self, loop, handler):
        super().__init__(loop)
        self.prepare_handler(handler)
        self.handler = handler
        self.prefix = self.RESP_PREFIX.pack(os.getpid() % 0x10000,
                                            random.randrange(0x10000))

    def prepare_handler(self, handler):
        # TODO: check handler and subhandlers for correctness
        # raise exception if needed
        pass

    def msg_received(self, data):
        peer, header, bname, bargs, bkwargs = data
        pid, rnd, req_id, timestamp = self.REQ.unpack(header)

        # TODO: send exception back to transport if lookup is failed
        try:
            func = self.dispatch(bname.decode('utf-8'))
        except NotFoundError as exc:
            fut = asyncio.Future(loop=self.loop)
            fut.add_done_callback(partial(self.process_call_result,
                                          req_id=req_id, peer=peer))
            fut.set_exception(exc)
        else:
            self.unpacker.feed(bargs)
            args = self.unpacker.unpack()
            self.unpacker.feed(bkwargs)
            kwargs = self.unpacker.unpack()

            if asyncio.iscoroutinefunction(func):
                fut = asyncio.async(func(*args, **kwargs), loop=self.loop)
                fut.add_done_callback(partial(self.process_call_result,
                                              req_id=req_id, peer=peer))
            else:
                fut = asyncio.Future(loop=self.loop)
                fut.add_done_callback(partial(self.process_call_result,
                                              req_id=req_id, peer=peer))
                try:
                    fut.set_result(func(*args, **kwargs))
                except Exception as exc:
                    fut.set_exception(exc)

    def process_call_result(self, fut, *, req_id, peer):
        try:
            ret = fut.result()
            prefix = self.prefix + self.RESP_SUFFIX.pack(req_id,
                                                         time.time(), False)
            self.transport.write([peer, prefix, self.packer.pack(ret)])
        except Exception as exc:
            prefix = self.prefix + self.RESP_SUFFIX.pack(req_id,
                                                         time.time(), True)
            exc_type = exc.__class__
            exc_info = (exc_type.__module__ + '.' + exc_type.__name__,
                        exc.args)
            self.transport.write([peer, prefix, self.packer.pack(exc_info)])

    def dispatch(self, name):
        if not name:
            raise NotFoundError(name)
        namespaces, sep, method = name.rpartition('.')
        handler = self.handler
        if namespaces:
            for part in namespaces.split('.'):
                try:
                    handler = handler[part]
                except KeyError:
                    raise NotFoundError(name)
                else:
                    if not isinstance(handler, AbstractHandler):
                        raise NotFoundError(name)

        try:
            func = handler[method]
        except KeyError:
            raise NotFoundError(name)
        else:
            if isinstance(func, MethodType):
                holder = func.__func__
            else:
                holder = func
            try:
                data = getattr(holder, '__rpc__')
                # TODO: validate trafaret
                return func
            except AttributeError:
                raise NotFoundError(name)
