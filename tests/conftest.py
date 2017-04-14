import pytest
import pathlib
import subprocess
import tempfile
import os
import string
import socket
import textwrap

import yarl
import aiohttp
import asyncio

from collections import namedtuple
from functools import partial
from aiohttp import web

ROOT = pathlib.Path('/work')


def pytest_addoption(parser):
    parser.addoption('--swindon-bin', default=[],
                     action='append',
                     help="Path to swindon binary to run")
    parser.addoption('--swindon-config',
                     default='./tests/config.yaml.tpl',
                     help=("Path to swindon config template,"
                           " default is `%(default)s`"))
    parser.addoption('--rust-log',
                     default='debug,tokio_core=warn',
                     help=("Set RUST_LOG for swindon, default is"
                           " \"%(default)s\""))


SWINDON_BIN = []


def pytest_configure(config):
    bins = config.getoption('--swindon-bin')[:]
    SWINDON_BIN[:] = bins or ['target/debug/swindon']
    for _ in range(len(SWINDON_BIN)):
        p = SWINDON_BIN.pop(0)
        p = ROOT / p
        assert p.exists(), p
        SWINDON_BIN.append(str(p))

# Fixtures


@pytest.fixture(params=[
    'GET', 'PATCH', 'POST', 'PUT', 'UPDATED', 'DELETE', 'XXX'])
def request_method(request):
    """Parametrized fixture changing request method
    (GET / POST / PATCH / ...).
    """
    return request.param


@pytest.fixture(params=[aiohttp.HttpVersion11, aiohttp.HttpVersion10],
                ids=['http/1.1', 'http/1.0'])
def http_version(request):
    return request.param


@pytest.fixture(scope='session', params=[True, False],
                ids=['debug-routing', 'no-debug-routing'])
def debug_routing(request):
    return request.param


@pytest.fixture
def http_request(request_method, http_version, debug_routing, loop):

    async def inner(url, **kwargs):
        async with aiohttp.ClientSession(version=http_version, loop=loop) as s:
            async with s.request(request_method, url, **kwargs) as resp:
                data = await resp.read()
                assert resp.version == http_version
                assert_headers(resp.headers, debug_routing)
                return resp, data
    return inner


def assert_headers(headers, debug_routing):
    assert 'Content-Type' in headers
    assert 'Content-Length' in headers
    assert 'Date' in headers
    assert 'Server' in headers
    if debug_routing:
        assert 'X-Swindon-Route' in headers
    else:
        assert 'X-Swindon-Route' not in headers

    assert len(headers.getall('Content-Type')) == 1
    assert len(headers.getall('Content-Length')) == 1
    assert len(headers.getall('Date')) == 1
    assert headers.getall('Server') == ['swindon/func-tests']


SwindonInfo = namedtuple('SwindonInfo', 'proc url proxy api api2')


@pytest.fixture(scope='session')
def TESTS_DIR():
    return os.path.dirname(__file__)


@pytest.fixture(scope='session')
def unused_port():
    used = set()

    def find():
        while True:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('127.0.0.1', 0))
                port = s.getsockname()[1]
                if port in used:
                    continue
                used.add(port)
                return port
    return find


@pytest.fixture(scope='session', params=SWINDON_BIN)
def swindon(_proc, request, debug_routing, unused_port, slapd, TESTS_DIR):
    SWINDON_ADDRESS = unused_port()
    PROXY_ADDRESS = unused_port()
    SESSION_POOL_ADDRESS1 = unused_port()
    SESSION_POOL_ADDRESS2 = unused_port()

    def to_addr(port):
        return '127.0.0.1:{}'.format(port)

    no_permission_file = pathlib.Path(tempfile.gettempdir())
    no_permission_file /= 'no-permission.txt'
    if not no_permission_file.exists():
        no_permission_file.touch(0o000)

    swindon_bin = request.param
    fd, fname = tempfile.mkstemp()

    conf_template = pathlib.Path(request.config.getoption('--swindon-config'))
    with (ROOT / conf_template).open('rt') as f:
        tpl = string.Template(f.read())

    config = tpl.substitute(listen_address=to_addr(SWINDON_ADDRESS),
                            debug_routing=str(debug_routing).lower(),
                            proxy_address=to_addr(PROXY_ADDRESS),
                            spool_address1=to_addr(SESSION_POOL_ADDRESS1),
                            spool_address2=to_addr(SESSION_POOL_ADDRESS2),
                            ldap_port=slapd.port,
                            TESTS_DIR=TESTS_DIR,
                            )
    assert _check_config(config, returncode=0, __swindon_bin=swindon_bin) == ''

    os.write(fd, config.encode('utf-8'))

    proc = _proc(swindon_bin,
                 '--verbose',
                 '--config',
                 fname,
                 env={'RUST_LOG': request.config.getoption('--rust-log'),
                      'RUST_BACKTRACE': os.environ.get('RUST_BACKTRACE', '0')},
                 )
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.connect(('127.0.0.1', SWINDON_ADDRESS))
            except ConnectionRefusedError:
                continue
            break

    url = yarl.URL('http://localhost:{}'.format(SWINDON_ADDRESS))
    proxy = yarl.URL('http://localhost:{}'.format(PROXY_ADDRESS))
    api = yarl.URL('http://localhost:{}'.format(SESSION_POOL_ADDRESS1))
    api2 = yarl.URL('http://localhost:{}'.format(SESSION_POOL_ADDRESS2))
    try:
        yield SwindonInfo(proc, url, proxy, api, api2)
    finally:
        os.close(fd)
        os.remove(fname)


LdapInfo = namedtuple('LdapInfo', 'port')

INIT_LDIF = """
dn: dc=ldap,dc=example,dc=org
objectClass: top
objectClass: dcObject
objectClass: organization
o: example
dc: ldap
"""

USER1_LDIF = """
dn: uid=user1,dc=ldap,dc=example,dc=org
objectClass: top
objectClass: person
objectClass: organizationalPerson
objectClass: inetOrgPerson
objectClass: posixAccount
objectClass: shadowAccount
uid: user1
cn: user1
sn: Doe
loginShell: /bin/bash
uidNumber: 88888
gidNumber: 88888
homeDirectory: /home/user1/
userPassword: {SSHA}XnjCdRGr5tD5cjmcBM7WvfKeBTumAYgW
"""

@pytest.fixture(scope='session')
def slapd(_proc, request, debug_routing, unused_port):
    LDAP_PORT = unused_port()

    proc = _proc('/usr/sbin/slapd',
                 '-f/etc/ldap/slapd.conf',
                 '-hldap://127.0.0.1:{}'.format(LDAP_PORT),
                 '-dnone')
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.connect(('127.0.0.1', LDAP_PORT))
            except ConnectionRefusedError:
                continue
            break

    subprocess.Popen(['ldapadd',
        '-Hldap://127.0.0.1:{}'.format(LDAP_PORT),
        '-Dcn=Manager,dc=ldap,dc=example,dc=org', '-w1234', '-x',
        ]).communicate(INIT_LDIF)

    subprocess.Popen(['ldapadd',
        '-Hldap://127.0.0.1:{}'.format(LDAP_PORT),
        '-Dcn=Manager,dc=ldap,dc=example,dc=org', '-w1234', '-x',
        ]).communicate(USER1_LDIF)

    yield LdapInfo(LDAP_PORT)


@pytest.fixture(params=SWINDON_BIN)
def check_config(request):
    return partial(_check_config, __swindon_bin=request.param)


def _check_config(cfg='', returncode=1, *, __swindon_bin):
    cfg = textwrap.dedent(cfg)
    with tempfile.NamedTemporaryFile('wt') as f:
        f.write(cfg)
        f.flush()

        res = subprocess.run([
            __swindon_bin,
            '--check-config',
            '--config',
            f.name,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # encoding='utf-8',
            timeout=15,
            )
        assert not res.stdout, res
        assert res.returncode == returncode, res
        return res.stderr.decode('utf-8').replace(f.name, 'TEMP_FILE_NAME')


@pytest.fixture
def proxy_server(swindon, loop):
    ctx = ContextServer(swindon.proxy.port, loop)
    loop.run_until_complete(ctx.start_server())
    try:
        yield ctx
    finally:
        loop.run_until_complete(ctx.stop_server())


class ContextServer:

    def __init__(self, port, loop=None):
        self.port = port
        self.loop = loop
        self.queue = asyncio.Queue(loop=loop)

        async def handler(request):
            fut = self.loop.create_future()
            await self.queue.put((request, fut))
            return await fut

        self.server = web.Server(handler, loop=loop)
        self._srv = None

    async def start_server(self):
        assert self._srv is None
        self._srv = await self.loop.create_server(
            self.server, '127.0.0.1', self.port,
            reuse_address=True,
            reuse_port=hasattr(socket, 'SO_REUSEPORT'))

    async def stop_server(self):
        assert self._srv is not None
        await self.server.shutdown(1)
        self._srv.close()
        await self._srv.wait_closed()

    def send(self, method, url, **kwargs):
        assert self._srv

        async def _send_request():
            async with aiohttp.ClientSession(loop=self.loop) as sess:
                async with sess.request(method, url, **kwargs) as resp:
                    await resp.read()
                    return resp

        tsk = asyncio.ensure_future(_send_request(), loop=self.loop)
        return _RequestContext(self.queue, tsk, loop=self.loop)

    def swindon_chat(self, url, **kwargs):
        # TODO: return Websocket worker
        return _WSContext(self.queue, url, kwargs, loop=self.loop)


class _RequestContext:
    def __init__(self, queue, tsk, loop=None):
        self.queue = queue
        self.tsk = tsk
        self.loop = loop

    async def __aenter__(self):
        get = asyncio.ensure_future(self.queue.get(), loop=self.loop)
        await asyncio.wait([get, self.tsk],
                           return_when=asyncio.FIRST_COMPLETED,
                           loop=self.loop)
        # must receive request first
        # otherwise something is wrong and request completed first
        if get.done():
            self._req, self._fut = await get
        else:
            get.cancel()
            self._req = self._fut = None

        return Inflight(self._req, self._fut, self.tsk)

    async def __aexit__(self, exc_type, exc, tb):
        if self._fut and not self._fut.done():
            self._fut.cancel()
        if not self.tsk.done():
            self.tsk.cancel()


class _WSContext:
    def __init__(self, queue, url, kwargs, loop):
        self.queue = queue
        self.loop = loop
        self.url = url
        self.kwargs = kwargs
        self.sess = aiohttp.ClientSession(loop=loop)
        self.ws = None

    async def __aenter__(self):

        fut = asyncio.ensure_future(
            self.sess.ws_connect(self.url, **self.kwargs), loop=self.loop)

        def set_ws(f):
            try:
                self.ws = f.result()
            except Exception as err:
                self.queue.put_nowait((err, None))
        fut.add_done_callback(set_ws)

        return WSInflight(self.queue, fut)

    async def __aexit__(self, exc_type, exc, tb):
        # XXX: this hangs for a while...
        if self.ws:
            await self.ws.close()
        await self.sess.close()


_Inflight = namedtuple('Inflight', 'req srv_resp client_resp')

_WSInflight = namedtuple('WSInflight', 'queue websocket')


class Inflight(_Inflight):
    @property
    def has_client_response(self):
        return self.client_resp.done()

    async def send_resp(self, resp):
        if isinstance(resp, Exception):
            self.srv_resp.set_exception(resp)
        else:
            self.srv_resp.set_result(resp)
        return await self.client_resp


class WSInflight(_WSInflight):

    async def request(self):
        return await self.queue.get()


# helpers


@pytest.fixture(scope='session')
def _proc():
    # Process runner
    processes = []

    def run(*cmdline, **kwargs):
        cmdline = list(map(str, cmdline))
        proc = subprocess.Popen(cmdline, **kwargs)
        processes.append(proc)
        return proc

    try:
        yield run
    finally:
        while processes:
            proc = processes.pop(0)
            proc.terminate()
            proc.wait()
