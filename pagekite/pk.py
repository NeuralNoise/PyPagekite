"""
This is what is left of the original monolithic pagekite.py.
This is slowly being refactored into smaller sub-modules.
"""
##############################################################################
LICENSE = """\
This file is part of pagekite.py.
Copyright 2010-2013, the Beanstalks Project ehf. and Bjarni Runar Einarsson

This program is free software: you can redistribute it and/or modify it under
the terms of the  GNU  Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version.

This program is distributed in the hope that it will be useful,  but  WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public License for more
details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see: <http://www.gnu.org/licenses/>
"""
##############################################################################
import base64
import cgi
from cgi import escape as escape_html
import errno
import getopt
import httplib
import os
import random
import re
import select
import socket
import struct
import sys
import tempfile
import threading
import time
import traceback
import urllib
import xmlrpclib
import zlib

import SocketServer
from CGIHTTPServer import CGIHTTPRequestHandler
from SimpleXMLRPCServer import SimpleXMLRPCServer, SimpleXMLRPCRequestHandler
import Cookie
from pagekite.dnsclient import DnsClient

from compat import *
from common import *
import pagekite.common as common

import compat
import logging

OPT_FLAGS = 'o:O:S:H:P:X:L:ZI:fA:R:h:p:aD:U:NE:'
OPT_ARGS = ['noloop', 'clean', 'nopyopenssl', 'nossl',
            'help', 'settings',
            'optfile=', 'optdir=', 'savefile=',
            'list', 'add', 'only', 'disable', 'remove', 'save',
            'service_xmlrpc=', 'controlpanel', 'controlpass',
            'httpd=', 'pemfile=', 'httppass=', 'errorurl=', 'webpath=',
            'logfile=', 'daemonize', 'nodaemonize', 'runas=', 'pidfile=',
            'isfrontend', 'noisfrontend', 'settings',
            'defaults', 'local=', 'domain=',
            'authdomain=', 'motd=', 'register=', 'host=',
            'noupgradeinfo', 'upgradeinfo=',
            'ports=', 'protos=', 'portalias=', 'rawports=',
            'tls_default=', 'tls_endpoint=', 'selfsign',
            'fe_certname=', 'jakenoia', 'ca_certs=',
            'kitename=', 'kitesecret=', 'fingerpath=',
            'backend=', 'define_backend=', 'be_config=', 'insecure',
            'service_on=', 'service_off=', 'service_cfg=',
            'tunnel_acl=', 'client_acl=', 'accept_acl_file=',
            'frontend=', 'nofrontend=', 'frontends=',
            'torify=', 'socksify=', 'proxy=', 'noproxy',
            'new', 'all', 'noall', 'dyndns=', 'nozchunks', 'sslzlib',
            'buffers=', 'noprobes', 'debugio', 'watch=',
            'server_ns_update=',
            # DEPRECATED:
            'reloadfile=', 'autosave', 'noautosave', 'webroot=',
            'webaccess=', 'webindexes=', 'delete_backend=']


# Enable system proxies
# This will all fail if we don't have PySocksipyChain available.
# FIXME: Move this code somewhere else?
socks.usesystemdefaults()
socks.wrapmodule(sys.modules[__name__])

if socks.HAVE_SSL:
  # Secure connections to pagekite.net in SSL tunnels.
  def_hop = socks.parseproxy('default')
  https_hop = socks.parseproxy(('httpcs!%s!443'
                                ) % ','.join(['pagekite.net']+SERVICE_CERTS))
  for dest in ('pagekite.net', 'up.pagekite.net', 'up.b5p.us'):
    socks.setproxy(dest, *def_hop)
    socks.addproxy(dest, *socks.parseproxy('http!%s!443' % dest))
    socks.addproxy(dest, *https_hop)
else:
  # FIXME: Should scream and shout about lack of security.
  pass


##[ PageKite.py code starts here! ]############################################

from proto.proto import *
from proto.parsers import *
from proto.selectables import *
from proto.filters import *
from proto.conns import *
from ui.nullui import NullUi


# FIXME: This could easily be a pool of threads to let us handle more
#        than one incoming request at a time.
class AuthThread(threading.Thread):
  """Handle authentication work in a separate thread."""

  #daemon = True

  def __init__(self, conns):
    threading.Thread.__init__(self)
    self.qc = threading.Condition()
    self.jobs = []
    self.conns = conns

  def check(self, requests, conn, callback):
    self.qc.acquire()
    self.jobs.append((requests, conn, callback))
    self.qc.notify()
    self.qc.release()

  def quit(self):
    self.qc.acquire()
    self.keep_running = False
    self.qc.notify()
    self.qc.release()
    try:
      self.join()
    except RuntimeError:
      pass

  def run(self):
    self.keep_running = True
    while self.keep_running:
      try:
        self._run()
      except Exception, e:
        logging.LogError('AuthThread died: %s' % e)
        time.sleep(5)
    logging.LogDebug('AuthThread: done')

  def _run(self):
    self.qc.acquire()
    while self.keep_running:
      now = int(time.time())
      if not self.jobs:
        (requests, conn, callback) = None, None, None
        self.qc.wait()
      else:
        (requests, conn, callback) = self.jobs.pop(0)
        if logging.DEBUG_IO: print '=== AUTH REQUESTS\n%s\n===' % requests
        self.qc.release()

        quotas = []
        q_conns = []
        q_days = []
        results = []
        log_info = []
        session = '%x:%s:' % (now, globalSecret())
        for request in requests:
          try:
            proto, domain, srand, token, sign, prefix = request
          except:
            logging.LogError('Invalid request: %s' % (request, ))
            continue

          what = '%s:%s:%s' % (proto, domain, srand)
          session += what
          if not token or not sign:
            # Send a challenge. Our challenges are time-stamped, so we can
            # put stict bounds on possible replay attacks (20 minutes atm).
            results.append(('%s-SignThis' % prefix,
                            '%s:%s' % (what, signToken(payload=what,
                                                       timestamp=now))))
          else:
            # This is a bit lame, but we only check the token if the quota
            # for this connection has never been verified.
            (quota, days, conns, reason
             ) = self.conns.config.GetDomainQuota(proto, domain, srand, token,
                                         sign, check_token=(conn.quota is None))
            duplicates = self.conns.Tunnel(proto, domain)
            if not quota:
              if not reason: reason = 'quota'
              results.append(('%s-Invalid' % prefix, what))
              results.append(('%s-Invalid-Why' % prefix,
                              '%s;%s' % (what, reason)))
              log_info.extend([('rejected', domain),
                               ('quota', quota),
                               ('reason', reason)])
            elif duplicates:
              # Duplicates... is the old one dead?  Trigger a ping.
              for conn in duplicates:
                conn.TriggerPing()
              results.append(('%s-Duplicate' % prefix, what))
              log_info.extend([('rejected', domain),
                               ('duplicate', 'yes')])
            else:
              results.append(('%s-OK' % prefix, what))
              quotas.append((quota, request))
              if conns: q_conns.append(conns)
              if days: q_days.append(days)
              if (proto.startswith('http') and
                  self.conns.config.GetTlsEndpointCtx(domain)):
                results.append(('%s-SSL-OK' % prefix, what))

        results.append(('%s-SessionID' % prefix,
                        '%x:%s' % (now, sha1hex(session))))
        results.append(('%s-Misc' % prefix, urllib.urlencode({
                          'motd': (self.conns.config.motd_message or ''),
                        })))
        for upgrade in self.conns.config.upgrade_info:
          results.append(('%s-Upgrade' % prefix, ';'.join(upgrade)))

        if quotas:
          min_qconns = min(q_conns or [0])
          if q_conns and min_qconns:
            results.append(('%s-QConns' % prefix, min_qconns))

          min_qdays = min(q_days or [0])
          if q_days and min_qdays:
            results.append(('%s-QDays' % prefix, min_qdays))

          nz_quotas = [qp for qp in quotas if qp[0] and qp[0] > 0]
          if nz_quotas:
            quota = min(nz_quotas)[0]
            conn.quota = [quota, [qp[1] for qp in nz_quotas], time.time()]
            results.append(('%s-Quota' % prefix, quota))
          elif requests:
            if not conn.quota:
              conn.quota = [None, requests, time.time()]
            else:
              conn.quota[2] = time.time()

        if logging.DEBUG_IO: print '=== AUTH RESULTS\n%s\n===' % results
        callback(results, log_info)
        self.qc.acquire()

    self.buffering = 0
    self.qc.release()


##[ Selectables ]##############################################################

class Connections(object):
  """A container for connections (Selectables), config and tunnel info."""

  def __init__(self, config):
    self.config = config
    self.ip_tracker = {}
    self.idle = []
    self.conns = []
    self.conns_by_id = {}
    self.tunnels = {}
    self.auth = None

  def start(self, auth_thread=None):
    self.auth = auth_thread or AuthThread(self)
    self.auth.start()

  def Add(self, conn):
    self.conns.append(conn)

  def SetAltId(self, conn, new_id):
    if conn.alt_id and conn.alt_id in self.conns_by_id:
      del self.conns_by_id[conn.alt_id]
    if new_id:
      self.conns_by_id[new_id] = conn
    conn.alt_id = new_id

  def SetIdle(self, conn, seconds):
    self.idle.append((time.time() + seconds, conn.last_activity, conn))

  def TrackIP(self, ip, domain):
    tick = '%d' % (time.time()/12)
    if tick not in self.ip_tracker:
      deadline = int(tick)-10
      for ot in self.ip_tracker.keys():
        if int(ot) < deadline:
          del self.ip_tracker[ot]
      self.ip_tracker[tick] = {}

    if ip not in self.ip_tracker[tick]:
      self.ip_tracker[tick][ip] = [1, domain]
    else:
      self.ip_tracker[tick][ip][0] += 1
      self.ip_tracker[tick][ip][1] = domain

  def LastIpDomain(self, ip):
    domain = None
    for tick in sorted(self.ip_tracker.keys()):
      if ip in self.ip_tracker[tick]:
        domain = self.ip_tracker[tick][ip][1]
    return domain

  def Remove(self, conn, retry=True):
    try:
      if conn.alt_id and conn.alt_id in self.conns_by_id:
        del self.conns_by_id[conn.alt_id]
      if conn in self.conns:
        self.conns.remove(conn)
      rmp = []
      for elc in self.idle:
        if elc[-1] == conn:
          rmp.append(elc)
      for elc in rmp:
        self.idle.remove(elc)
      for tid, tunnels in self.tunnels.items():
        if conn in tunnels:
          tunnels.remove(conn)
          if not tunnels:
            del self.tunnels[tid]
    except (ValueError, KeyError):
      # Let's not asplode if another thread races us for this.
      logging.LogError('Failed to remove %s: %s' % (conn, format_exc()))
      if retry:
        return self.Remove(conn, retry=False)

  def IdleConns(self):
    return [p[-1] for p in self.idle]

  def Sockets(self):
    return [s.fd for s in self.conns]

  def Readable(self):
    # FIXME: This is O(n)
    now = time.time()
    return [s.fd for s in self.conns if s.IsReadable(now)]

  def Blocked(self):
    # FIXME: This is O(n)
    # Magic side-effect: update buffered byte counter
    blocked = [s for s in self.conns if s.IsBlocked()]
    common.buffered_bytes[0] = sum([len(s.write_blocked) for s in blocked])
    return [s.fd for s in blocked]

  def DeadConns(self):
    return [s for s in self.conns if s.IsDead()]

  def CleanFds(self):
    evil = []
    for s in self.conns:
      try:
        i, o, e = select.select([s.fd], [s.fd], [s.fd], 0)
      except:
        evil.append(s)
    for s in evil:
      logging.LogDebug('Removing broken Selectable: %s' % s)
      s.Cleanup()
      self.Remove(s)

  def Connection(self, fd):
    for conn in self.conns:
      if conn.fd == fd:
        return conn
    return None

  def TunnelServers(self):
    servers = {}
    for tid in self.tunnels:
      for tunnel in self.tunnels[tid]:
        server = tunnel.server_info[tunnel.S_NAME]
        if server is not None:
          servers[server] = 1
    return servers.keys()

  def CloseTunnel(self, proto, domain, conn):
    tid = '%s:%s' % (proto, domain)
    if tid in self.tunnels:
      if conn in self.tunnels[tid]:
        self.tunnels[tid].remove(conn)
      if not self.tunnels[tid]:
        del self.tunnels[tid]

  def CheckIdleConns(self, now):
    active = []
    for elc in self.idle:
      expire, last_activity, conn = elc
      if conn.last_activity > last_activity:
        active.append(elc)
      elif expire < now:
        logging.LogDebug('Killing idle connection: %s' % conn)
        conn.Die(discard_buffer=True)
      elif conn.created < now - 1:
        conn.SayHello()
    for pair in active:
      self.idle.remove(pair)

  def Tunnel(self, proto, domain, conn=None):
    tid = '%s:%s' % (proto, domain)
    if conn is not None:
      if tid not in self.tunnels:
        self.tunnels[tid] = []
      self.tunnels[tid].append(conn)

    if tid in self.tunnels:
      return self.tunnels[tid]
    else:
      try:
        dparts = domain.split('.')[1:]
        while len(dparts) > 1:
          wild_tid = '%s:*.%s' % (proto, '.'.join(dparts))
          if wild_tid in self.tunnels:
            return self.tunnels[wild_tid]
          dparts = dparts[1:]
      except:
        pass

      return []


class HttpUiThread(threading.Thread):
  """Handle HTTP UI in a separate thread."""

  daemon = True

  def __init__(self, pkite, conns,
               server=None, handler=None, ssl_pem_filename=None):
    threading.Thread.__init__(self)
    if not (server and handler):
      self.serve = False
      self.httpd = None
      return

    self.ui_sspec = pkite.ui_sspec
    self.httpd = server(self.ui_sspec, pkite, conns,
                        handler=handler,
                        ssl_pem_filename=ssl_pem_filename)
    self.httpd.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    self.ui_sspec = pkite.ui_sspec = (self.ui_sspec[0],
                                      self.httpd.socket.getsockname()[1])
    self.serve = True

  def quit(self):
    self.serve = False
    try:
      knock = rawsocket(socket.AF_INET, socket.SOCK_STREAM)
      knock.connect(self.ui_sspec)
      knock.close()
    except IOError:
      pass
    try:
      self.join()
    except RuntimeError:
      try:
        if self.httpd and self.httpd.socket:
          self.httpd.socket.close()
      except IOError:
        pass

  def run(self):
    while self.serve:
      try:
        self.httpd.handle_request()
      except KeyboardInterrupt:
        self.serve = False
      except Exception, e:
        logging.LogInfo('HTTP UI caught exception: %s' % e)
    if self.httpd: self.httpd.socket.close()
    logging.LogDebug('HttpUiThread: done')


class TunnelManager(threading.Thread):
  """Create new tunnels as necessary or kill idle ones."""

  daemon = True

  def __init__(self, pkite, conns):
    threading.Thread.__init__(self)
    self.pkite = pkite
    self.conns = conns

  def CheckTunnelQuotas(self, now):
    for tid in self.conns.tunnels:
      for tunnel in self.conns.tunnels[tid]:
        tunnel.RecheckQuota(self.conns, when=now)

  def PingTunnels(self, now):
    dead = {}
    for tid in self.conns.tunnels:
      for tunnel in self.conns.tunnels[tid]:
        pings = PING_INTERVAL
        if tunnel.server_info[tunnel.S_IS_MOBILE]:
          pings = PING_INTERVAL_MOBILE
        grace = max(PING_GRACE_DEFAULT,
                    len(tunnel.write_blocked)/(tunnel.write_speed or 0.001))
        if tunnel.last_activity == 0:
          pass
        elif tunnel.last_ping < now - PING_GRACE_MIN:
          if tunnel.last_activity < tunnel.last_ping-(PING_GRACE_MIN+grace):
            dead['%s' % tunnel] = tunnel
          elif tunnel.last_activity < now-pings:
            tunnel.SendPing()
          elif random.randint(0, 10*pings) == 0:
            tunnel.SendPing()

    for tunnel in dead.values():
      logging.Log([('dead', tunnel.server_info[tunnel.S_NAME])])
      tunnel.Die(discard_buffer=True)

  def CloseTunnels(self):
    close = []
    for tid in self.conns.tunnels:
      for tunnel in self.conns.tunnels[tid]:
        close.append(tunnel)
    for tunnel in close:
      logging.Log([('closing', tunnel.server_info[tunnel.S_NAME])])
      tunnel.Die(discard_buffer=True)

  def quit(self):
    self.keep_running = False
    try:
      self.join()
    except RuntimeError:
      pass

  def run(self):
    self.keep_running = True
    self.explained = False
    while self.keep_running:
      try:
        self._run()
      except Exception, e:
        logging.LogError('TunnelManager died: %s' % e)
        if logging.DEBUG_IO:
          traceback.print_exc(file=sys.stderr)
        time.sleep(5)
    logging.LogDebug('TunnelManager: done')

  def DoFrontendWork(self):
    self.CheckTunnelQuotas(time.time())
    self.pkite.LoadMOTD()

    # FIXME: Front-ends should close dead back-end tunnels.
    for tid in self.conns.tunnels:
      proto, domain = tid.split(':')
      if '-' in proto:
        proto, port = proto.split('-')
      else:
        port = ''
      self.pkite.ui.NotifyFlyingFE(proto, port, domain)

  def ListBackEnds(self):
    self.pkite.ui.StartListingBackEnds()

    for bid in self.pkite.backends:
      be = self.pkite.backends[bid]
      # Do we have auto-SSL at the front-end?
      protoport, domain = bid.split(':', 1)
      tunnels = self.conns.Tunnel(protoport, domain)
      if be[BE_PROTO] in ('http', 'http2', 'http3') and tunnels:
        has_ssl = True
        for t in tunnels:
          if (protoport, domain) not in t.remote_ssl:
            has_ssl = False
      else:
        has_ssl = False

      # Get list of webpaths...
      domainp = '%s/%s' % (domain, be[BE_PORT] or '80')
      if (self.pkite.ui_sspec and
          be[BE_BHOST] == self.pkite.ui_sspec[0] and
          be[BE_BPORT] == self.pkite.ui_sspec[1]):
        builtin = True
        dpaths = self.pkite.ui_paths.get(domainp, {})
      else:
        builtin = False
        dpaths = {}

      self.pkite.ui.NotifyBE(bid, be, has_ssl, dpaths,
                             is_builtin=builtin,
                         fingerprint=(builtin and self.pkite.ui_pemfingerprint))

    self.pkite.ui.EndListingBackEnds()

  def UpdateUiStatus(self, problem, connecting):
    tunnel_count = len(self.pkite.conns and
                       self.pkite.conns.TunnelServers() or [])
    tunnel_total = len(self.pkite.servers)
    if tunnel_count == 0:
      if self.pkite.isfrontend:
        self.pkite.ui.Status('idle', message='Waiting for back-ends.')
      elif tunnel_total == 0:
        self.pkite.ui.Notify('It looks like your Internet connection might be '
                             'down! Will retry soon.')
        self.pkite.ui.Status('down', color=self.pkite.ui.GREY,
                       message='No kites ready to fly.  Waiting...')
      elif connecting == 0:
        self.pkite.ui.Status('down', color=self.pkite.ui.RED,
                       message='Not connected to any front-ends, will retry...')
    elif tunnel_count < tunnel_total:
      self.pkite.ui.Status('flying', color=self.pkite.ui.YELLOW,
                    message=('Only connected to %d/%d front-ends, will retry...'
                             ) % (tunnel_count, tunnel_total))
    elif problem:
      self.pkite.ui.Status('flying', color=self.pkite.ui.YELLOW,
                     message='DynDNS updates may be incomplete, will retry...')
    else:
      self.pkite.ui.Status('flying', color=self.pkite.ui.GREEN,
                                   message='Kites are flying and all is well.')

  def _run(self):
    self.check_interval = 5
    while self.keep_running:

      # Reconnect if necessary, randomized exponential fallback.
      problem, connecting = self.pkite.CreateTunnels(self.conns)
      if problem or connecting:
        self.check_interval = min(60, self.check_interval +
                                     int(1+random.random()*self.check_interval))
        time.sleep(1)
      else:
        self.check_interval = 5

      # Make sure tunnels are really alive.
      if self.pkite.isfrontend:
        self.DoFrontendWork()
      self.PingTunnels(time.time())

      # FIXME: This is constant noise, instead there should be a
      #        command which requests this stuff.
      self.ListBackEnds()
      self.UpdateUiStatus(problem, connecting)

      for i in xrange(0, self.check_interval):
        if self.keep_running:
          time.sleep(1)
          if i > self.check_interval:
            break
          if self.pkite.isfrontend:
            self.conns.CheckIdleConns(time.time())

  def HurryUp(self):
    self.check_interval = 0


def SecureCreate(path):
  fd = open(path, 'w')
  try:
    os.chmod(path, 0600)
  except OSError:
    pass
  return fd

def CreateSelfSignedCert(pem_path, ui):
  ui.Notify('Creating a 2048-bit self-signed TLS certificate ...',
            prefix='-', color=ui.YELLOW)

  workdir = tempfile.mkdtemp()
  def w(fn):
    return os.path.join(workdir, fn)

  os.system(('openssl genrsa -out %s 2048') % w('key'))
  os.system(('openssl req -batch -new -key %s -out %s'
                        ' -subj "/CN=PageKite/O=Self-Hosted/OU=Website"'
             ) % (w('key'), w('csr')))
  os.system(('openssl x509 -req -days 3650 -in %s -signkey %s -out %s'
             ) % (w('csr'), w('key'), w('crt')))

  pem = SecureCreate(pem_path)
  pem.write(open(w('key')).read())
  pem.write('\n')
  pem.write(open(w('crt')).read())
  pem.close()

  for fn in ['key', 'csr', 'crt']:
    os.remove(w(fn))
  os.rmdir(workdir)

  ui.Notify('Saved certificate to: %s' % pem_path,
            prefix='-', color=ui.YELLOW)


class PageKite(object):
  """Configuration and master select loop."""

  def __init__(self, ui=None, http_handler=None, http_server=None):
    self.progname = ((sys.argv[0] or 'pagekite.py').split('/')[-1]
                                                   .split('\\')[-1])
    self.ui = ui or NullUi()
    self.ui_request_handler = http_handler
    self.ui_http_server = http_server
    self.ResetConfiguration()

  def ResetConfiguration(self):
    self.isfrontend = False
    self.upgrade_info = []
    self.auth_domain = None
    self.auth_domains = {}
    self.motd = None
    self.motd_message = None
    self.server_host = ''
    self.server_ports = [80]
    self.server_raw_ports = []
    self.server_portalias = {}
    self.server_aliasport = {}
    self.server_protos = ['http', 'http2', 'http3', 'https', 'websocket',
                          'irc', 'finger', 'httpfinger', 'raw', 'minecraft']

    self.accept_acl_file = None
    self.tunnel_acls = []
    self.client_acls = []

    self.tls_default = None
    self.tls_endpoints = {}
    self.fe_certname = []
    self.fe_anon_tls_wrap = False

    self.service_xmlrpc = SERVICE_XMLRPC

    self.daemonize = False
    self.pidfile = None
    self.logfile = None
    self.setuid = None
    self.setgid = None
    self.ui_httpd = None
    self.ui_sspec_cfg = None
    self.ui_sspec = None
    self.ui_socket = None
    self.ui_password = None
    self.ui_pemfile = None
    self.ui_pemfingerprint = None
    self.ui_magic_file = '.pagekite.magic'
    self.ui_paths = {}
    self.insecure = False
    self.be_config = {}
    self.disable_zchunks = False
    self.enable_sslzlib = False
    self.buffer_max = DEFAULT_BUFFER_MAX
    self.error_url = None
    self.finger_path = '/~%s/.finger'

    self.tunnel_manager = None
    self.client_mode = 0

    self.proxy_servers = []
    self.no_proxy = False
    self.require_all = False
    self.no_probes = False
    self.servers = []
    self.servers_manual = []
    self.servers_never = []
    self.servers_auto = None
    self.servers_new_only = False
    self.servers_no_ping = False
    self.servers_preferred = []
    self.servers_sessionids = {}
    self.dns_cache = {}
    self.ping_cache = {}
    self.last_frontend_choice = 0

    self.kitename = ''
    self.kitesecret = ''
    self.dyndns = None
    self.last_updates = []
    self.backends = {}  # These are the backends we want tunnels for.
    self.conns = None
    self.last_loop = 0
    self.keep_looping = True
    self.main_loop = True
    self.watch_level = [None]

    self.rcfile_recursion = 0
    self.rcfiles_loaded = []
    self.savefile = None
    self.added_kites = False
    self.ui_wfile = sys.stderr
    self.ui_rfile = sys.stdin

    self.save = 0
    self.kite_add = False
    self.kite_only = False
    self.kite_disable = False
    self.kite_remove = False

    self.reloadfile = None
    self.server_ns_update = None
    self.dnsclient = None

    # Searching for our configuration file!  We prefer the documented
    # 'standard' locations, but if nothing is found there and something local
    # exists, use that instead.
    try:
      if sys.platform[:3] in ('win', 'os2'):
        self.rcfile = os.path.join(os.path.expanduser('~'), 'pagekite.cfg')
        self.devnull = 'nul'
      else:
        # Everything else
        self.rcfile = os.path.join(os.path.expanduser('~'), '.pagekite.rc')
        self.devnull = '/dev/null'

    except Exception, e:
      # The above stuff may fail in some cases, e.g. on Android in SL4A.
      self.rcfile = 'pagekite.cfg'
      self.devnull = '/dev/null'

    # Look for CA Certificates. If we don't find them in the host OS,
    # we assume there might be something good in the program itself.
    self.ca_certs_default = '/etc/ssl/certs/ca-certificates.crt'
    if not os.path.exists(self.ca_certs_default):
      self.ca_certs_default = sys.argv[0]
    self.ca_certs = self.ca_certs_default

  ACL_SHORTHAND = {
    'localhost': '((::ffff:)?127\..*|::1)',
    'any': '.*'
  }
  def CheckAcls(self, acls, address, which, conn=None):
    if not acls:
      return True
    for policy, pattern in acls:
      if re.match(self.ACL_SHORTHAND.get(pattern, pattern)+'$', address[0]):
        if (policy.lower() == 'allow'):
          return True
        else:
          if conn:
            conn.LogError(('%s rejected by %s ACL: %s:%s'
                           ) % (address[0], which, policy, pattern))
          return False
    if conn:
      conn.LogError('%s rejected by default %s ACL' % (address[0], which))
    return False

  def CheckClientAcls(self, address, conn=None):
    return self.CheckAcls(self.client_acls, address, 'client', conn)

  def CheckTunnelAcls(self, address, conn=None):
    return self.CheckAcls(self.tunnel_acls, address, 'tunnel', conn)

  def SetLocalSettings(self, ports):
    self.isfrontend = True
    self.servers_auto = None
    self.servers_manual = []
    self.servers_never = []
    self.server_ports = ports
    self.backends = self.ArgToBackendSpecs('http:localhost:localhost:builtin:-')

  def SetServiceDefaults(self, clobber=True, check=False):
    def_dyndns    = (DYNDNS['pagekite.net'], {'user': '', 'pass': ''})
    def_frontends = (1, 'frontends.b5p.us', 443)
    def_ca_certs  = sys.argv[0]
    def_fe_certs  = ['b5p.us'] + [c for c in SERVICE_CERTS if c != 'b5p.us']
    def_error_url = 'https://pagekite.net/offline/?'
    if check:
      return (self.dyndns == def_dyndns and
              self.servers_auto == def_frontends and
              self.error_url == def_error_url and
              self.ca_certs == def_ca_certs and
              (sorted(self.fe_certname) == sorted(def_fe_certs) or
               not socks.HAVE_SSL))
    else:
      self.dyndns = (not clobber and self.dyndns) or def_dyndns
      self.servers_auto = (not clobber and self.servers_auto) or def_frontends
      self.error_url = (not clobber and self.error_url) or def_error_url
      self.ca_certs = def_ca_certs
      if socks.HAVE_SSL:
        for cert in def_fe_certs:
          if cert not in self.fe_certname:
            self.fe_certname.append(cert)
      return True

  def GenerateConfig(self, safe=False):
    config = [
      '###[ Current settings for pagekite.py v%s. ]#########' % APPVER,
      '#',
      '## NOTE: This file may be rewritten/reordered by pagekite.py.',
      '#',
      '',
    ]

    if not self.kitename:
      for be in self.backends.values():
        if not self.kitename or len(self.kitename) < len(be[BE_DOMAIN]):
          self.kitename = be[BE_DOMAIN]
          self.kitesecret = be[BE_SECRET]

    new = not (self.kitename or self.kitesecret or self.backends)
    def p(vfmt, value, dval):
      return '%s%s' % (value and value != dval
                             and ('', vfmt % value) or ('# ', vfmt % dval))

    if self.kitename or self.kitesecret or new:
      config.extend([
        '##[ Default kite and account details ]##',
        p('kitename   = %s', self.kitename, 'NAME'),
        p('kitesecret = %s', self.kitesecret, 'SECRET'),
        ''
      ])

    if self.SetServiceDefaults(check=True):
      config.extend([
        '##[ Front-end settings: use pagekite.net defaults ]##',
        'defaults',
        ''
      ])
      if self.servers_manual or self.servers_never:
        config.append('##[ Manual front-ends ]##')
        for server in sorted(self.servers_manual):
          config.append('frontend=%s' % server)
        for server in sorted(self.servers_never):
          config.append('nofrontend=%s' % server)
        config.append('')
    else:
      if not self.servers_auto and not self.servers_manual:
        new = True
        config.extend([
          '##[ Use this to just use pagekite.net defaults ]##',
          '# defaults',
          ''
        ])
      config.append('##[ Custom front-end and dynamic DNS settings ]##')
      if self.servers_auto:
        config.append('frontends = %d:%s:%d' % self.servers_auto)
      if self.servers_manual:
        for server in sorted(self.servers_manual):
          config.append('frontend = %s' % server)
      if self.servers_never:
        for server in sorted(self.servers_never):
          config.append('nofrontend = %s' % server)
      if not self.servers_auto and not self.servers_manual:
        new = True
        config.append('# frontends = N:hostname:port')
        config.append('# frontend = hostname:port')
        config.append('# nofrontend = hostname:port  # never connect')

      for server in self.fe_certname:
        config.append('fe_certname = %s' % server)
      if self.ca_certs != self.ca_certs_default:
        config.append('ca_certs = %s' % self.ca_certs)

      if self.dyndns:
        provider, args = self.dyndns
        for prov in sorted(DYNDNS.keys()):
          if DYNDNS[prov] == provider and prov != 'beanstalks.net':
            args['prov'] = prov
        if 'prov' not in args:
          args['prov'] = provider
        if args['pass']:
          config.append('dyndns = %(user)s:%(pass)s@%(prov)s' % args)
        elif args['user']:
          config.append('dyndns = %(user)s@%(prov)s' % args)
        else:
          config.append('dyndns = %(prov)s' % args)
      else:
        new = True
        config.extend([
          '# dyndns = pagekite.net OR',
          '# dyndns = user:pass@dyndns.org OR',
          '# dyndns = user:pass@no-ip.com' ,
          '#',
          p('errorurl  = %s', self.error_url, 'http://host/page/'),
          p('fingerpath = %s', self.finger_path, '/~%s/.finger'),
          '',
        ])
      config.append('')

    if self.ui_sspec or self.ui_password or self.ui_pemfile:
      config.extend([
        '##[ Built-in HTTPD settings ]##',
        p('httpd = %s:%s', self.ui_sspec_cfg, ('host', 'port'))
      ])
      if self.ui_password: config.append('httppass=%s' % self.ui_password)
      if self.ui_pemfile: config.append('pemfile=%s' % self.ui_pemfile)
      for http_host in sorted(self.ui_paths.keys()):
        for path in sorted(self.ui_paths[http_host].keys()):
          up = self.ui_paths[http_host][path]
          config.append('webpath = %s:%s:%s:%s' % (http_host, path, up[0], up[1]))
      config.append('')

    config.append('##[ Back-ends and local services ]##')
    bprinted = 0
    for bid in sorted(self.backends.keys()):
      be = self.backends[bid]
      proto, domain = bid.split(':')
      if be[BE_BHOST]:
        be_spec = (be[BE_BHOST], be[BE_BPORT])
        be_spec = ((be_spec == self.ui_sspec) and 'localhost:builtin'
                                               or ('%s:%s' % be_spec))
        fe_spec = ('%s:%s' % (proto, (domain == self.kitename) and '@kitename'
                                                               or domain))
        secret = ((be[BE_SECRET] == self.kitesecret) and '@kitesecret'
                                                      or be[BE_SECRET])
        config.append(('%s = %-33s: %-18s: %s'
                       ) % ((be[BE_STATUS] == BE_STATUS_DISABLED
                             ) and 'service_off' or 'service_on ',
                            fe_spec, be_spec, secret))
        bprinted += 1
    if bprinted == 0:
      config.append('# No back-ends!  How boring!')
    config.append('')
    for http_host in sorted(self.be_config.keys()):
      for key in sorted(self.be_config[http_host].keys()):
        config.append(('service_cfg = %-30s: %-15s: %s'
                       ) % (http_host, key, self.be_config[http_host][key]))
    config.append('')

    if bprinted == 0:
      new = True
      config.extend([
        '##[ Back-end service examples ... ]##',
        '#',
        '# service_on = http:YOU.pagekite.me:localhost:80:SECRET',
        '# service_on = ssh:YOU.pagekite.me:localhost:22:SECRET',
        '# service_on = http/8080:YOU.pagekite.me:localhost:8080:SECRET',
        '# service_on = https:YOU.pagekite.me:localhost:443:SECRET',
        '# service_on = websocket:YOU.pagekite.me:localhost:8080:SECRET',
        '# service_on = minecraft:YOU.pagekite.me:localhost:8080:SECRET',
        '#',
        '# service_off = http:YOU.pagekite.me:localhost:4545:SECRET',
        ''
      ])

    config.extend([
      '##[ Allow risky known-to-be-risky incoming HTTP requests? ]##',
      (self.insecure) and 'insecure' or '# insecure',
      ''
    ])

    if self.isfrontend or new:
      config.extend([
        '##[ Front-end Options ]##',
        (self.isfrontend and 'isfrontend' or '# isfrontend')
      ])
      comment = ((not self.isfrontend) and '# ' or '')
      config.extend([
        p('host = %s', self.isfrontend and self.server_host, 'machine.domain.com'),
        '%sports = %s' % (comment, ','.join(['%s' % x for x in sorted(self.server_ports)] or [])),
        '%sprotos = %s' % (comment, ','.join(['%s' % x for x in sorted(self.server_protos)] or []))
      ])
      for pa in self.server_portalias:
        config.append('portalias = %s:%s' % (int(pa), int(self.server_portalias[pa])))
      config.extend([
        '%srawports = %s' % (comment or (not self.server_raw_ports) and '# ' or '',
                           ','.join(['%s' % x for x in sorted(self.server_raw_ports)] or [VIRTUAL_PN])),
        p('authdomain = %s', self.isfrontend and self.auth_domain, 'foo.com'),
        p('motd = %s', self.isfrontend and self.motd, '/path/to/motd.txt')
      ])
      for d in sorted(self.auth_domains.keys()):
        config.append('authdomain=%s:%s' % (d, self.auth_domains[d]))
      dprinted = 0
      for bid in sorted(self.backends.keys()):
        be = self.backends[bid]
        if not be[BE_BHOST]:
          config.append('domain = %s:%s' % (bid, be[BE_SECRET]))
          dprinted += 1
      if not dprinted:
        new = True
        config.extend([
          '# domain = http:*.pagekite.me:SECRET1',
          '# domain = http,https,websocket:THEM.pagekite.me:SECRET2',
        ])

      eprinted = 0
      config.extend([
        '',
        '##[ Domains we terminate SSL/TLS for natively, with key/cert-files ]##'
      ])
      for ep in sorted(self.tls_endpoints.keys()):
        config.append('tls_endpoint = %s:%s' % (ep, self.tls_endpoints[ep][0]))
        eprinted += 1
      if eprinted == 0:
        new = True
        config.append('# tls_endpoint = DOMAIN:PEM_FILE')
      config.extend([
        p('tls_default = %s', self.tls_default, 'DOMAIN'),
        '',
      ])

    config.extend([
      '##[ Proxy-chain settings ]##',
      (self.no_proxy and 'noproxy' or '# noproxy'),
    ])
    for proxy in self.proxy_servers:
      config.append('proxy = %s' % proxy)
    if not self.proxy_servers:
      config.extend([
        '# socksify = host:port',
        '# torify   = host:port',
        '# proxy    = ssl:/path/to/client-cert.pem@host,CommonName:port',
        '# proxy    = http://user:password@host:port/',
        '# proxy    = socks://user:password@host:port/'
      ])

    config.extend([
      '',
      '##[ Front-end access controls (default=deny, if configured) ]##',
      p('accept_acl_file = %s', self.accept_acl_file, '/path/to/file'),
    ])
    for policy, pattern in self.client_acls:
      config.append('client_acl=%s:%s' % (policy, pattern))
    if not self.client_acls:
      config.append('# client_acl=[allow|deny]:IP-regexp')
    for policy, pattern in self.tunnel_acls:
      config.append('tunnel_acl=%s:%s' % (policy, pattern))
    if not self.tunnel_acls:
      config.append('# tunnel_acl=[allow|deny]:IP-regexp')
    config.extend([
      '',
      '',
      '###[ Anything below this line can usually be ignored. ]#########',
      '',
      '##[ Miscellaneous settings ]##',
      p('logfile = %s', self.logfile, '/path/to/file'),
      p('buffers = %s', self.buffer_max, DEFAULT_BUFFER_MAX),
      (self.servers_new_only is True) and 'new' or '# new',
      (self.require_all and 'all' or '# all'),
      (self.no_probes and 'noprobes' or '# noprobes'),
      p('savefile = %s', safe and self.savefile, '/path/to/savefile'),
      '',
    ])

    if self.daemonize or self.setuid or self.setgid or self.pidfile or new:
      config.extend([
        '##[ Systems administration settings ]##',
        (self.daemonize and 'daemonize' or '# daemonize')
      ])
      if self.setuid and self.setgid:
        config.append('runas = %s:%s' % (self.setuid, self.setgid))
      elif self.setuid:
        config.append('runas = %s' % self.setuid)
      else:
        new = True
        config.append('# runas = uid:gid')
      config.append(p('pidfile = %s', self.pidfile, '/path/to/file'))

    config.extend([
      '',
      '###[ End of pagekite.py configuration ]#########',
      'END',
      ''
    ])
    if not new:
      config = [l for l in config if not l.startswith('# ')]
      clean_config = []
      for i in range(0, len(config)-1):
        if i > 0 and (config[i].startswith('#') or config[i] == ''):
          if config[i+1] != '' or clean_config[-1].startswith('#'):
            clean_config.append(config[i])
        else:
          clean_config.append(config[i])
      clean_config.append(config[-1])
      return clean_config
    else:
      return config

  def ConfigSecret(self, new=False):
    # This method returns a stable secret for the lifetime of this process.
    #
    # The secret depends on the active configuration as, reported by
    # GenerateConfig().  This lets external processes generate the same
    # secret and use the remote-control APIs as long as they can read the
    # *entire* config (which contains all the sensitive bits anyway).
    #
    if self.ui_httpd and self.ui_httpd.httpd and not new:
      return self.ui_httpd.httpd.secret
    else:
      return sha1hex('\n'.join(self.GenerateConfig()))

  def LoginPath(self, goto):
    return '/_pagekite/login/%s/%s' % (self.ConfigSecret(), goto)

  def LoginUrl(self, goto=''):
    return 'http%s://%s%s' % (self.ui_pemfile and 's' or '',
                              '%s:%s' % self.ui_sspec,
                              self.LoginPath(goto))

  def ListKites(self):
    self.ui.welcome = '>>> ' + self.ui.WHITE + 'Your kites:' + self.ui.NORM
    message = []
    for bid in sorted(self.backends.keys()):
      be = self.backends[bid]
      be_be = (be[BE_BHOST], be[BE_BPORT])
      backend = (be_be == self.ui_sspec) and 'builtin' or '%s:%s' % be_be
      fe_port = be[BE_PORT] or ''
      frontend = '%s://%s%s%s' % (be[BE_PROTO], be[BE_DOMAIN],
                                  fe_port and ':' or '', fe_port)

      if be[BE_STATUS] == BE_STATUS_DISABLED:
        color = self.ui.GREY
        status = '(disabled)'
      else:
        color = self.ui.NORM
        status = (be[BE_PROTO] == 'raw') and '(HTTP proxied)' or ''
      message.append(''.join([color, backend, ' ' * (19-len(backend)),
                              frontend, ' ' * (42-len(frontend)), status]))
    message.append(self.ui.NORM)
    self.ui.Tell(message)

  def PrintSettings(self, safe=False):
    print '\n'.join(self.GenerateConfig(safe=safe))

  def SaveUserConfig(self, quiet=False):
    self.savefile = self.savefile or self.rcfile
    try:
      fd = SecureCreate(self.savefile)
      fd.write('\n'.join(self.GenerateConfig(safe=True)))
      fd.close()
      if not quiet:
        self.ui.Tell(['Settings saved to: %s' % self.savefile])
        self.ui.Spacer()
      logging.Log([('saved', 'Settings saved to: %s' % self.savefile)])
    except Exception, e:
      if logging.DEBUG_IO: traceback.print_exc(file=sys.stderr)
      self.ui.Tell(['Could not save to %s: %s' % (self.savefile, e)],
                   error=True)
      self.ui.Spacer()

  def FallDown(self, message, help=True, longhelp=False, noexit=False):
    if self.conns and self.conns.auth:
      self.conns.auth.quit()
    if self.ui_httpd:
      self.ui_httpd.quit()
    if self.tunnel_manager:
      self.tunnel_manager.quit()
    self.keep_looping = False

    for fd in (self.conns and self.conns.Sockets() or []):
      try:
        fd.close()
      except (IOError, OSError, TypeError, AttributeError):
        pass
    self.conns = self.ui_httpd = self.tunnel_manager = None

    try:
      os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    except:
      pass
    print
    if help or longhelp:
      import manual
      print longhelp and manual.DOC() or manual.MINIDOC()
      print '***'
    elif not noexit:
      self.ui.Status('exiting', message=(message or 'Good-bye!'))
    if message:
      print 'Error: %s' % message

    if logging.DEBUG_IO:
      traceback.print_exc(file=sys.stderr)
    if not noexit:
      self.main_loop = False
      sys.exit(1)

  def GetTlsEndpointCtx(self, domain):
    if domain in self.tls_endpoints:
      return self.tls_endpoints[domain][1]
    parts = domain.split('.')
    # Check for wildcards ...
    if len(parts) > 2:
      parts[0] = '*'
      domain = '.'.join(parts)
      if domain in self.tls_endpoints:
        return self.tls_endpoints[domain][1]
    return None

  def SetBackendStatus(self, domain, proto='', add=None, sub=None):
    match = '%s:%s' % (proto, domain)
    for bid in self.backends:
      if bid == match or (proto == '' and bid.endswith(match)):
        status = self.backends[bid][BE_STATUS]
        if add: self.backends[bid][BE_STATUS] |= add
        if sub and (status & sub): self.backends[bid][BE_STATUS] -= sub
        logging.Log([('bid', bid),
             ('status', '0x%x' % self.backends[bid][BE_STATUS])])

  def GetBackendData(self, proto, domain, recurse=True):
    backend = '%s:%s' % (proto.lower(), domain.lower())
    if backend in self.backends:
      if self.backends[backend][BE_STATUS] not in BE_INACTIVE:
        return self.backends[backend]

    if recurse:
      dparts = domain.split('.')
      while len(dparts) > 1:
        dparts = dparts[1:]
        data = self.GetBackendData(proto, '.'.join(['*'] + dparts), recurse=False)
        if data: return data

    return None

  def GetBackendServer(self, proto, domain, recurse=True):
    backend = self.GetBackendData(proto, domain) or BE_NONE
    bhost, bport = (backend[BE_BHOST], backend[BE_BPORT])
    if bhost == '-' or not bhost: return None, None
    return (bhost, bport), backend

  def IsSignatureValid(self, sign, secret, proto, domain, srand, token):
    return checkSignature(sign=sign, secret=secret,
                          payload='%s:%s:%s:%s' % (proto, domain, srand, token))

  def LookupDomainQuota(self, lookup):
    if not lookup.endswith('.'): lookup += '.'
    if logging.DEBUG_IO: print '=== AUTH LOOKUP\n%s\n===' % lookup
    (hn, al, ips) = socket.gethostbyname_ex(lookup)
    if logging.DEBUG_IO: print 'hn=%s\nal=%s\nips=%s\n' % (hn, al, ips)

    # Extract auth error and extended quota info from CNAME replies
    if al:
      error, hg, hd, hc, junk = hn.split('.', 4)
      q_days = int(hd, 16)
      q_conns = int(hc, 16)
    else:
      error = q_days = q_conns = None

    # If not an authentication error, quota should be encoded as an IP.
    ip = ips[0]
    if ip.startswith(AUTH_ERRORS):
      if not error and (ip.endswith(AUTH_ERR_USER_UNKNOWN) or
                        ip.endswith(AUTH_ERR_INVALID)):
        error = 'unauthorized'
    else:
      o = [int(x) for x in ip.split('.')]
      return ((((o[0]*256 + o[1])*256 + o[2])*256 + o[3]), q_days, q_conns, None)

    # Errors on real errors are final.
    if not ip.endswith(AUTH_ERR_USER_UNKNOWN): return (None, q_days, q_conns, error)

    # User unknown, fall through to local test.
    return (-1, q_days, q_conns, error)

  def GetDomainQuota(self, protoport, domain, srand, token, sign,
                     recurse=True, check_token=True):
    if '-' in protoport:
      try:
        proto, port = protoport.split('-', 1)
        if proto == 'raw':
          port_list = self.server_raw_ports
        else:
          port_list = self.server_ports

        porti = int(port)
        if porti in self.server_aliasport: porti = self.server_aliasport[porti]
        if porti not in port_list and VIRTUAL_PN not in port_list:
          logging.LogInfo('Unsupported port request: %s (%s:%s)' % (porti, protoport, domain))
          return (None, None, None, 'port')

      except ValueError:
        logging.LogError('Invalid port request: %s:%s' % (protoport, domain))
        return (None, None, None, 'port')
    else:
      proto, port = protoport, None

    if proto not in self.server_protos:
      logging.LogInfo('Invalid proto request: %s:%s' % (protoport, domain))
      return (None, None, None, 'proto')

    data = '%s:%s:%s' % (protoport, domain, srand)
    auth_error_type = None
    if ((not token) or
        (not check_token) or
        checkSignature(sign=token, payload=data)):

      secret = (self.GetBackendData(protoport, domain) or BE_NONE)[BE_SECRET]
      if not secret:
        secret = (self.GetBackendData(proto, domain) or BE_NONE)[BE_SECRET]

      if secret:
        if self.IsSignatureValid(sign, secret, protoport, domain, srand, token):
          return (-1, None, None, None)
        elif not self.auth_domain:
          logging.LogError('Invalid signature for: %s (%s)' % (domain, protoport))
          return (None, None, None, auth_error_type or 'signature')

      if self.auth_domain:
        adom = self.auth_domain
        for dom in self.auth_domains:
          if domain.endswith('.%s' % dom):
            adom = self.auth_domains[dom]
        try:
          lookup = '.'.join([srand, token, sign, protoport,
                             domain.replace('*', '_any_'), adom])
          (rv, qd, qc, auth_error_type) = self.LookupDomainQuota(lookup)
          if rv is None or rv >= 0:
            return (rv, qd, qc, auth_error_type)
        except Exception, e:
          # Lookup failed, fail open.
          logging.LogError('Quota lookup failed: %s' % e)
          return (-2, None, None, None)

    logging.LogInfo('No authentication found for: %s (%s)' % (domain, protoport))
    return (None, None, None, auth_error_type or 'unauthorized')

  def ConfigureFromFile(self, filename=None, data=None):
    if not filename: filename = self.rcfile

    if self.rcfile_recursion > 25:
      raise ConfigError('Nested too deep: %s' % filename)

    self.rcfiles_loaded.append(filename)
    optfile = data or open(filename)
    args = []
    for line in optfile:
      line = line.strip()
      if line and not line.startswith('#'):
        if line.startswith('END'): break
        if not line.startswith('-'): line = '--%s' % line
        args.append(re.sub(r'\s*:\s*', ':', re.sub(r'\s*=\s*', '=', line)))

    self.rcfile_recursion += 1
    self.Configure(args)
    self.rcfile_recursion -= 1
    return self

  def ConfigureFromDirectory(self, dirname):
    for fn in sorted(os.listdir(dirname)):
      if not fn.startswith('.') and fn.endswith('.rc'):
        self.ConfigureFromFile(os.path.join(dirname, fn))

  def HelpAndExit(self, longhelp=False):
    import manual
    print longhelp and manual.DOC() or manual.MINIDOC()
    sys.exit(0)

  def AddNewKite(self, kitespec, status=BE_STATUS_UNKNOWN, secret=None):
    new_specs = self.ArgToBackendSpecs(kitespec, status, secret)
    self.backends.update(new_specs)
    req = {}
    for server in self.conns.TunnelServers():
      req[server] = '\r\n'.join(PageKiteRequestHeaders(server, new_specs, {}))
    for tid, tunnels in self.conns.tunnels.iteritems():
      for tunnel in tunnels:
        server_name = tunnel.server_info[tunnel.S_NAME]
        if server_name in req:
          tunnel.SendChunked('NOOP: 1\r\n%s\r\n\r\n!' % req[server_name],
                             compress=False)
          del req[server_name]

  def ArgToBackendSpecs(self, arg, status=BE_STATUS_UNKNOWN, secret=None):
    protos, fe_domain, be_host, be_port = '', '', '', ''

    # Interpret the argument into a specification of what we want.
    parts = arg.split(':')
    if len(parts) == 5:
      protos, fe_domain, be_host, be_port, secret = parts
    elif len(parts) == 4:
      protos, fe_domain, be_host, be_port = parts
    elif len(parts) == 3:
      protos, fe_domain, be_port = parts
    elif len(parts) == 2:
      if (parts[1] == 'builtin') or ('.' in parts[0] and
                                            os.path.exists(parts[1])):
        fe_domain, be_port = parts[0], parts[1]
        protos = 'http'
      else:
        try:
          fe_domain, be_port = parts[0], '%s' % int(parts[1])
          protos = 'http'
        except:
          be_port = ''
          protos, fe_domain = parts
    elif len(parts) == 1:
      fe_domain = parts[0]
    else:
      return {}

    # Allow http:// as a common typo instead of http:
    fe_domain = fe_domain.replace('/', '').lower()

    # Allow easy referencing of built-in HTTPD
    if be_port == 'builtin':
      self.BindUiSspec()
      be_host, be_port = self.ui_sspec

    # Specs define what we are searching for...
    specs = []
    if protos:
      for proto in protos.replace('/', '-').lower().split(','):
        if proto == 'ssh':
          specs.append(['raw', '22', fe_domain, be_host, be_port or '22', secret])
        else:
          if '-' in proto:
            proto, port = proto.split('-')
          else:
            if len(parts) == 1:
              port = '*'
            else:
              port = ''
          specs.append([proto, port, fe_domain, be_host, be_port, secret])
    else:
      specs = [[None, '', fe_domain, be_host, be_port, secret]]

    backends = {}
    # For each spec, search through the existing backends and copy matches
    # or just shared secrets for partial matches.
    for proto, port, fdom, bhost, bport, sec in specs:
      matches = 0
      for bid in self.backends:
        be = self.backends[bid]
        if fdom and fdom != be[BE_DOMAIN]: continue
        if not sec and be[BE_SECRET]: sec = be[BE_SECRET]
        if proto and (proto != be[BE_PROTO]): continue
        if bhost and (bhost.lower() != be[BE_BHOST]): continue
        if bport and (int(bport) != be[BE_BHOST]): continue
        if port and (port != '*') and (int(port) != be[BE_PORT]): continue
        backends[bid] = be[:]
        backends[bid][BE_STATUS] = status
        matches += 1

      if matches == 0:
        proto = (proto or 'http')
        bhost = (bhost or 'localhost')
        bport = (bport or (proto in ('http', 'httpfinger', 'websocket') and 80)
                       or (proto == 'irc' and 6667)
                       or (proto == 'https' and 443)
                       or (proto == 'minecraft' and 25565)
                       or (proto == 'finger' and 79))
        if port:
          bid = '%s-%d:%s' % (proto, int(port), fdom)
        else:
          bid = '%s:%s' % (proto, fdom)

        backends[bid] = BE_NONE[:]
        backends[bid][BE_PROTO] = proto
        backends[bid][BE_PORT] = port and int(port) or ''
        backends[bid][BE_DOMAIN] = fdom
        backends[bid][BE_BHOST] = bhost.lower()
        backends[bid][BE_BPORT] = int(bport)
        backends[bid][BE_SECRET] = sec
        backends[bid][BE_STATUS] = status

    return backends

  def BindUiSspec(self, force=False):
    # Create the UI thread
    if self.ui_httpd and self.ui_httpd.httpd:
      if not force: return self.ui_sspec
      self.ui_httpd.httpd.socket.close()

    self.ui_sspec = self.ui_sspec or ('localhost', 0)
    self.ui_httpd = HttpUiThread(self, self.conns,
                                 handler=self.ui_request_handler,
                                 server=self.ui_http_server,
                                 ssl_pem_filename = self.ui_pemfile)
    return self.ui_sspec

  def LoadMOTD(self):
    if self.motd:
      try:
        f = open(self.motd, 'r')
        self.motd_message = ''.join(f.readlines()).strip()[:8192]
        f.close()
      except (OSError, IOError):
        pass

  def SetPem(self, filename):
    self.ui_pemfile = filename
    try:
      p = os.popen('openssl x509 -noout -fingerprint -in %s' % filename, 'r')
      data = p.read().strip()
      p.close()
      self.ui_pemfingerprint = data.split('=')[1]
    except (OSError, ValueError):
      pass

  def Configure(self, argv):
    self.conns = self.conns or Connections(self)
    opts, args = getopt.getopt(argv, OPT_FLAGS, OPT_ARGS)

    for opt, arg in opts:
      if opt in ('-o', '--optfile'):
        self.ConfigureFromFile(arg)
      elif opt in ('-O', '--optdir'):
        self.ConfigureFromDirectory(arg)
      elif opt in ('-S', '--savefile'):
        if self.savefile: raise ConfigError('Multiple save-files!')
        self.savefile = arg
      elif opt == '--save':
        self.save = True
      elif opt == '--only':
        self.save = self.kite_only = True
        if self.kite_remove or self.kite_add or self.kite_disable:
          raise ConfigError('One change at a time please!')
      elif opt == '--add':
        self.save = self.kite_add = True
        if self.kite_remove or self.kite_only or self.kite_disable:
          raise ConfigError('One change at a time please!')
      elif opt == '--remove':
        self.save = self.kite_remove = True
        if self.kite_add or self.kite_only or self.kite_disable:
          raise ConfigError('One change at a time please!')
      elif opt == '--disable':
        self.save = self.kite_disable = True
        if self.kite_add or self.kite_only or self.kite_remove:
          raise ConfigError('One change at a time please!')
      elif opt == '--list': pass

      elif opt in ('-I', '--pidfile'): self.pidfile = arg
      elif opt in ('-L', '--logfile'): self.logfile = arg
      elif opt in ('-Z', '--daemonize'):
        self.daemonize = True
        if not self.ui.DAEMON_FRIENDLY: self.ui = NullUi()
      elif opt in ('-U', '--runas'):
        import pwd
        import grp
        parts = arg.split(':')
        if len(parts) > 1:
          self.setuid, self.setgid = (pwd.getpwnam(parts[0])[2],
                                      grp.getgrnam(parts[1])[2])
        else:
          self.setuid = pwd.getpwnam(parts[0])[2]
        self.main_loop = False

      elif opt in ('-X', '--httppass'): self.ui_password = arg
      elif opt in ('-P', '--pemfile'): self.SetPem(arg)
      elif opt in ('--selfsign', ):
        pf = self.rcfile.replace('.rc', '.pem').replace('.cfg', '.pem')
        if not os.path.exists(pf):
          CreateSelfSignedCert(pf, self.ui)
        self.SetPem(pf)
      elif opt in ('-H', '--httpd'):
        parts = arg.split(':')
        host = parts[0] or 'localhost'
        if len(parts) > 1:
          self.ui_sspec = self.ui_sspec_cfg = (host, int(parts[1]))
        else:
          self.ui_sspec = self.ui_sspec_cfg = (host, 0)

      elif opt == '--nowebpath':
        host, path = arg.split(':', 1)
        if host in self.ui_paths and path in self.ui_paths[host]:
          del self.ui_paths[host][path]
      elif opt == '--webpath':
        host, path, policy, fpath = arg.split(':', 3)

        # Defaults...
        path = path or os.path.normpath(fpath)
        host = host or '*'
        policy = policy or WEB_POLICY_DEFAULT

        if policy not in WEB_POLICIES:
          raise ConfigError('Policy must be one of: %s' % WEB_POLICIES)
        elif os.path.isdir(fpath):
          if not path.endswith('/'): path += '/'

        hosti = self.ui_paths.get(host, {})
        hosti[path] = (policy or 'public', os.path.abspath(fpath))
        self.ui_paths[host] = hosti

      elif opt == '--tls_default': self.tls_default = arg
      elif opt == '--tls_endpoint':
        name, pemfile = arg.split(':', 1)
        ctx = SSL.Context(SSL.SSLv23_METHOD)
        ctx.use_privatekey_file(pemfile)
        ctx.use_certificate_chain_file(pemfile)
        self.tls_endpoints[name] = (pemfile, ctx)

      elif opt in ('-D', '--dyndns'):
        if arg.startswith('http'):
          self.dyndns = (arg, {'user': '', 'pass': ''})
        elif '@' in arg:
          splits = arg.split('@')
          provider = splits.pop()
          usrpwd = '@'.join(splits)
          if provider in DYNDNS: provider = DYNDNS[provider]
          if ':' in usrpwd:
            usr, pwd = usrpwd.split(':', 1)
            self.dyndns = (provider, {'user': usr, 'pass': pwd})
          else:
            self.dyndns = (provider, {'user': usrpwd, 'pass': ''})
        elif arg:
          if arg in DYNDNS: arg = DYNDNS[arg]
          self.dyndns = (arg, {'user': '', 'pass': ''})
        else:
          self.dyndns = None

      elif opt in ('-p', '--ports'): self.server_ports = [int(x) for x in arg.split(',')]
      elif opt == '--portalias':
        port, alias = arg.split(':')
        self.server_portalias[int(port)] = int(alias)
        self.server_aliasport[int(alias)] = int(port)
      elif opt == '--protos': self.server_protos = [x.lower() for x in arg.split(',')]
      elif opt == '--rawports':
        self.server_raw_ports = [(x == VIRTUAL_PN and x or int(x)) for x in arg.split(',')]
      elif opt in ('-h', '--host'): self.server_host = arg
      elif opt in ('-A', '--authdomain'):
        if ':' in arg:
          d, a = arg.split(':')
          self.auth_domains[d.lower()] = a
          if not self.auth_domain: self.auth_domain = a
        else:
          self.auth_domains = {}
          self.auth_domain = arg
      elif opt == '--motd':
        self.motd = arg
        self.LoadMOTD()
      elif opt == '--noupgradeinfo': self.upgrade_info = []
      elif opt == '--upgradeinfo':
        version, tag, md5, human_url, file_url = arg.split(';')
        self.upgrade_info.append((version, tag, md5, human_url, file_url))
      elif opt in ('-f', '--isfrontend'):
        self.isfrontend = True
        logging.LOG_THRESHOLD *= 4

      elif opt in ('-a', '--all'): self.require_all = True
      elif opt in ('-N', '--new'): self.servers_new_only = True
      elif opt == '--accept_acl_file':
        self.accept_acl_file = arg
      elif opt == '--client_acl':
        policy, pattern = arg.split(':', 1)
        self.client_acls.append((policy, pattern))
      elif opt == '--tunnel_acl':
        policy, pattern = arg.split(':', 1)
        self.tunnel_acls.append((policy, pattern))
      elif opt in ('--noproxy', ):
        self.no_proxy = True
        self.proxy_servers = []
        socks.setdefaultproxy()
      elif opt in ('--proxy', '--socksify', '--torify'):
        if opt == '--proxy':
          socks.adddefaultproxy(*socks.parseproxy(arg))
        else:
          (host, port) = arg.rsplit(':', 1)
          socks.adddefaultproxy(socks.PROXY_TYPE_SOCKS5, host, int(port))

        if not self.proxy_servers:
          # Make DynDNS updates go via the proxy.
          socks.wrapmodule(urllib)
          self.proxy_servers = [arg]
        else:
          self.proxy_servers.append(arg)

        if opt == '--torify':
          self.servers_new_only = True  # Disable initial DNS lookups (leaks)
          self.servers_no_ping = True   # Disable front-end pings

          # This increases the odds of unrelated requests getting lumped
          # together in the tunnel, which makes traffic analysis harder.
          compat.SEND_ALWAYS_BUFFERS = True

      elif opt == '--ca_certs': self.ca_certs = arg
      elif opt == '--jakenoia': self.fe_anon_tls_wrap = True
      elif opt == '--fe_certname':
        if arg == '':
          self.fe_certname = []
        else:
          cert = arg.lower()
          if cert not in self.fe_certname: self.fe_certname.append(cert)
      elif opt == '--service_xmlrpc': self.service_xmlrpc = arg
      elif opt == '--frontend': self.servers_manual.append(arg)
      elif opt == '--nofrontend': self.servers_never.append(arg)
      elif opt == '--frontends':
        count, domain, port = arg.split(':')
        self.servers_auto = (int(count), domain, int(port))

      elif opt in ('--errorurl', '-E'): self.error_url = arg
      elif opt == '--fingerpath': self.finger_path = arg
      elif opt == '--kitename': self.kitename = arg
      elif opt == '--kitesecret': self.kitesecret = arg

      elif opt in ('--service_on', '--service_off',
                   '--backend', '--define_backend'):
        if opt in ('--backend', '--service_on'):
          status = BE_STATUS_UNKNOWN
        else:
          status = BE_STATUS_DISABLED
        bes = self.ArgToBackendSpecs(arg.replace('@kitesecret', self.kitesecret)
                                        .replace('@kitename', self.kitename),
                                     status=status)
        for bid in bes:
          if bid in self.backends:
            raise ConfigError("Same service/domain defined twice: %s" % bid)
          if not self.kitename:
            self.kitename = bes[bid][BE_DOMAIN]
            self.kitesecret = bes[bid][BE_SECRET]
        self.backends.update(bes)
      elif opt in ('--be_config', '--service_cfg'):
        host, key, val = arg.split(':', 2)
        if key.startswith('user/'): key = key.replace('user/', 'password/')
        hostc = self.be_config.get(host, {})
        hostc[key] = {'True': True, 'False': False, 'None': None}.get(val, val)
        self.be_config[host] = hostc

      elif opt == '--domain':
        protos, domain, secret = arg.split(':')
        if protos in ('*', ''): protos = ','.join(self.server_protos)
        for proto in protos.split(','):
          bid = '%s:%s' % (proto, domain)
          if bid in self.backends:
            #logging.LogDebug("Redefining domain: %s" % bid)
            if (self.backends[bid][BE_SECRET] != secret):
                logging.LogDebug("Redefining domain: %s" % bid)
                self.backends[bid][BE_SECRET] = secret
                self.backends[bid][BE_STATUS] = BE_STATUS_UNKNOWN
          self.backends[bid] = BE_NONE[:]
          self.backends[bid][BE_PROTO] = proto
          self.backends[bid][BE_DOMAIN] = domain
          self.backends[bid][BE_SECRET] = secret
          self.backends[bid][BE_STATUS] = BE_STATUS_UNKNOWN
          # TODO: Remove removed backends

      elif opt == '--insecure': self.insecure = True
      elif opt == '--noprobes': self.no_probes = True
      elif opt == '--nofrontend': self.isfrontend = False
      elif opt == '--nodaemonize': self.daemonize = False
      elif opt == '--noall': self.require_all = False
      elif opt == '--nozchunks': self.disable_zchunks = True
      elif opt == '--sslzlib': self.enable_sslzlib = True
      elif opt == '--watch':
        self.watch_level[0] = int(arg)
      elif opt == '--debugio':
        logging.DEBUG_IO = True
      elif opt == '--buffers': self.buffer_max = int(arg)
      elif opt == '--noloop': self.main_loop = False
      elif opt == '--local':
        self.SetLocalSettings([int(p) for p in arg.split(',')])
        if not 'localhost' in args: args.append('localhost')
      elif opt == '--defaults': self.SetServiceDefaults()
      elif opt in ('--clean', '--nopyopenssl', '--nossl', '--settings'):
        # These are handled outside the main loop, we just ignore them.
        pass
      elif opt in ('--reloadfile'):
          self.reloadfile = arg
      elif opt in ('--server_ns_update'):
          self.server_ns_update = arg
      elif opt in ('--webroot', '--webaccess', '--webindexes',
                   '--noautosave', '--autosave',
                   '--delete_backend'):
        # FIXME: These are deprecated, we should probably warn the user.
        pass
      elif opt == '--help':
        self.HelpAndExit(longhelp=True)

      elif opt == '--controlpanel':
        import webbrowser
        webbrowser.open(self.LoginUrl())
        sys.exit(0)

      elif opt == '--controlpass':
        print self.ConfigSecret()
        sys.exit(0)

      else:
        self.HelpAndExit()

    # Make sure these are configured before we try and do XML-RPC stuff.
    socks.DEBUG = (logging.DEBUG_IO or socks.DEBUG) and logging.LogDebug
    if self.ca_certs: socks.setdefaultcertfile(self.ca_certs)

    # Handle the user-friendly argument stuff and simple registration.
    return self.ParseFriendlyBackendSpecs(args)

  def ParseFriendlyBackendSpecs(self, args):
    just_these_backends = {}
    just_these_webpaths = {}
    just_these_be_configs = {}
    argsets = []
    while 'AND' in args:
      argsets.append(args[0:args.index('AND')])
      args[0:args.index('AND')+1] = []
    if args:
      argsets.append(args)

    for args in argsets:
      # Extract the config options first...
      be_config = [p for p in args if p.startswith('+')]
      args = [p for p in args if not p.startswith('+')]

      fe_spec = (args.pop().replace('@kitesecret', self.kitesecret)
                           .replace('@kitename', self.kitename))
      if os.path.exists(fe_spec):
        raise ConfigError('Is a local file: %s' % fe_spec)

      be_paths = []
      be_path_prefix = ''
      if len(args) == 0:
        be_spec = ''
      elif len(args) == 1:
        if '*' in args[0] or '?' in args[0]:
          if sys.platform[:3] in ('win', 'os2'):
            be_paths = [args[0]]
            be_spec = 'builtin'
        elif os.path.exists(args[0]):
          be_paths = [args[0]]
          be_spec = 'builtin'
        else:
          be_spec = args[0]
      else:
        be_spec = 'builtin'
        be_paths = args[:]

      be_proto = 'http' # A sane default...
      if be_spec == '':
        be = None
      else:
        be = be_spec.replace('/', '').split(':')
        if be[0].lower() in ('http', 'http2', 'http3', 'https',
                             'httpfinger', 'finger', 'ssh', 'irc'):
          be_proto = be.pop(0)
          if len(be) < 2:
            be.append({'http': '80', 'http2': '80', 'http3': '80',
                       'https': '443', 'irc': '6667',
                       'httpfinger': '80', 'finger': '79',
                       'ssh': '22'}[be_proto])
        if len(be) > 2:
          raise ConfigError('Bad back-end definition: %s' % be_spec)
        if len(be) < 2:
          try:
            if be[0] != 'builtin':
              int(be[0])
            be = ['localhost', be[0]]
          except ValueError:
            raise ConfigError('`%s` should be a file, directory, port or '
                              'protocol' % be_spec)

      # Extract the path prefix from the fe_spec
      fe_urlp = fe_spec.split('/', 3)
      if len(fe_urlp) == 4:
        fe_spec = '/'.join(fe_urlp[:3])
        be_path_prefix = '/' + fe_urlp[3]

      fe = fe_spec.replace('/', '').split(':')
      if len(fe) == 3:
        fe = ['%s-%s' % (fe[0], fe[2]), fe[1]]
      elif len(fe) == 2:
        try:
          fe = ['%s-%s' % (be_proto, int(fe[1])), fe[0]]
        except ValueError:
          pass
      elif len(fe) == 1 and be:
        fe = [be_proto, fe[0]]

      # Do our own globbing on Windows
      if sys.platform[:3] in ('win', 'os2'):
        import glob
        new_paths = []
        for p in be_paths:
          new_paths.extend(glob.glob(p))
        be_paths = new_paths

      for f in be_paths:
        if not os.path.exists(f):
          raise ConfigError('File or directory not found: %s' % f)

      spec = ':'.join(fe)
      if be: spec += ':' + ':'.join(be)
      specs = self.ArgToBackendSpecs(spec)
      just_these_backends.update(specs)

      spec = specs[specs.keys()[0]]
      http_host = '%s/%s' % (spec[BE_DOMAIN], spec[BE_PORT] or '80')
      if be_config:
        # Map the +foo=bar values to per-site config settings.
        host_config = just_these_be_configs.get(http_host, {})
        for cfg in be_config:
          if '=' in cfg:
            key, val = cfg[1:].split('=', 1)
          elif cfg.startswith('+no'):
            key, val = cfg[3:], False
          else:
            key, val = cfg[1:], True
          if ':' in key:
            raise ConfigError('Please do not use : in web config keys.')
          if key.startswith('user/'): key = key.replace('user/', 'password/')
          host_config[key] = val
        just_these_be_configs[http_host] = host_config

      if be_paths:
        host_paths = just_these_webpaths.get(http_host, {})
        host_config = just_these_be_configs.get(http_host, {})
        rand_seed = '%s:%x' % (specs[specs.keys()[0]][BE_SECRET],
                               time.time()/3600)

        first = (len(host_paths.keys()) == 0) or be_path_prefix
        paranoid = host_config.get('hide', False)
        set_root = host_config.get('root', True)
        if len(be_paths) == 1:
          skip = len(os.path.dirname(be_paths[0]))
        else:
          skip = len(os.path.dirname(os.path.commonprefix(be_paths)+'X'))

        for path in be_paths:
          phead, ptail = os.path.split(path)
          if paranoid:
            if path.endswith('/'): path = path[0:-1]
            webpath = '%s/%s' % (sha1hex(rand_seed+os.path.dirname(path))[0:9],
                                  os.path.basename(path))
          elif (first and set_root and os.path.isdir(path)):
            webpath = ''
          elif (os.path.isdir(path) and
                not path.startswith('.') and
                not os.path.isabs(path)):
            webpath = path[skip:] + '/'
          elif path == '.':
            webpath = ''
          else:
            webpath = path[skip:]
          while webpath.endswith('/.'):
            webpath = webpath[:-2]
          host_paths[(be_path_prefix + '/' + webpath).replace('///', '/'
                                                    ).replace('//', '/')
                     ] = (WEB_POLICY_DEFAULT, os.path.abspath(path))
          first = False
        just_these_webpaths[http_host] = host_paths

    need_registration = {}
    for be in just_these_backends.values():
      if not be[BE_SECRET]:
        if self.kitesecret and be[BE_DOMAIN] == self.kitename:
          be[BE_SECRET] = self.kitesecret
        elif not self.kite_remove and not self.kite_disable:
          need_registration[be[BE_DOMAIN]] = True

    for domain in need_registration:
      if '.' not in domain:
        raise ConfigError('Not valid domain: %s' % domain)

    for domain in need_registration:
      raise ConfigError("Not sure what to do with %s, giving up." % domain)

    if just_these_backends.keys():
      if self.kite_add:
        self.backends.update(just_these_backends)
      elif self.kite_remove:
        try:
          for bid in just_these_backends:
            be = self.backends[bid]
            if be[BE_PROTO] in ('http', 'http2', 'http3'):
              http_host = '%s/%s' % (be[BE_DOMAIN], be[BE_PORT] or '80')
              if http_host in self.ui_paths: del self.ui_paths[http_host]
              if http_host in self.be_config: del self.be_config[http_host]
            del self.backends[bid]
        except KeyError:
          raise ConfigError('No such kite: %s' % bid)
      elif self.kite_disable:
        try:
          for bid in just_these_backends:
            self.backends[bid][BE_STATUS] = BE_STATUS_DISABLED
        except KeyError:
          raise ConfigError('No such kite: %s' % bid)
      elif self.kite_only:
        for be in self.backends.values(): be[BE_STATUS] = BE_STATUS_DISABLED
        self.backends.update(just_these_backends)
      else:
        # Nothing explictly requested: 'only' behavior with a twist;
        # If kites are new, don't make disables persist on save.
        for be in self.backends.values():
          be[BE_STATUS] = (need_registration and BE_STATUS_DISABLE_ONCE
                                              or BE_STATUS_DISABLED)
        self.backends.update(just_these_backends)

      self.ui_paths.update(just_these_webpaths)
      self.be_config.update(just_these_be_configs)

    return self

  def GetServiceXmlRpc(self):
    service = self.service_xmlrpc
    return xmlrpclib.ServerProxy(self.service_xmlrpc, None, None, False)

  def _KiteInfo(self, kitename):
    is_service_domain = kitename and SERVICE_DOMAIN_RE.search(kitename)
    is_subdomain_of = is_cname_for = is_cname_ready = False
    secret = None

    for be in self.backends.values():
      if be[BE_SECRET] and (be[BE_DOMAIN] == kitename):
        secret = be[BE_SECRET]

    if is_service_domain:
      parts = kitename.split('.')
      if '-' in parts[0]:
        parts[0] = '-'.join(parts[0].split('-')[1:])
        is_subdomain_of = '.'.join(parts)
      elif len(parts) > 3:
        is_subdomain_of = '.'.join(parts[1:])

    elif kitename:
      try:
        (hn, al, ips) = socket.gethostbyname_ex(kitename)
        if hn != kitename and SERVICE_DOMAIN_RE.search(hn):
          is_cname_for = hn
      except:
        pass

    return (secret, is_subdomain_of, is_service_domain,
            is_cname_for, is_cname_ready)

  def CheckConfig(self):
    if self.ui_sspec: self.BindUiSspec()
    if not self.servers_manual and not self.servers_auto and not self.isfrontend:
      if not self.servers and not self.ui.ALLOWS_INPUT:
        raise ConfigError('Nothing to do!  List some servers, or run me as one.')
    return self

  def CheckAllTunnels(self, conns):
    missing = []
    for backend in self.backends:
      proto, domain = backend.split(':')
      if not conns.Tunnel(proto, domain):
        missing.append(domain)
    if missing:
      self.FallDown('No tunnel for %s' % missing, help=False)

  TMP_UUID_MAP = {
    '2400:8900::f03c:91ff:feae:ea35:443': '106.187.99.46:443',
    '2a01:7e00::f03c:91ff:fe96:234:443': '178.79.140.143:443',
    '2600:3c03::f03c:91ff:fe96:2bf:443': '50.116.52.206:443',
    '2600:3c01::f03c:91ff:fe96:257:443': '173.230.155.164:443',
    '69.164.211.158:443': '50.116.52.206:443',
  }
  def Ping(self, host, port):
    cid = uuid = '%s:%s' % (host, port)

    if self.servers_no_ping:
      return (0, uuid)

    while ((cid not in self.ping_cache) or
           (len(self.ping_cache[cid]) < 2) or
           (time.time()-self.ping_cache[cid][0][0] > 60)):

      start = time.time()
      try:
        try:
          if ':' in host:
            fd = socks.socksocket(socket.AF_INET6, socket.SOCK_STREAM)
          else:
            fd = socks.socksocket(socket.AF_INET, socket.SOCK_STREAM)
        except:
          fd = socks.socksocket(socket.AF_INET, socket.SOCK_STREAM)

        try:
          fd.settimeout(3.0) # Missing in Python 2.2
        except:
          fd.setblocking(1)

        fd.connect((host, port))
        fd.send('HEAD / HTTP/1.0\r\n\r\n')
        data = fd.recv(1024)
        fd.close()

      except Exception, e:
        logging.LogDebug('Ping %s:%s failed: %s' % (host, port, e))
        return (100000, uuid)

      elapsed = (time.time() - start)
      try:
        uuid = data.split('X-PageKite-UUID: ')[1].split()[0]
      except:
        uuid = self.TMP_UUID_MAP.get(uuid, uuid)

      if cid not in self.ping_cache:
        self.ping_cache[cid] = []
      elif len(self.ping_cache[cid]) > 10:
        self.ping_cache[cid][8:] = []

      self.ping_cache[cid][0:0] = [(time.time(), (elapsed, uuid))]

    window = min(3, len(self.ping_cache[cid]))
    pingval = sum([e[1][0] for e in self.ping_cache[cid][:window]])/window
    uuid = self.ping_cache[cid][0][1][1]

    logging.LogDebug(('Pinged %s:%s: %f [win=%s, uuid=%s]'
                      ) % (host, port, pingval, window, uuid))
    return (pingval, uuid)

  def GetHostIpAddrs(self, host):
    rv = []
    try:
      info = socket.getaddrinfo(host, 0, socket.AF_UNSPEC, socket.SOCK_STREAM)
      rv = [i[4][0] for i in info]
    except AttributeError:
      rv = socket.gethostbyname_ex(host)[2]
    return rv

  def CachedGetHostIpAddrs(self, host):
    now = int(time.time())

    if host in self.dns_cache:
      # FIXME: This number (900) is 3x the pagekite.net service DNS TTL, which
      # should be about right.  BUG: nothing keeps those two numbers in sync!
      # This number must be larger, or we prematurely disconnect frontends.
      for exp in [t for t in self.dns_cache[host] if t < now-900]:
        del self.dns_cache[host][exp]
    else:
      self.dns_cache[host] = {}

    try:
      self.dns_cache[host][now] = self.GetHostIpAddrs(host)
    except:
      logging.LogDebug('DNS lookup failed for %s' % host)

    ips = {}
    for ipaddrs in self.dns_cache[host].values():
      for ip in ipaddrs:
        ips[ip] = 1
    return ips.keys()

  def GetActiveBackends(self):
    active = []
    for bid in self.backends:
      (proto, bdom) = bid.split(':')
      if (self.backends[bid][BE_STATUS] not in BE_INACTIVE and
          self.backends[bid][BE_SECRET] and
          not bdom.startswith('*')):
        active.append(bid)
    return active

  def ChooseFrontEnds(self):
    self.servers = []
    self.servers_preferred = []
    self.last_frontend_choice = time.time()

    servers_all = {}
    servers_pref = {}

    # Enable internal loopback
    if self.isfrontend:
      need_loopback = False
      for be in self.backends.values():
        if be[BE_BHOST]:
          need_loopback = True
      if need_loopback:
        servers_all['loopback'] = servers_pref['loopback'] = LOOPBACK_FE

    # Convert the hostnames into IP addresses...
    for server in self.servers_manual:
      (host, port) = server.split(':')
      ipaddrs = self.CachedGetHostIpAddrs(host)
      if ipaddrs:
        ptime, uuid = self.Ping(ipaddrs[0], int(port))
        server = '%s:%s' % (ipaddrs[0], port)
        if server not in self.servers_never:
          servers_all[uuid] = servers_pref[uuid] = server

    # Lookup and choose from the auto-list (and our old domain).
    if self.servers_auto:
      (count, domain, port) = self.servers_auto

      # First, check for old addresses and always connect to those.
      selected = {}
      if not self.servers_new_only:
        for bid in self.GetActiveBackends():
          (proto, bdom) = bid.split(':')
          for ip in self.CachedGetHostIpAddrs(bdom):
            # FIXME: What about IPv6 localhost?
            if not ip.startswith('127.'):
              server = '%s:%s' % (ip, port)
              if server not in self.servers_never:
                servers_all[self.Ping(ip, int(port))[1]] = server

      try:
        ips = [ip for ip in self.CachedGetHostIpAddrs(domain)
               if ('%s:%s' % (ip, port)) not in self.servers_never]
        pings = [self.Ping(ip, port) for ip in ips]
      except Exception, e:
        logging.LogDebug('Unreachable: %s, %s' % (domain, e))
        ips = pings = []

      while count > 0 and ips:
        mIdx = pings.index(min(pings))
        if pings[mIdx][0] > 60:
          # This is worthless data, abort.
          break
        else:
          count -= 1
          uuid = pings[mIdx][1]
          server = '%s:%s' % (ips[mIdx], port)
          if uuid not in servers_all:
            servers_all[uuid] = server
          if uuid not in servers_pref:
            servers_pref[uuid] = ips[mIdx]
          del pings[mIdx]
          del ips[mIdx]

    self.servers = servers_all.values()
    self.servers_preferred = servers_pref.values()
    if (len(self.servers_preferred) > 0):
        logging.LogDebug('Preferred: %s' % ', '.join(self.servers_preferred))

  def ConnectFrontend(self, conns, server):
    self.ui.Status('connect', color=self.ui.YELLOW,
                   message='Front-end connect: %s' % server)
    tun = Tunnel.BackEnd(server, self.backends, self.require_all, conns)
    if tun:
      tun.filters.append(HttpHeaderFilter(self.ui))
      if not self.insecure:
        tun.filters.append(HttpSecurityFilter(self.ui))
        if self.watch_level[0] is not None:
          tun.filters.append(TunnelWatcher(self.ui, self.watch_level))
        logging.Log([('connect', server)])
        return True
      else:
        logging.LogInfo('Failed to connect', [('FE', server)])
        self.ui.Notify('Failed to connect to %s' % server,
                       prefix='!', color=self.ui.YELLOW)
        return False

  def DisconnectFrontend(self, conns, server):
    logging.Log([('disconnect', server)])
    kill = []
    for bid in conns.tunnels:
      for tunnel in conns.tunnels[bid]:
        if (server == tunnel.server_info[tunnel.S_NAME] and
            tunnel.countas.startswith('frontend')):
          kill.append(tunnel)
    for tunnel in kill:
      if len(tunnel.users.keys()) < 1:
        tunnel.Die()
    return kill and True or False

  def CreateTunnels(self, conns):
    live_servers = conns.TunnelServers()
    failures = 0
    connections = 0

    if len(self.GetActiveBackends()) > 0:
      if self.last_frontend_choice < time.time()-FE_PING_INTERVAL:
        self.servers = []
      if not self.servers or len(self.servers) > len(live_servers):
        self.ChooseFrontEnds()
    else:
      self.servers_preferred = []
      self.servers = []

    if not self.servers:
      if not self.isfrontend:
          logging.LogDebug('Not sure which servers to contact, making no changes.')
      return 0, 0

    for server in self.servers:
      if server not in live_servers:
        if server == LOOPBACK_FE:
          loop = LoopbackTunnel.Loop(conns, self.backends)
          loop.filters.append(HttpHeaderFilter(self.ui))
          if not self.insecure:
            loop.filters.append(HttpSecurityFilter(self.ui))
        else:
          if self.ConnectFrontend(conns, server):
            connections += 1
          else:
            failures += 1

    for server in live_servers:
      if server not in self.servers and server not in self.servers_preferred:
        if self.DisconnectFrontend(conns, server):
          connections += 1

    if self.dyndns:
      ddns_fmt, ddns_args = self.dyndns

      domains = {}
      for bid in self.backends.keys():
        proto, domain = bid.split(':')
        if domain not in domains:
          domains[domain] = (self.backends[bid][BE_SECRET], [])

        if bid in conns.tunnels:
          ips, bips = [], []
          for tunnel in conns.tunnels[bid]:
            ip = rsplit(':', tunnel.server_info[tunnel.S_NAME])[0]
            if not ip == LOOPBACK_HN and not tunnel.read_eof:
              if not self.servers_preferred or ip in self.servers_preferred:
                ips.append(ip)
              else:
                bips.append(ip)

          for ip in (ips or bips):
            if ip not in domains[domain]:
              domains[domain][1].append(ip)

      updates = {}
      for domain, (secret, ips) in domains.iteritems():
        if ips:
          iplist = ','.join(ips)
          payload = '%s:%s' % (domain, iplist)
          args = {}
          args.update(ddns_args)
          args.update({
            'domain': domain,
            'ip': ips[0],
            'ips': iplist,
            'sign': signToken(secret=secret, payload=payload, length=100)
          })
          # FIXME: This may fail if different front-ends support different
          #        protocols. In practice, this should be rare.
          updates[payload] = ddns_fmt % args

      last_updates = self.last_updates
      self.last_updates = []
      for update in updates:
        if update in last_updates:
          # Was successful last time, no point in doing it again.
          self.last_updates.append(update)
        else:
          domain, ips = update.split(':', 1)
          try:
            self.ui.Status('dyndns', color=self.ui.YELLOW,
                                     message='Updating DNS for %s...' % domain)
            result = ''.join(urllib.urlopen(updates[update]).readlines())
            if result.startswith('good') or result.startswith('nochg'):
              logging.Log([('dyndns', result), ('data', update)])
              self.SetBackendStatus(update.split(':')[0],
                                    sub=BE_STATUS_ERR_DNS)
              self.last_updates.append(update)
              # Success!  Make sure we remember these IP were live.
              if domain not in self.dns_cache:
                self.dns_cache[domain] = {}
              self.dns_cache[domain][int(time.time())] = ips.split(',')
            else:
              logging.LogInfo('DynDNS update failed: %s' % result, [('data', update)])
              self.SetBackendStatus(update.split(':')[0],
                                    add=BE_STATUS_ERR_DNS)
              failures += 1
          except Exception, e:
            logging.LogInfo('DynDNS update failed: %s' % e, [('data', update)])
            if logging.DEBUG_IO: traceback.print_exc(file=sys.stderr)
            self.SetBackendStatus(update.split(':')[0],
                                  add=BE_STATUS_ERR_DNS)
            # Hmm, the update may have succeeded - assume the "worst".
            self.dns_cache[domain][int(time.time())] = ips.split(',')
            failures += 1

    return failures, connections

  def LogTo(self, filename, close_all=True, dont_close=[]):
    if filename == 'memory':
      logging.Log = logging.LogToMemory
      filename = self.devnull

    elif filename == 'syslog':
      logging.Log = logging.LogSyslog
      filename = self.devnull
      compat.syslog.openlog(self.progname, syslog.LOG_PID, syslog.LOG_DAEMON)

    else:
      logging.Log = logging.LogToFile

    if filename in ('stdio', 'stdout'):
      try:
        logging.LogFile = os.fdopen(sys.stdout.fileno(), 'w', 0)
      except:
        logging.LogFile = sys.stdout
    else:
      try:
        logging.LogFile = fd = open(filename, "a", 0)
        os.dup2(fd.fileno(), sys.stdout.fileno())
        if not self.ui.WANTS_STDERR:
          os.dup2(fd.fileno(), sys.stdin.fileno())
          os.dup2(fd.fileno(), sys.stderr.fileno())
      except Exception, e:
        raise ConfigError('%s' % e)

  def Daemonize(self):
    # Fork once...
    if os.fork() != 0: os._exit(0)

    # Fork twice...
    os.setsid()
    if os.fork() != 0: os._exit(0)

  def ProcessWritable(self, oready):
    if logging.DEBUG_IO:
      print '\n=== Ready for Write: %s' % [o and o.fileno() or ''
                                           for o in oready]
    for osock in oready:
      if osock:
        conn = self.conns.Connection(osock)
        if conn and not conn.Send([], try_flush=True):
          conn.Die(discard_buffer=True)

  def ProcessReadable(self, iready, throttle):
    if logging.DEBUG_IO:
      print '\n=== Ready for Read: %s' % [i and i.fileno() or None
                                          for i in iready]
    for isock in iready:
      if isock is not None:
        conn = self.conns.Connection(isock)
        if conn and not (conn.fd and conn.ReadData(maxread=throttle)):
          conn.Die(discard_buffer=True)

  def ProcessDead(self, epoll=None):
    for conn in self.conns.DeadConns():
      if epoll and conn.fd:
        try:
          epoll.unregister(conn.fd)
        except (IOError, TypeError):
          pass
      conn.Cleanup()
      self.conns.Remove(conn)

  def Select(self, epoll, waittime):
    iready = oready = eready = None
    isocks, osocks = self.conns.Readable(), self.conns.Blocked()
    try:
      if isocks or osocks:
        iready, oready, eready = select.select(isocks, osocks, [], waittime)
      else:
        # Windoes does not seem to like empty selects, so we do this instead.
        time.sleep(waittime/2)
    except KeyboardInterrupt:
      raise
    except:
      logging.LogError('Error in select(%s/%s): %s' % (isocks, osocks,
                                                       format_exc()))
      self.conns.CleanFds()
      self.last_loop -= 1

    now = time.time()
    if not iready and not oready:
      if (isocks or osocks) and (now < self.last_loop + 1):
        logging.LogError('Spinning, pausing ...')
        time.sleep(0.1)

    return iready, oready, eready

  def Epoll(self, epoll, waittime):
    fdc = {}
    now = time.time()
    evs = []
    try:
      bbc = 0
      for c in self.conns.conns:
        try:
          if c.IsDead():
            epoll.unregister(c.fd)
          else:
            fdc[c.fd.fileno()] = c.fd
            mask = 0
            if c.IsBlocked():
              bbc += len(c.write_blocked)
              mask |= select.EPOLLOUT
            if c.IsReadable(now):
              mask |= select.EPOLLIN
            if mask:
              try:
                try:
                  epoll.modify(c.fd, mask)
                except IOError:
                  epoll.register(c.fd, mask)
              except (IOError, TypeError):
                evs.append((c.fd, select.EPOLLHUP))
                logging.LogError('Epoll mod/reg: %s(%s), mask=0x%x'
                                 '' % (c, c.fd, mask))
            else:
              epoll.unregister(c.fd)
        except (IOError, TypeError):
          # Failing to unregister is FINE, we don't complain about that.
          pass

      common.buffered_bytes[0] = bbc
      evs.extend(epoll.poll(waittime))
    except IOError:
      pass
    except KeyboardInterrupt:
      epoll.close()
      raise

    rmask = select.EPOLLIN | select.EPOLLHUP
    iready = [fdc.get(e[0]) for e in evs if e[1] & rmask]
    oready = [fdc.get(e[0]) for e in evs if e[1] & select.EPOLLOUT]

    return iready, oready, []

  def CreatePollObject(self):
    try:
      epoll = select.epoll()
      mypoll = self.Epoll
    except:
      epoll = None
      mypoll = self.Select
    return epoll, mypoll

  def Loop(self):
    self.conns.start()
    if self.ui_httpd: self.ui_httpd.start()
    if self.tunnel_manager: self.tunnel_manager.start()

    epoll, mypoll = self.CreatePollObject()
    self.last_barf = self.last_loop = time.time()

    logging.LogDebug('Entering main %s loop' % (epoll and 'epoll' or 'select'))
    while self.keep_looping:
      iready, oready, eready = mypoll(epoll, 1.1)
      now = time.time()

      if oready:
        self.ProcessWritable(oready)

      if common.buffered_bytes[0] < 1024 * self.buffer_max:
        throttle = None
      else:
        logging.LogDebug("FIXME: Nasty pause to let buffers clear!")
        time.sleep(0.1)
        throttle = 1024

      if iready:
        self.ProcessReadable(iready, throttle)

      self.ProcessDead(epoll)
      self.last_loop = now

      if now - self.last_barf > (logging.DEBUG_IO and 15 or 600):
        self.last_barf = now
        if epoll:
          epoll.close()
        epoll, mypoll = self.CreatePollObject()
        if logging.DEBUG_IO:
          logging.LogDebug('Selectable map: %s' % SELECTABLES)

    if epoll:
      epoll.close()

  def Start(self, howtoquit='CTRL+C = Stop'):
    conns = self.conns = self.conns or Connections(self)

    # If we are going to spam stdout with ugly crap, then there is no point
    # attempting the fancy stuff. This also makes us backwards compatible
    # for the most part.
    if self.logfile == 'stdio':
      if not self.ui.DAEMON_FRIENDLY: self.ui = NullUi()

    # Announce that we've started up!
    self.ui.Status('startup', message='Starting up...')
    self.ui.Notify(('Hello! This is %s v%s.'
                    ) % (self.progname, APPVER),
                    prefix='>', color=self.ui.GREEN,
                    alignright='[%s]' % howtoquit)
    config_report = [('started', sys.argv[0]), ('version', APPVER),
                     ('platform', sys.platform),
                     ('argv', ' '.join(sys.argv[1:])),
                     ('ca_certs', self.ca_certs)]
    for optf in self.rcfiles_loaded:
      config_report.append(('optfile_%s' % optf, 'ok'))
    logging.Log(config_report)

    if not socks.HAVE_SSL:
      self.ui.Notify('SECURITY WARNING: No SSL support was found, tunnels are insecure!',
                     prefix='!', color=self.ui.WHITE)
      self.ui.Notify('Please install either pyOpenSSL or python-ssl.',
                     prefix='!', color=self.ui.WHITE)

    # Create global secret
    self.ui.Status('startup', message='Collecting entropy for a secure secret...')
    logging.LogInfo('Collecting entropy for a secure secret.')
    globalSecret()
    self.ui.Status('startup', message='Starting up...')

    try:

      # Set up our listeners if we are a server.
      if self.isfrontend:
        self.ui.Notify('This is a PageKite front-end server.')
        for port in self.server_ports:
          Listener(self.server_host, port, conns, acl=self.accept_acl_file)
        for port in self.server_raw_ports:
          if port != VIRTUAL_PN and port > 0:
            Listener(self.server_host, port, conns,
                     connclass=RawConn, acl=self.accept_acl_file)

      # Create the Tunnel Manager
      self.tunnel_manager = TunnelManager(self, conns)

    except Exception, e:
      self.LogTo('stdio')
      logging.FlushLogMemory()
      if logging.DEBUG_IO:
        traceback.print_exc(file=sys.stderr)
      raise ConfigError('Configuring listeners: %s ' % e)

    # Configure logging
    if self.logfile:
      keep_open = [s.fd.fileno() for s in conns.conns]
      if self.ui_httpd: keep_open.append(self.ui_httpd.httpd.socket.fileno())
      self.LogTo(self.logfile, dont_close=keep_open)

    elif not sys.stdout.isatty():
      # Preserve sane behavior when not run at the console.
      self.LogTo('stdio')

    # Flush in-memory log, if necessary
    logging.FlushLogMemory()

    # Set up SIGHUP handler.
    if self.logfile or self.reloadfile:
      try:
        import signal
        def reopen(x,y):
          if self.logfile:
            self.LogTo(self.logfile, close_all=False)
            logging.LogDebug('SIGHUP received, reopening: %s' % self.logfile)
          if self.reloadfile:
            logging.LogDebug('SIGHUP received, reloading: %s' % self.reloadfile)
            #print 'SIGHUP received, reloading: %s' % self.reloadfile
            try:
                self.ConfigureFromFile(self.reloadfile)
            except Exception as e:
                logging.LogError ('Could not reload config: %s' % e)

        signal.signal(signal.SIGHUP, reopen)
      except Exception:
        logging.LogError('Warning: signal handler unavailable, logrotate will not work.')

    # Initialize nameserver client
    if (self.server_ns_update):
        # Option: server_ns_update=server,zone,tsigname,tsigkey[,address]
        options = self.server_ns_update.split(",")
        logging.LogInfo('Configured to update nameserver %s for zone %s' % (options[0], options[1]))
        self.dnsclient = DnsClient(self, options[0], options[1], options[2], options[3], options[4] if len(options)>4 else None)

    # Disable compression in OpenSSL
    if socks.HAVE_SSL and not self.enable_sslzlib:
      socks.DisableSSLCompression()

    # Daemonize!
    if self.daemonize:
      self.Daemonize()

    # Create PID file
    if self.pidfile:
      pf = open(self.pidfile, 'w')
      pf.write('%s\n' % os.getpid())
      pf.close()

    # Do this after creating the PID and log-files.
    if self.daemonize:
      os.chdir('/')

    # Drop privileges, if we have any.
    if self.setgid:
      os.setgid(self.setgid)
    if self.setuid:
      os.setuid(self.setuid)
    if self.setuid or self.setgid:
      logging.Log([('uid', os.getuid()), ('gid', os.getgid())])

    # Make sure we have what we need
    if self.require_all:
      self.CreateTunnels(conns)
      self.CheckAllTunnels(conns)

    # Finally, run our select loop.
    self.Loop()

    self.ui.Status('exiting', message='Stopping...')
    logging.Log([('stopping', 'pagekite.py')])
    if self.ui_httpd:
      self.ui_httpd.quit()
    if self.tunnel_manager:
      self.tunnel_manager.quit()
    if self.conns:
      if self.conns.auth: self.conns.auth.quit()
      for conn in self.conns.conns:
        conn.Cleanup()


##[ Main ]#####################################################################

def Main(pagekite, configure, uiclass=NullUi,
                              progname=None, appver=APPVER,
                              http_handler=None, http_server=None):
  crashes = 0
  while True:
    ui = uiclass()
    logging.ResetLog()


    pk = pagekite(ui=ui, http_handler=http_handler, http_server=http_server)

    common.pko = pk
    try:
      try:
        try:
          configure(pk)
        except SystemExit, status:
          sys.exit(status)
        except Exception, e:
          raise ConfigError(e)

        # Start Pagekite
        pk.Start()

      except (ConfigError, getopt.GetoptError), msg:
        pk.FallDown(msg, help=False, noexit=False)

      except KeyboardInterrupt, msg:
        pk.FallDown(None, help=False, noexit=True)
        return

    except SystemExit, status:
      sys.exit(status)

    except Exception, msg:
      # Close system
      traceback.print_exc(file=sys.stderr)
      pk.FallDown(msg, help=False, noexit=pk.main_loop)
      crashes = min(9, crashes+1)

    if not pk.main_loop:
      return

    # Exponential fall-back.
    logging.LogDebug('Restarting in %d seconds...' % (2 ** crashes))
    time.sleep(2 ** crashes)


def Configure(pk):
  if '--appver' in sys.argv:
    print '%s' % APPVER
    sys.exit(0)

  if '--clean' not in sys.argv and '--help' not in sys.argv:
    if os.path.exists(pk.rcfile):
      pk.ConfigureFromFile()

  pk.Configure(sys.argv[1:])

  if '--settings' in sys.argv:
    pk.PrintSettings(safe=True)
    sys.exit(0)

  pk.CheckConfig()

  if pk.added_kites:
    if (pk.save or
        pk.ui.AskYesNo('Save settings to %s?' % pk.rcfile,
                       default=(len(pk.backends.keys()) > 0))):
      pk.SaveUserConfig()
    pk.servers_new_only = 'Once'
  elif pk.save:
    pk.SaveUserConfig(quiet=True)

  if ('--list' in sys.argv or
      pk.kite_add or pk.kite_remove or pk.kite_only or pk.kite_disable):
    pk.ListKites()
    sys.exit(0)
