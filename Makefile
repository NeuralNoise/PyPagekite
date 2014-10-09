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

	# replacements
	@sed -e "s/@VERSION@/$(VERSION)/g" < pagekite-$(VERSION)/debian/control.in > pagekite-$(VERSION)/debian/control
	@sed -e "s/@VERSION@/$(VERSION)/g" < pagekite-$(VERSION)/debian/copyright.in > pagekite-$(VERSION)/debian/copyright
	@sed -e "s/@VERSION@/$(VERSION)/g" -e "s/@DATE@/`date -R`/g" < pagekite-$(VERSION)/debian/changelog.in > pagekite-$(VERSION)/debian/changelog
	@ln -fs ../etc/logrotate.d/pagekite.debian pagekite-$(VERSION)/debian/pagekite.logrotate
	@ln -fs ../etc/init.d/pagekite.debian pagekite-$(VERSION)/debian/init.d	
    
	# build the package
	mv pagekite-$(VERSION) $(BUILDIR)
	cd $(BUILDIR)/pagekite-$(VERSION) ; dpkg-buildpackage -rfakeroot

clean:
	#$(PYTHON) setup.py clean
	$(MAKE) -f $(CURDIR)/debian/rules clean
	rm -rf build/ MANIFEST pagekite-$(VERSION)* pagekite.egg-info
	find . -name '*.pyc' -delete

