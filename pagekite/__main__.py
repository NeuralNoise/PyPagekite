#!/usr/bin/env python
"""
This is the pagekite.py Main() function.
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
import sys
from pagekite import pk
from pagekite import httpd

if __name__ == "__main__":

  import pagekite.ui.nullui
  uiclass = pagekite.ui.nullui.NullUi

  pk.Main(pk.PageKite, pk.Configure,
          uiclass=uiclass,
          http_handler=httpd.UiRequestHandler,
          http_server=httpd.UiHttpServer)

