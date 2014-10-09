# Makefile 

PYTHON=`which python`
DESTDIR=/
BUILDIR=$(CURDIR)/build
PROJECT=pagekite

VERSION=`python setup.py --version`

all:
	@echo "make source - Create source package"
	@echo "make install - Install on local system"
	@echo "make builddeb - Generate a deb package"
	@echo "make clean - Get rid of scratch and byte files"

source:
	$(PYTHON) setup.py sdist $(COMPILE)

install:
	$(PYTHON) setup.py install --root $(DESTDIR) $(COMPILE)

builddeb:
	# create
	$(PYTHON) setup.py sdist $(COMPILE) --dist-dir=$(BUILDIR) -k 
    	
	cd pagekite-$(VERSION)

	# replacements
	@sed -e "s/@VERSION@/$(VERSION)/g" < debian/control.in >debian/control
	@sed -e "s/@VERSION@/$(VERSION)/g" < debian/copyright.in >debian/copyright
	@sed -e "s/@VERSION@/$(VERSION)/g" -e "s/@DATE@/`date -R`/g" < debian/changelog.in >debian/changelog
	@ln -fs ../etc/logrotate.d/pagekite.debian debian/pagekite.logrotate
	@ln -fs ../etc/init.d/pagekite.debian debian/init.d	
    
	# build the package
	dpkg-buildpackage -rfakeroot

clean:
	#$(PYTHON) setup.py clean
	$(MAKE) -f $(CURDIR)/debian/rules clean
	rm -rf build/ MANIFEST pagekite-$(VERSION) pagekite.egg-info
	find . -name '*.pyc' -delete

