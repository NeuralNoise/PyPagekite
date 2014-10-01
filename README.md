# PageQuite.py #

PageQuite is a fork of `pagekite.py`, a fast and reliable tool to make 
services visible to the public Internet.

This fork aims to trim down the software to a minimal service that
can be run in unattended mode as a service and to make the frontend
(relay servers) easier to integrate in an automated environment. It
removes configuration interfaces and several options. It also
provides config reloading capabilities, useful for frontends
that need to be automated. It also removes internal HTTP server,
focusing on HTTP proxying and raw TCP tunnelling.

 
