#!/usr/bin/env python
"""CLI for serialising metadata updates on an s3-hosted yum repository.
"""
import os
import sys
import time
import urlparse
import tempfile
import shutil
import optparse
import logging
import collections
import yum
import boto
import subprocess
sys.path.insert(1, "/vagrant/s3yum-updater/vendor/pexpect")
import pexpect

sys.path.insert(1, "/vagrant/s3yum-updater/vendor/createrepo")
import createrepo

# Hack for creating s3 urls
urlparse.uses_relative.append('s3')
urlparse.uses_netloc.append('s3')


class LoggerCallback(object):
    def errorlog(self, message):
        logging.error(message)

    def log(self, message):
        message = message.strip()
        if message:
            logging.debug(message)


class S3Grabber(object):
    def __init__(self, baseurl, visibility):
        logging.info('S3Grabber: %s', baseurl)
        base = urlparse.urlsplit(baseurl)
        self.baseurl = baseurl
        self.basepath = base.path.lstrip('/')
        self.bucket = boto.connect_s3(os.environ['AWS_ACCESS_KEY'], os.environ['AWS_SECRET_KEY']).get_bucket(base.netloc)
        self.visibility = visibility

    def check(self, url):
        if url.startswith(self.baseurl):
            url = url[len(self.baseurl):].lstrip('/')
        logging.info("checking if key exists: %s", os.path.join(self.basepath, url))
        return self.bucket.get_key(os.path.join(self.basepath, url))

    def urlgrab(self, url, filename, **kwargs):
        logging.info('urlgrab: %s', filename)
        key = self.check(url)
        if not key:
            raise createrepo.grabber.URLGrabError(14, '%s not found' % url)
        logging.info('downloading: %s', key.name)
        key.get_contents_to_filename(filename)
        return filename

    def syncdir(self, dir, url):
        """Copy all files in dir to url, removing any existing keys."""
        base = os.path.join(self.basepath, url)
        existing_keys = list(self.bucket.list(base))
        new_keys = []
        for filename in sorted(os.listdir(dir)):
            key = self.bucket.new_key(os.path.join(base, filename))
            key.set_contents_from_filename(os.path.join(dir, filename))
            key.set_acl(self.visibility)
            new_keys.append(key.name)
            logging.info('uploading: %s', key.name)
        for key in existing_keys:
            if key.name not in new_keys:
                key.delete()
                logging.info('removing: %s', key.name)

    def upload(self, file, url):
        """Copy file to url."""
        target = os.path.join(self.basepath, url)
        logging.info("upload: %s to %s", (file, target))
        filename = os.path.basename(file)
        key = self.bucket.new_key(target)
        key.set_contents_from_filename(file)
        key.set_acl(self.visibility)


class FileGrabber(object):
    def __init__(self, baseurl):
        logging.info('FileGrabber: %s', baseurl)
        self.basepath = urlparse.urlsplit(baseurl).path
        logging.info('basepath: %s', self.basepath)

    def urlgrab(self, url, filename, **kwargs):
        """Expects url of the form: file:///path/to/file"""
        filepath = urlparse.urlsplit(url).path
        realfilename = os.path.join(self.basepath, filepath)
        logging.info('fetching: %s', realfilename)
        # The filename name is REALLY important for createrepo as it derives
        # the base path from its own tempdir, etc.
        os.symlink(realfilename, filename)
        return filename

def sign(rpmfile):
    """Requires a proper ~/.rpmmacros file. See <http://fedoranews.org/tchung/gpg/>"""
    cmd = "rpm --resign '%s'" % rpmfile
    logging.info(cmd)
    try:
        child = pexpect.spawn(cmd)
        child.expect('Enter pass phrase: ')
        child.sendline('')
        child.expect(pexpect.EOF)
    except pexpect.EOF, e:
        print "Unable to sign package '%s' - %s" % (rpmfile, child.before)
        logging.error("Unable to sign package: %s", e)
        exit(1)

def setup_repository(repo, repopath):
    """Make sure a repo is present at repopath"""
    key = repo._grab.check("repodata/repomd.xml")
    if key:
        logging.info("Existing repository detected.")
    else:
        path_to_empty_repo = os.path.join(os.path.dirname(__file__), "empty-repo", "repodata")
        logging.info("Empty repository detected. Initializing with empty repodata...")
        repo._grab.syncdir(path_to_empty_repo, "repodata")

def update_repodata(repopath, rpmfiles, options):
    logging.info('rpmfiles: %s', rpmfiles)
    tmpdir = tempfile.mkdtemp()
    s3base = urlparse.urlunsplit(('s3', options.bucket, repopath, '', ''))
    s3grabber = S3Grabber(s3base, options.visibility)
    filegrabber = FileGrabber("file://" + os.getcwd())

    # Set up temporary repo that will fetch repodata from s3
    yumbase = yum.YumBase()
    yumbase.preconf.disabled_plugins = '*'
    yumbase.conf.cachedir = os.path.join(tmpdir, 'cache')
    yumbase.repos.disableRepo('*')
    repo = yumbase.add_enable_repo('s3')
    repo._grab = s3grabber

    setup_repository(repo, repopath)

    # Ensure that missing base path doesn't cause trouble
    repo._sack = yum.sqlitesack.YumSqlitePackageSack(
        createrepo.readMetadata.CreaterepoPkgOld)

    # Create metadata generator
    mdconf = createrepo.MetaDataConfig()
    mdconf.directory = tmpdir
    mdgen = createrepo.MetaDataGenerator(mdconf, LoggerCallback())
    mdgen.tempdir = tmpdir

    # Combine existing package sack with new rpm file list
    new_packages = []
    for rpmfile in rpmfiles:
        rpmfile = os.path.realpath(rpmfile)
        logging.info("rpmfile: %s", rpmfile)

        if options.sign:
            sign(rpmfile)

        mdgen._grabber = filegrabber
        newpkg = mdgen.read_in_package("file://" + rpmfile, repopath)
        mdgen._grabber = s3grabber

        # newpkg._baseurl = '/'   # don't leave s3 base urls in primary metadata
        older_pkgs = yumbase.pkgSack.searchNevra(name=newpkg.name)

        # Remove older versions of this package (or if it's the same version)
        for i, older in enumerate(reversed(older_pkgs), 1):
            if i > options.keep or older.pkgtup == newpkg.pkgtup:
                yumbase.pkgSack.delPackage(older)
                logging.info('ignoring: %s', older.ui_nevra)
        new_packages.append(newpkg)
    mdconf.pkglist = list(yumbase.pkgSack) + new_packages

    # Write out new metadata to tmpdir
    mdgen.doPkgMetadata()
    mdgen.doRepoMetadata()
    mdgen.doFinalMove()

    # Upload rpm files to destination
    for rpmfile in rpmfiles:
        rpmfile = os.path.realpath(rpmfile)
        logging.info("rpmfile: %s", rpmfile)
        s3grabber.upload(rpmfile, os.path.basename(rpmfile))

    # Replace metadata on s3
    s3grabber.syncdir(os.path.join(tmpdir, 'repodata'), 'repodata')

    shutil.rmtree(tmpdir)

def main(options, args):
    loglevel = ('WARNING', 'INFO', 'DEBUG')[min(2, options.verbose)]
    logging.basicConfig(
        filename=options.logfile,
        level=logging.getLevelName(loglevel),
        format='%(asctime)s %(levelname)s %(message)s',
    )

    update_repodata(options.repopath, args, options)


if __name__ == '__main__':
    parser = optparse.OptionParser()
    parser.add_option('-b', '--bucket', default='my-bucket')
    parser.add_option('-p', '--repopath', default='')
    parser.add_option('-k', '--keep', type='int', default=2)
    parser.add_option('-v', '--verbose', action='count', default=0)
    parser.add_option('--visibility', default='private')
    parser.add_option('-s', '--sign', action='count', default=0)
    parser.add_option('-l', '--logfile')
    options, args = parser.parse_args()
    main(options, args)