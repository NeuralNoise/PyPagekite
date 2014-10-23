"""
This is a class implementing a flexible metric-store and an HTTP
thread for browsing the numbers.
"""
##############################################################################
import logging
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

from multiprocessing.pool import ThreadPool

class DnsClient ():
    """
    Performs synchronous or asynchronous update to a nameserver.
    This class assumes that hostnames belong to a given DNS zone, and
    the domainname is removed from the hostname in order to perform the update.
    """
    
    def __init__(self, pagekite, nshost, zone, tsigname, tsigkey, default_address = "127.0.0.1"):
    
        self.pagekite = pagekite
        self.nshost = nshost
        self.zone = zone
        self.default_address = default_address       
        
        self.pool = ThreadPool(processes=6) 
        
        self.keyring = dns.tsigkeyring.from_text({
            tsigname : tsigkey
        })

    def _hostname (self, fqdn):
        
        if (fqdn.endswith(self.zone)):
            return fqdn[:len(fqdn)-len(self.zone)-1]
        else:
            return fqdn

    def update_async (self, hostname, address = None):
        self.pool.apply_async(self.update, [hostname, address])

    def update (self, hostname, address = None):
        
        try:
        
            if address == None: address = self.default_address
                    
            update = dns.update.Update(self.zone, keyring=self.keyring)
            update.replace(self._hostname(hostname), 300, 'a', address)
            response = dns.query.tcp(update, self.nshost)
            
        except Exception as e:
            
            logging.LogError("Could not update hostname %s: %s" % (hostname, e))
        
        #print response
    
    def delete_async (self, hostname):
        self.pool.apply_async(self.delete, [hostname])
    
    def delete (self, hostname):
        
        try:
        
            update = dns.update.Update(self.zone, keyring=self.keyring)
            update.delete(self._hostname(hostname))
            response = dns.query.tcp(update, self.nshost)
        
        except Exception as e:
            
            logging.LogError("Could not update hostname %s: %s" % (hostname, e))
        
        #print response

if __name__ == "__main__":
    
    c = DnsClient('servip', 'zone', {})
    c.delete_async ("hostname")
    time.sleep(5)
    
