"""
This is a class implementing a flexible metric-store and an HTTP
thread for browsing the numbers.
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

import getopt
import os
import random
import re
import select
import socket
import struct
import sys
import threading
import time
import traceback
import urllib

import dns.query
import dns.tsigkeyring
import dns.update

class DnsClient ():
    
    def __init__(self, pagekite):
    
        self.pagekite = pagekite
        self.dnsclient = None

    def update (self, hostname, address = None):
        
        if address == None: address = self.pagekite.public_address
        
        #keyring = dns.tsigkeyring.from_text({
        #    'host-example.' : 'XXXXXXXXXXXXXXXXXXXXXX=='
        #})
        
        #update = dns.update.Update('dyn.test.example', keyring=keyring)
        #update.replace('host', 300, 'a', address)
        
        #response = dns.query.tcp(update, '10.0.0.1')
    
    def remove (self, hostname):
        pass

