#############################################################################
#
# Copyright (c) 2005 Zope Corporation and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE.
#
##############################################################################
"""Python easy_install API

This module provides a high-level Python API for installing packages.
It doesn't install scripts.  It uses setuptools and requires it to be
installed.
"""

import distutils.errors
import fnmatch
import glob
import logging
import os
import pkg_resources
import py_compile
import re
import setuptools.archive_util
import setuptools.command.setopt
import setuptools.package_index
import shutil
import subprocess
import sys
import tempfile
import zc.buildout
import zipimport

_oprp = getattr(os.path, 'realpath', lambda path: path)
def realpath(path):
    return os.path.normcase(os.path.abspath(_oprp(path)))

default_index_url = os.environ.get(
    'buildout-testing-index-url',
    'http://pypi.python.org/simple',
    )

logger = logging.getLogger('zc.buildout.easy_install')

url_match = re.compile('[a-z0-9+.-]+://').match

is_win32 = sys.platform == 'win32'
is_jython = sys.platform.startswith('java')

if is_jython:
    import java.lang.System
    jython_os_name = (java.lang.System.getProperties()['os.name']).lower()


setuptools_loc = pkg_resources.working_set.find(
    pkg_resources.Requirement.parse('setuptools')
    ).location

# Include buildout and setuptools eggs in paths.  We prevent dupes just to
# keep from duplicating any log messages about them.
buildout_loc = pkg_resources.working_set.find(
    pkg_resources.Requirement.parse('zc.buildout')).location
buildout_and_setuptools_path = [setuptools_loc]
if os.path.normpath(setuptools_loc) != os.path.normpath(buildout_loc):
    buildout_and_setuptools_path.append(buildout_loc)

def _get_system_paths(executable):
    """Return lists of standard lib and site paths for executable.
    """
    # We want to get a list of the site packages, which is not easy.
    # The canonical way to do this is to use
    # distutils.sysconfig.get_python_lib(), but that only returns a
    # single path, which does not reflect reality for many system
    # Pythons, which have multiple additions.  Instead, we start Python
    # with -S, which does not import site.py and set up the extra paths
    # like site-packages or (Ubuntu/Debian) dist-packages and
    # python-support. We then compare that sys.path with the normal one
    # (minus user packages if this is Python 2.6, because we don't
    # support those (yet?).  The set of the normal one minus the set of
    # the ones in ``python -S`` is the set of packages that are
    # effectively site-packages.
    #
    # The given executable might not be the current executable, so it is
    # appropriate to do another subprocess to figure out what the
    # additional site-package paths are. Moreover, even if this
    # executable *is* the current executable, this code might be run in
    # the context of code that has manipulated the sys.path--for
    # instance, to add local zc.buildout or setuptools eggs.
    def get_sys_path(*args, **kwargs):
        cmd = [executable]
        cmd.extend(args)
        cmd.extend([
            "-c", "import sys, os;"
            "print repr([os.path.normpath(p) for p in sys.path if p])"])
        # Windows needs some (as yet to be determined) part of the real env.
        env = os.environ.copy()
        env.update(kwargs)
        _proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        stdout, stderr = _proc.communicate();
        if _proc.returncode:
            raise RuntimeError(
                'error trying to get system packages:\n%s' % (stderr,))
        res = eval(stdout.strip())
        try:
            res.remove('.')
        except ValueError:
            pass
        return res
    stdlib = get_sys_path('-S') # stdlib only
    no_user_paths = get_sys_path(PYTHONNOUSERSITE='x')
    site_paths = [p for p in no_user_paths if p not in stdlib]
    return (stdlib, site_paths)

def _get_version_info(executable):
    cmd = [executable, '-Sc', 'import sys; print repr(sys.version_info)']
    _proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = _proc.communicate();
    if _proc.returncode:
        raise RuntimeError(
            'error trying to get system packages:\n%s' % (stderr,))
    return eval(stdout.strip())


class IncompatibleVersionError(zc.buildout.UserError):
    """A specified version is incompatible with a given requirement.
    """

_versions = {sys.executable: '%d.%d' % sys.version_info[:2]}
def _get_version(executable):
    try:
        return _versions[executable]
    except KeyError:
        cmd = _safe_arg(executable) + ' -V'
        p = subprocess.Popen(cmd,
                             shell=True,
                             stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT,
                             close_fds=not is_win32)
        i, o = (p.stdin, p.stdout)
        i.close()
        version = o.read().strip()
        o.close()
        pystring, version = version.split()
        assert pystring == 'Python'
        version = re.match('(\d[.]\d)([.].*\d)?$', version).group(1)
        _versions[executable] = version
        return version

FILE_SCHEME = re.compile('file://', re.I).match


class AllowHostsPackageIndex(setuptools.package_index.PackageIndex):
    """Will allow urls that are local to the system.

    No matter what is allow_hosts.
    """
    def url_ok(self, url, fatal=False):
        if FILE_SCHEME(url):
            return True
        return setuptools.package_index.PackageIndex.url_ok(self, url, False)


_indexes = {}
def _get_index(executable, index_url, find_links, allow_hosts=('*',),
               path=None):
    # If path is None, the index will use sys.path.  If you provide an empty
    # path ([]), it will complain uselessly about missing index pages for
    # packages found in the paths that you expect to use.  Therefore, this path
    # is always the same as the _env path in the Installer.
    key = executable, index_url, tuple(find_links)
    index = _indexes.get(key)
    if index is not None:
        return index

    if index_url is None:
        index_url = default_index_url
    index = AllowHostsPackageIndex(
        index_url, hosts=allow_hosts, search_path=path,
        python=_get_version(executable)
        )

    if find_links:
        index.add_find_links(find_links)

    _indexes[key] = index
    return index

clear_index_cache = _indexes.clear

if is_win32:
    # work around spawn lamosity on windows
    # XXX need safe quoting (see the subprocess.list2cmdline) and test
    def _safe_arg(arg):
        return '"%s"' % arg
else:
    _safe_arg = str

# The following string is used to run easy_install in
# Installer._call_easy_install.  It is started with python -S (that is,
# don't import site at start).  That flag, and all of the code in this
# snippet above the last two lines, exist to work around a relatively rare
# problem.  If
#
# - your buildout configuration is trying to install a package that is within
#   a namespace package, and
#
# - you use a Python that has a different version of this package
#   installed in in its site-packages using
#   --single-version-externally-managed (that is, using the mechanism
#   sometimes used by system packagers:
#   http://peak.telecommunity.com/DevCenter/setuptools#install-command ), and
#
# - the new package tries to do sys.path tricks in the setup.py to get a
#   __version__,
#
# then the older package will be loaded first, making the setup version
# the wrong number. While very arguably packages simply shouldn't do
# the sys.path tricks, some do, and we don't want buildout to fall over
# when they do.
#
# The namespace packages installed in site-packages with
# --single-version-externally-managed use a mechanism that cause them to
# be processed when site.py is imported  (see
# http://mail.python.org/pipermail/distutils-sig/2009-May/011730.html
# for another description of the problem).  Simply starting Python with
# -S addresses the problem in Python 2.4 and 2.5, but Python 2.6's
# distutils imports a value from the site module, so we unfortunately
# have to do more drastic surgery in the _easy_install_cmd code below.
#
# Here's an example of the .pth files created by setuptools when using that
# flag:
#
# import sys,new,os;
# p = os.path.join(sys._getframe(1).f_locals['sitedir'], *('<NAMESPACE>',));
# ie = os.path.exists(os.path.join(p,'__init__.py'));
# m = not ie and sys.modules.setdefault('<NAMESPACE>',new.module('<NAMESPACE>'));
# mp = (m or []) and m.__dict__.setdefault('__path__',[]);
# (p not in mp) and mp.append(p)
#
# The code, below, then, runs under -S, indicating that site.py should
# not be loaded initially.  It gets the initial sys.path under these
# circumstances, and then imports site (because Python 2.6's distutils
# will want it, as mentioned above). It then reinstates the old sys.path
# value. Then it removes namespace packages (created by the setuptools
# code above) from sys.modules.  It identifies namespace packages by
# iterating over every loaded module.  It first looks if there is a
# __path__, so it is a package; and then it sees if that __path__ does
# not have an __init__.py.  (Note that PEP 382,
# http://www.python.org/dev/peps/pep-0382, makes it possible to have a
# namespace package that has an __init__.py, but also should make it
# unnecessary for site.py to preprocess these packages, so it should be
# fine, as far as can be guessed as of this writing.)  Finally, it
# imports easy_install and runs it.

_easy_install_cmd = _safe_arg('''\
import sys,os;\
p = sys.path[:];\
import site;\
sys.path[:] = p;\
[sys.modules.pop(k) for k, v in sys.modules.items()\
 if hasattr(v, '__path__') and len(v.__path__)==1 and\
 not os.path.exists(os.path.join(v.__path__[0],'__init__.py'))];\
from setuptools.command.easy_install import main;\
main()''')


class Installer:

    _versions = {}
    _download_cache = None
    _install_from_cache = False
    _prefer_final = True
    _use_dependency_links = True
    _allow_picked_versions = True
    _always_unzip = False
    _include_site_packages = True
    _allowed_eggs_from_site_packages = ('*',)

    def __init__(self,
                 dest=None,
                 links=(),
                 index=None,
                 executable=sys.executable,
                 always_unzip=None,
                 path=None,
                 newest=True,
                 versions=None,
                 use_dependency_links=None,
                 include_site_packages=None,
                 allowed_eggs_from_site_packages=None,
                 allow_hosts=('*',)
                 ):
        self._dest = dest
        self._allow_hosts = allow_hosts

        if self._install_from_cache:
            if not self._download_cache:
                raise ValueError("install_from_cache set to true with no"
                                 " download cache")
            links = ()
            index = 'file://' + self._download_cache

        if use_dependency_links is not None:
            self._use_dependency_links = use_dependency_links
        self._links = links = list(_fix_file_links(links))
        if self._download_cache and (self._download_cache not in links):
            links.insert(0, self._download_cache)

        self._index_url = index
        self._executable = executable
        if always_unzip is not None:
            self._always_unzip = always_unzip
        path = (path and path[:] or [])
        if include_site_packages is not None:
            self._include_site_packages = include_site_packages
        if allowed_eggs_from_site_packages is not None:
            self._allowed_eggs_from_site_packages = tuple(
                allowed_eggs_from_site_packages)
        stdlib, self._site_packages = _get_system_paths(executable)
        version_info = _get_version_info(executable)
        if version_info == sys.version_info:
            # Maybe we can add the buildout and setuptools path.  If we
            # are including site_packages, we only have to include the extra
            # bits here, so we don't duplicate.  On the other hand, if we
            # are not including site_packages, we only want to include the
            # parts that are not in site_packages, so the code is the same.
            path.extend(
                set(buildout_and_setuptools_path).difference(
                    self._site_packages))
        if self._include_site_packages:
            path.extend(self._site_packages)
        # else we could try to still include the buildout_and_setuptools_path
        # if the elements are not in site_packages, but we're not bothering
        # with this optimization for now, in the name of code simplicity.
        if dest is not None and dest not in path:
            path.insert(0, dest)
        self._path = path
        if self._dest is None:
            newest = False
        self._newest = newest
        self._env = pkg_resources.Environment(path,
                                              python=_get_version(executable))
        self._index = _get_index(executable, index, links, self._allow_hosts,
                                 self._path)

        if versions is not None:
            self._versions = versions

    _allowed_eggs_from_site_packages_regex = None
    def allow_site_package_egg(self, name):
        if (not self._include_site_packages or
            not self._allowed_eggs_from_site_packages):
            # If the answer is a blanket "no," perform a shortcut.
            return False
        if self._allowed_eggs_from_site_packages_regex is None:
            pattern = '(%s)' % (
                '|'.join(
                    fnmatch.translate(name)
                    for name in self._allowed_eggs_from_site_packages),
                )
            self._allowed_eggs_from_site_packages_regex = re.compile(pattern)
        return bool(self._allowed_eggs_from_site_packages_regex.match(name))

    def _satisfied(self, req, source=None):
        # We get all distributions that match the given requirement.  If we are
        # not supposed to include site-packages for the given egg, we also
        # filter those out. Even if include_site_packages is False and so we
        # have excluded site packages from the _env's paths (see
        # Installer.__init__), we need to do the filtering here because an
        # .egg-link, such as one for setuptools or zc.buildout installed by
        # zc.buildout.buildout.Buildout.bootstrap, can indirectly include a
        # path in our _site_packages.
        dists = [dist for dist in self._env[req.project_name] if (
                    dist in req and (
                        dist.location not in self._site_packages or
                        self.allow_site_package_egg(dist.project_name))
                    )
                ]
        if not dists:
            logger.debug('We have no distributions for %s that satisfies %r.',
                         req.project_name, str(req))

            return None, self._obtain(req, source)

        # Note that dists are sorted from best to worst, as promised by
        # env.__getitem__

        for dist in dists:
            if (dist.precedence == pkg_resources.DEVELOP_DIST):
                logger.debug('We have a develop egg: %s', dist)
                return dist, None

        # Special common case, we have a specification for a single version:
        specs = req.specs
        if len(specs) == 1 and specs[0][0] == '==':
            logger.debug('We have the distribution that satisfies %r.',
                         str(req))
            return dists[0], None

        if self._prefer_final:
            fdists = [dist for dist in dists
                      if _final_version(dist.parsed_version)
                      ]
            if fdists:
                # There are final dists, so only use those
                dists = fdists

        if not self._newest:
            # We don't need the newest, so we'll use the newest one we
            # find, which is the first returned by
            # Environment.__getitem__.
            return dists[0], None

        best_we_have = dists[0] # Because dists are sorted from best to worst

        # We have some installed distros.  There might, theoretically, be
        # newer ones.  Let's find out which ones are available and see if
        # any are newer.  We only do this if we're willing to install
        # something, which is only true if dest is not None:

        if self._dest is not None:
            best_available = self._obtain(req, source)
        else:
            best_available = None

        if best_available is None:
            # That's a bit odd.  There aren't any distros available.
            # We should use the best one we have that meets the requirement.
            logger.debug(
                'There are no distros available that meet %r.\n'
                'Using our best, %s.',
                str(req), best_available)
            return best_we_have, None

        if self._prefer_final:
            if _final_version(best_available.parsed_version):
                if _final_version(best_we_have.parsed_version):
                    if (best_we_have.parsed_version
                        <
                        best_available.parsed_version
                        ):
                        return None, best_available
                else:
                    return None, best_available
            else:
                if (not _final_version(best_we_have.parsed_version)
                    and
                    (best_we_have.parsed_version
                     <
                     best_available.parsed_version
                     )
                    ):
                    return None, best_available
        else:
            if (best_we_have.parsed_version
                <
                best_available.parsed_version
                ):
                return None, best_available

        logger.debug(
            'We have the best distribution that satisfies %r.',
            str(req))
        return best_we_have, None

    def _load_dist(self, dist):
        dists = pkg_resources.Environment(
            dist.location,
            python=_get_version(self._executable),
            )[dist.project_name]
        assert len(dists) == 1
        return dists[0]

    def _call_easy_install(self, spec, ws, dest, dist):

        tmp = tempfile.mkdtemp(dir=dest)
        try:
            path = setuptools_loc

            args = ('-Sc', _easy_install_cmd, '-mUNxd', _safe_arg(tmp))
            if self._always_unzip:
                args += ('-Z', )
            level = logger.getEffectiveLevel()
            if level > 0:
                args += ('-q', )
            elif level < 0:
                args += ('-v', )

            args += (_safe_arg(spec), )

            if level <= logging.DEBUG:
                logger.debug('Running easy_install:\n%s "%s"\npath=%s\n',
                             self._executable, '" "'.join(args), path)

            if is_jython:
                extra_env = dict(os.environ, PYTHONPATH=path)
            else:
                args += (dict(os.environ, PYTHONPATH=path), )

            sys.stdout.flush() # We want any pending output first

            if is_jython:
                exit_code = subprocess.Popen(
                [_safe_arg(self._executable)] + list(args),
                env=extra_env).wait()
            else:
                exit_code = os.spawnle(
                    os.P_WAIT, self._executable, _safe_arg (self._executable),
                    *args)

            dists = []
            env = pkg_resources.Environment(
                [tmp],
                python=_get_version(self._executable),
                )
            for project in env:
                dists.extend(env[project])

            if exit_code:
                logger.error(
                    "An error occured when trying to install %s. "
                    "Look above this message for any errors that "
                    "were output by easy_install.",
                    dist)

            if not dists:
                raise zc.buildout.UserError("Couldn't install: %s" % dist)

            if len(dists) > 1:
                logger.warn("Installing %s\n"
                            "caused multiple distributions to be installed:\n"
                            "%s\n",
                            dist, '\n'.join(map(str, dists)))
            else:
                d = dists[0]
                if d.project_name != dist.project_name:
                    logger.warn("Installing %s\n"
                                "Caused installation of a distribution:\n"
                                "%s\n"
                                "with a different project name.",
                                dist, d)
                if d.version != dist.version:
                    logger.warn("Installing %s\n"
                                "Caused installation of a distribution:\n"
                                "%s\n"
                                "with a different version.",
                                dist, d)

            result = []
            for d in dists:
                newloc = os.path.join(dest, os.path.basename(d.location))
                if os.path.exists(newloc):
                    if os.path.isdir(newloc):
                        shutil.rmtree(newloc)
                    else:
                        os.remove(newloc)
                os.rename(d.location, newloc)

                [d] = pkg_resources.Environment(
                    [newloc],
                    python=_get_version(self._executable),
                    )[d.project_name]

                result.append(d)

            return result

        finally:
            shutil.rmtree(tmp)

    def _obtain(self, requirement, source=None):
        # initialize out index for this project:
        index = self._index

        if index.obtain(requirement) is None:
            # Nothing is available.
            return None

        # Filter the available dists for the requirement and source flag.  If
        # we are not supposed to include site-packages for the given egg, we
        # also filter those out. Even if include_site_packages is False and so
        # we have excluded site packages from the _env's paths (see
        # Installer.__init__), we need to do the filtering here because an
        # .egg-link, such as one for setuptools or zc.buildout installed by
        # zc.buildout.buildout.Buildout.bootstrap, can indirectly include a
        # path in our _site_packages.
        dists = [dist for dist in index[requirement.project_name] if (
                    dist in requirement and (
                        dist.location not in self._site_packages or
                        self.allow_site_package_egg(dist.project_name))
                    and (
                        (not source) or
                        (dist.precedence == pkg_resources.SOURCE_DIST))
                    )
                 ]

        # If we prefer final dists, filter for final and use the
        # result if it is non empty.
        if self._prefer_final:
            fdists = [dist for dist in dists
                      if _final_version(dist.parsed_version)
                      ]
            if fdists:
                # There are final dists, so only use those
                dists = fdists

        # Now find the best one:
        best = []
        bestv = ()
        for dist in dists:
            distv = dist.parsed_version
            if distv > bestv:
                best = [dist]
                bestv = distv
            elif distv == bestv:
                best.append(dist)

        if not best:
            return None

        if len(best) == 1:
            return best[0]

        if self._download_cache:
            for dist in best:
                if (realpath(os.path.dirname(dist.location))
                    ==
                    self._download_cache
                    ):
                    return dist

        best.sort()
        return best[-1]

    def _fetch(self, dist, tmp, download_cache):
        if (download_cache
            and (realpath(os.path.dirname(dist.location)) == download_cache)
            ):
            return dist

        new_location = self._index.download(dist.location, tmp)
        if (download_cache
            and (realpath(new_location) == realpath(dist.location))
            and os.path.isfile(new_location)
            ):
            # setuptools avoids making extra copies, but we want to copy
            # to the download cache
            shutil.copy2(new_location, tmp)
            new_location = os.path.join(tmp, os.path.basename(new_location))

        return dist.clone(location=new_location)

    def _get_dist(self, requirement, ws, always_unzip):

        __doing__ = 'Getting distribution for %r.', str(requirement)

        # Maybe an existing dist is already the best dist that satisfies the
        # requirement
        dist, avail = self._satisfied(requirement)

        if dist is None:
            if self._dest is not None:
                logger.info(*__doing__)

            # Retrieve the dist:
            if avail is None:
                raise MissingDistribution(requirement, ws)

            # We may overwrite distributions, so clear importer
            # cache.
            sys.path_importer_cache.clear()

            tmp = self._download_cache
            if tmp is None:
                tmp = tempfile.mkdtemp('get_dist')

            try:
                dist = self._fetch(avail, tmp, self._download_cache)

                if dist is None:
                    raise zc.buildout.UserError(
                        "Couldn't download distribution %s." % avail)

                if dist.precedence == pkg_resources.EGG_DIST:
                    # It's already an egg, just fetch it into the dest

                    newloc = os.path.join(
                        self._dest, os.path.basename(dist.location))

                    if os.path.isdir(dist.location):
                        # we got a directory. It must have been
                        # obtained locally.  Just copy it.
                        shutil.copytree(dist.location, newloc)
                    else:

                        if self._always_unzip:
                            should_unzip = True
                        else:
                            metadata = pkg_resources.EggMetadata(
                                zipimport.zipimporter(dist.location)
                                )
                            should_unzip = (
                                metadata.has_metadata('not-zip-safe')
                                or
                                not metadata.has_metadata('zip-safe')
                                )

                        if should_unzip:
                            setuptools.archive_util.unpack_archive(
                                dist.location, newloc)
                        else:
                            shutil.copyfile(dist.location, newloc)

                    redo_pyc(newloc)

                    # Getting the dist from the environment causes the
                    # distribution meta data to be read.  Cloning isn't
                    # good enough.
                    dists = pkg_resources.Environment(
                        [newloc],
                        python=_get_version(self._executable),
                        )[dist.project_name]
                else:
                    # It's some other kind of dist.  We'll let easy_install
                    # deal with it:
                    dists = self._call_easy_install(
                        dist.location, ws, self._dest, dist)
                    for dist in dists:
                        redo_pyc(dist.location)

            finally:
                if tmp != self._download_cache:
                    shutil.rmtree(tmp)

            self._env.scan([self._dest])
            dist = self._env.best_match(requirement, ws)
            logger.info("Got %s.", dist)

        else:
            dists = [dist]

        for dist in dists:
            if (dist.has_metadata('dependency_links.txt')
                and not self._install_from_cache
                and self._use_dependency_links
                ):
                for link in dist.get_metadata_lines('dependency_links.txt'):
                    link = link.strip()
                    if link not in self._links:
                        logger.debug('Adding find link %r from %s', link, dist)
                        self._links.append(link)
                        self._index = _get_index(self._executable,
                                                 self._index_url, self._links,
                                                 self._allow_hosts, self._path)

        for dist in dists:
            # Check whether we picked a version and, if we did, report it:
            if not (
                dist.precedence == pkg_resources.DEVELOP_DIST
                or
                (len(requirement.specs) == 1
                 and
                 requirement.specs[0][0] == '==')
                ):
                logger.debug('Picked: %s = %s',
                             dist.project_name, dist.version)
                if not self._allow_picked_versions:
                    raise zc.buildout.UserError(
                        'Picked: %s = %s' % (dist.project_name, dist.version)
                        )

        return dists

    def _maybe_add_setuptools(self, ws, dist):
        if dist.has_metadata('namespace_packages.txt'):
            for r in dist.requires():
                if r.project_name == 'setuptools':
                    break
            else:
                # We have a namespace package but no requirement for setuptools
                if dist.precedence == pkg_resources.DEVELOP_DIST:
                    logger.warn(
                        "Develop distribution: %s\n"
                        "uses namespace packages but the distribution "
                        "does not require setuptools.",
                        dist)
                requirement = self._constrain(
                    pkg_resources.Requirement.parse('setuptools')
                    )
                if ws.find(requirement) is None:
                    for dist in self._get_dist(requirement, ws, False):
                        ws.add(dist)


    def _constrain(self, requirement):
        version = self._versions.get(requirement.project_name)
        if version:
            if version not in requirement:
                logger.error("The version, %s, is not consistent with the "
                             "requirement, %r.", version, str(requirement))
                raise IncompatibleVersionError("Bad version", version)

            requirement = pkg_resources.Requirement.parse(
                "%s[%s] ==%s" % (requirement.project_name,
                               ','.join(requirement.extras),
                               version))

        return requirement

    def install(self, specs, working_set=None):

        logger.debug('Installing %s.', repr(specs)[1:-1])

        path = self._path
        destination = self._dest
        if destination is not None and destination not in path:
            path.insert(0, destination)

        requirements = [self._constrain(pkg_resources.Requirement.parse(spec))
                        for spec in specs]



        if working_set is None:
            ws = pkg_resources.WorkingSet([])
        else:
            ws = working_set

        for requirement in requirements:
            for dist in self._get_dist(requirement, ws, self._always_unzip):
                ws.add(dist)
                self._maybe_add_setuptools(ws, dist)

        # OK, we have the requested distributions and they're in the working
        # set, but they may have unmet requirements.  We'll resolve these
        # requirements. This is code modified from
        # pkg_resources.WorkingSet.resolve.  We can't reuse that code directly
        # because we have to constrain our requirements (see
        # versions_section_ignored_for_dependency_in_favor_of_site_packages in
        # zc.buildout.tests).
        requirements.reverse() # Set up the stack.
        processed = {}  # This is a set of processed requirements.
        best = {}  # This is a mapping of key -> dist.
        # Note that we don't use the existing environment, because we want
        # to look for new eggs unless what we have is the best that
        # matches the requirement.
        env = pkg_resources.Environment(ws.entries)
        while requirements:
            # Process dependencies breadth-first.
            req = self._constrain(requirements.pop(0))
            if req in processed:
                # Ignore cyclic or redundant dependencies.
                continue
            dist = best.get(req.key)
            if dist is None:
                # Find the best distribution and add it to the map.
                dist = ws.by_key.get(req.key)
                if dist is None:
                    try:
                        dist = best[req.key] = env.best_match(req, ws)
                    except pkg_resources.VersionConflict, err:
                        raise VersionConflict(err, ws)
                    if dist is None:
                        if destination:
                            logger.debug('Getting required %r', str(req))
                        else:
                            logger.debug('Adding required %r', str(req))
                        _log_requirement(ws, req)
                        for dist in self._get_dist(req,
                                                   ws, self._always_unzip):
                            ws.add(dist)
                            self._maybe_add_setuptools(ws, dist)
            if dist not in req:
                # Oops, the "best" so far conflicts with a dependency.
                raise VersionConflict(
                    pkg_resources.VersionConflict(dist, req), ws)
            requirements.extend(dist.requires(req.extras)[::-1])
            processed[req] = True
            if dist.location in self._site_packages:
                logger.debug('Egg from site-packages: %s', dist)
        return ws

    def build(self, spec, build_ext):

        requirement = self._constrain(pkg_resources.Requirement.parse(spec))

        dist, avail = self._satisfied(requirement, 1)
        if dist is not None:
            return [dist.location]

        # Retrieve the dist:
        if avail is None:
            raise zc.buildout.UserError(
                "Couldn't find a source distribution for %r."
                % str(requirement))

        logger.debug('Building %r', spec)

        tmp = self._download_cache
        if tmp is None:
            tmp = tempfile.mkdtemp('get_dist')

        try:
            dist = self._fetch(avail, tmp, self._download_cache)

            build_tmp = tempfile.mkdtemp('build')
            try:
                setuptools.archive_util.unpack_archive(dist.location,
                                                       build_tmp)
                if os.path.exists(os.path.join(build_tmp, 'setup.py')):
                    base = build_tmp
                else:
                    setups = glob.glob(
                        os.path.join(build_tmp, '*', 'setup.py'))
                    if not setups:
                        raise distutils.errors.DistutilsError(
                            "Couldn't find a setup script in %s"
                            % os.path.basename(dist.location)
                            )
                    if len(setups) > 1:
                        raise distutils.errors.DistutilsError(
                            "Multiple setup scripts in %s"
                            % os.path.basename(dist.location)
                            )
                    base = os.path.dirname(setups[0])

                setup_cfg = os.path.join(base, 'setup.cfg')
                if not os.path.exists(setup_cfg):
                    f = open(setup_cfg, 'w')
                    f.close()
                setuptools.command.setopt.edit_config(
                    setup_cfg, dict(build_ext=build_ext))

                dists = self._call_easy_install(
                    base, pkg_resources.WorkingSet(),
                    self._dest, dist)

                for dist in dists:
                    redo_pyc(dist.location)

                return [dist.location for dist in dists]
            finally:
                shutil.rmtree(build_tmp)

        finally:
            if tmp != self._download_cache:
                shutil.rmtree(tmp)

def default_versions(versions=None):
    old = Installer._versions
    if versions is not None:
        Installer._versions = versions
    return old

def download_cache(path=-1):
    old = Installer._download_cache
    if path != -1:
        if path:
            path = realpath(path)
        Installer._download_cache = path
    return old

def install_from_cache(setting=None):
    old = Installer._install_from_cache
    if setting is not None:
        Installer._install_from_cache = bool(setting)
    return old

def prefer_final(setting=None):
    old = Installer._prefer_final
    if setting is not None:
        Installer._prefer_final = bool(setting)
    return old

def include_site_packages(setting=None):
    old = Installer._include_site_packages
    if setting is not None:
        Installer._include_site_packages = bool(setting)
    return old

def allowed_eggs_from_site_packages(setting=None):
    old = Installer._allowed_eggs_from_site_packages
    if setting is not None:
        Installer._allowed_eggs_from_site_packages = tuple(setting)
    return old

def use_dependency_links(setting=None):
    old = Installer._use_dependency_links
    if setting is not None:
        Installer._use_dependency_links = bool(setting)
    return old

def allow_picked_versions(setting=None):
    old = Installer._allow_picked_versions
    if setting is not None:
        Installer._allow_picked_versions = bool(setting)
    return old

def always_unzip(setting=None):
    old = Installer._always_unzip
    if setting is not None:
        Installer._always_unzip = bool(setting)
    return old

def install(specs, dest,
            links=(), index=None,
            executable=sys.executable, always_unzip=None,
            path=None, working_set=None, newest=True, versions=None,
            use_dependency_links=None, include_site_packages=None,
            allowed_eggs_from_site_packages=None, allow_hosts=('*',)):
    installer = Installer(dest, links, index, executable, always_unzip, path,
                          newest, versions, use_dependency_links,
                          include_site_packages=include_site_packages,
                          allowed_eggs_from_site_packages=
                            allowed_eggs_from_site_packages,
                          allow_hosts=allow_hosts)
    return installer.install(specs, working_set)


def build(spec, dest, build_ext,
          links=(), index=None,
          executable=sys.executable,
          path=None, newest=True, versions=None, include_site_packages=None,
          allowed_eggs_from_site_packages=None, allow_hosts=('*',)):
    installer = Installer(dest, links, index, executable, True, path, newest,
                          versions,
                          include_site_packages=include_site_packages,
                          allowed_eggs_from_site_packages=
                            allowed_eggs_from_site_packages,
                          allow_hosts=allow_hosts)
    return installer.build(spec, build_ext)



def _rm(*paths):
    for path in paths:
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.exists(path):
            os.remove(path)

def _copyeggs(src, dest, suffix, undo):
    result = []
    undo.append(lambda : _rm(*result))
    for name in os.listdir(src):
        if name.endswith(suffix):
            new = os.path.join(dest, name)
            _rm(new)
            os.rename(os.path.join(src, name), new)
            result.append(new)

    assert len(result) == 1, str(result)
    undo.pop()

    return result[0]

def develop(setup, dest,
            build_ext=None,
            executable=sys.executable):

    if os.path.isdir(setup):
        directory = setup
        setup = os.path.join(directory, 'setup.py')
    else:
        directory = os.path.dirname(setup)

    undo = []
    try:
        if build_ext:
            setup_cfg = os.path.join(directory, 'setup.cfg')
            if os.path.exists(setup_cfg):
                os.rename(setup_cfg, setup_cfg+'-develop-aside')
                def restore_old_setup():
                    if os.path.exists(setup_cfg):
                        os.remove(setup_cfg)
                    os.rename(setup_cfg+'-develop-aside', setup_cfg)
                undo.append(restore_old_setup)
            else:
                open(setup_cfg, 'w')
                undo.append(lambda: os.remove(setup_cfg))
            setuptools.command.setopt.edit_config(
                setup_cfg, dict(build_ext=build_ext))

        fd, tsetup = tempfile.mkstemp()
        undo.append(lambda: os.remove(tsetup))
        undo.append(lambda: os.close(fd))

        os.write(fd, runsetup_template % dict(
            setuptools=setuptools_loc,
            setupdir=directory,
            setup=setup,
            __file__ = setup,
            ))

        tmp3 = tempfile.mkdtemp('build', dir=dest)
        undo.append(lambda : shutil.rmtree(tmp3))

        args = [
            zc.buildout.easy_install._safe_arg(tsetup),
            '-q', 'develop', '-mxN',
            '-d', _safe_arg(tmp3),
            ]

        log_level = logger.getEffectiveLevel()
        if log_level <= 0:
            if log_level == 0:
                del args[1]
            else:
                args[1] == '-v'
        if log_level < logging.DEBUG:
            logger.debug("in: %r\n%s", directory, ' '.join(args))

        if is_jython:
            assert subprocess.Popen([_safe_arg(executable)] + args).wait() == 0
        else:
            assert os.spawnl(os.P_WAIT, executable, _safe_arg(executable),
                             *args) == 0

        return _copyeggs(tmp3, dest, '.egg-link', undo)

    finally:
        undo.reverse()
        [f() for f in undo]

def working_set(specs, executable, path, include_site_packages=None,
                allowed_eggs_from_site_packages=None):
    return install(
        specs, None, executable=executable, path=path,
        include_site_packages=include_site_packages,
        allowed_eggs_from_site_packages=allowed_eggs_from_site_packages)

############################################################################
# Script generation functions

def scripts(reqs, working_set, executable, dest,
            scripts=None,
            extra_paths=(),
            arguments='',
            interpreter=None,
            initialization='',
            relative_paths=False,
            ):
    """Generate scripts and/or an interpreter.

    See sitepackage_safe_scripts for a version that can be used with a Python
    that can be used with a Python that has code installed in site-packages.
    It has more options and a different approach.
    """
    path = _get_path(working_set, extra_paths)
    if initialization:
        initialization = '\n'+initialization+'\n'
    generated = _generate_scripts(
        reqs, working_set, dest, path, scripts, relative_paths,
        initialization, executable, arguments)
    if interpreter:
        sname = os.path.join(dest, interpreter)
        spath, rpsetup = _relative_path_and_setup(sname, path, relative_paths)
        generated.extend(
            _pyscript(spath, sname, executable, rpsetup))
    return generated

def sitepackage_safe_scripts(
    dest, working_set, executable, site_py_dest,
    reqs=(), scripts=None, interpreter=None, extra_paths=(),
    initialization='', include_site_packages=False, exec_sitecustomize=False,
    relative_paths=False, script_arguments='', script_initialization=''):
    """Generate scripts and/or an interpreter from a system Python.

    This accomplishes the same job as the ``scripts`` function, above,
    but it does so in an alternative way that allows safely including
    Python site packages, if desired, and  choosing to execute the Python's
    sitecustomize.
    """
    generated = []
    generated.append(_generate_sitecustomize(
        site_py_dest, executable, initialization, exec_sitecustomize))
    generated.append(_generate_site(
        site_py_dest, working_set, executable, extra_paths,
        include_site_packages, relative_paths))
    script_initialization = (
        '\nimport site # imports custom buildout-generated site.py\n%s' % (
            script_initialization,))
    if not script_initialization.endswith('\n'):
        script_initialization += '\n'
    generated.extend(_generate_scripts(
        reqs, working_set, dest, [site_py_dest], scripts, relative_paths,
        script_initialization, executable, script_arguments, block_site=True))
    if interpreter:
        generated.extend(_generate_interpreter(
            interpreter, dest, executable, site_py_dest, relative_paths))
    return generated

# Utilities for the script generation functions.

# These are shared by both ``scripts`` and ``sitepackage_safe_scripts``

def _get_path(working_set, extra_paths=()):
    """Given working set and extra paths, return a normalized path list."""
    path = [dist.location for dist in working_set]
    path.extend(extra_paths)
    return map(realpath, path)

def _generate_scripts(reqs, working_set, dest, path, scripts, relative_paths,
                      initialization, executable, arguments,
                      block_site=False):
    """Generate scripts for the given requirements.

    - reqs is an iterable of string requirements or entry points.
    - The requirements must be findable in the given working_set.
    - The dest is the directory in which the scripts should be created.
    - The path is a list of paths that should be added to sys.path.
    - The scripts is an optional dictionary.  If included, the keys should be
      the names of the scripts that should be created, as identified in their
      entry points; and the values should be the name the script should
      actually be created with.
    - relative_paths, if given, should be the path that is the root of the
      buildout (the common path that should be the root of what is relative).
    """
    if isinstance(reqs, str):
        raise TypeError('Expected iterable of requirements or entry points,'
                        ' got string.')
    generated = []
    entry_points = []
    for req in reqs:
        if isinstance(req, str):
            req = pkg_resources.Requirement.parse(req)
            dist = working_set.find(req)
            for name in pkg_resources.get_entry_map(dist, 'console_scripts'):
                entry_point = dist.get_entry_info('console_scripts', name)
                entry_points.append(
                    (name, entry_point.module_name,
                     '.'.join(entry_point.attrs))
                    )
        else:
            entry_points.append(req)
    for name, module_name, attrs in entry_points:
        if scripts is not None:
            sname = scripts.get(name)
            if sname is None:
                continue
        else:
            sname = name
        sname = os.path.join(dest, sname)
        spath, rpsetup = _relative_path_and_setup(sname, path, relative_paths)
        generated.extend(
            _script(sname, executable, rpsetup, spath, initialization,
                    module_name, attrs, arguments, block_site=block_site))
    return generated

def _relative_path_and_setup(sname, path,
                             relative_paths=False, indent_level=1,
                             omit_os_import=False):
    """Return a string of code of paths and of setup if appropriate.

    - sname is the full path to the script name to be created.
    - path is the list of paths to be added to sys.path.
    - relative_paths, if given, should be the path that is the root of the
      buildout (the common path that should be the root of what is relative).
    - indent_level is the number of four-space indents that the path should
      insert before each element of the path.
    """
    if relative_paths:
        relative_paths = os.path.normcase(relative_paths)
        sname = os.path.normcase(os.path.abspath(sname))
        spath = _format_paths(
            [_relativitize(os.path.normcase(path_item), sname, relative_paths)
             for path_item in path], indent_level=indent_level)
        rpsetup = relative_paths_setup
        if not omit_os_import:
            rpsetup = '\n\nimport os\n' + rpsetup
        for i in range(_relative_depth(relative_paths, sname)):
            rpsetup += "\nbase = os.path.dirname(base)"
    else:
        spath = _format_paths((repr(p) for p in path),
                              indent_level=indent_level)
        rpsetup = ''
    return spath, rpsetup

def _relative_depth(common, path):
    """Return number of dirs separating ``path`` from ancestor, ``common``.

    For instance, if path is /foo/bar/baz/bing, and common is /foo, this will
    return 2--in UNIX, the number of ".." to get from bing's directory
    to foo.

    This is a helper for _relative_path_and_setup.
    """
    n = 0
    while 1:
        dirname = os.path.dirname(path)
        if dirname == path:
            raise AssertionError("dirname of %s is the same" % dirname)
        if dirname == common:
            break
        n += 1
        path = dirname
    return n

def _relative_path(common, path):
    """Return the relative path from ``common`` to ``path``.

    This is a helper for _relativitize, which is a helper to
    _relative_path_and_setup.
    """
    r = []
    while 1:
        dirname, basename = os.path.split(path)
        r.append(basename)
        if dirname == common:
            break
        if dirname == path:
            raise AssertionError("dirname of %s is the same" % dirname)
        path = dirname
    r.reverse()
    return os.path.join(*r)

def _relativitize(path, script, relative_paths):
    """Return a code string for the given path.

    Path is relative to the base path ``relative_paths``if the common prefix
    between ``path`` and ``script`` starts with ``relative_paths``.
    """
    if path == script:
        raise AssertionError("path == script")
    common = os.path.dirname(os.path.commonprefix([path, script]))
    if (common == relative_paths or
        common.startswith(os.path.join(relative_paths, ''))
        ):
        return "join(base, %r)" % _relative_path(common, path)
    else:
        return repr(path)

relative_paths_setup = """
join = os.path.join
base = os.path.dirname(os.path.abspath(os.path.realpath(__file__)))"""

def _write_script(full_name, contents, logged_type):
    """Write contents of script in full_name, logging the action.

    The only tricky bit in this function is that it supports Windows by
    creating exe files using a pkg_resources helper.
    """
    generated = []
    script_name = full_name
    if is_win32:
        script_name += '-script.py'
        # Generate exe file and give the script a magic name.
        exe = full_name + '.exe'
        new_data = pkg_resources.resource_string('setuptools', 'cli.exe')
        if not os.path.exists(exe) or (open(exe, 'rb').read() != new_data):
            # Only write it if it's different.
            open(exe, 'wb').write(new_data)
        generated.append(exe)
    changed = not (os.path.exists(script_name) and
                   open(script_name).read() == contents)
    if changed:
        open(script_name, 'w').write(contents)
        try:
            os.chmod(script_name, 0755)
        except (AttributeError, os.error):
            pass
        logger.info("Generated %s %r.", logged_type, full_name)
    generated.append(script_name)
    return generated

def _format_paths(paths, indent_level=1):
    """Format paths for inclusion in a script."""
    separator = ',\n' + indent_level * '    '
    return separator.join(paths)

def _script(dest, executable, relative_paths_setup, path, initialization,
            module_name, attrs, arguments, block_site=False):
    if block_site:
        dash_S = ' -S'
    else:
        dash_S = ''
    contents = script_template % dict(
        python=_safe_arg(executable),
        dash_S=dash_S,
        path=path,
        module_name=module_name,
        attrs=attrs,
        arguments=arguments,
        initialization=initialization,
        relative_paths_setup=relative_paths_setup,
        )
    return _write_script(dest, contents, 'script')

if is_jython and jython_os_name == 'linux':
    script_header = '#!/usr/bin/env %(python)s%(dash_S)s'
else:
    script_header = '#!%(python)s%(dash_S)s'

sys_path_template = '''\
import sys
sys.path[0:0] = [
    %s,
    ]
'''

script_template = script_header + '''\
%(relative_paths_setup)s

import sys
sys.path[0:0] = [
    %(path)s,
    ]

%(initialization)s
import %(module_name)s

if __name__ == '__main__':
    %(module_name)s.%(attrs)s(%(arguments)s)
'''

# These are used only by the older ``scripts`` function.

def _pyscript(path, dest, executable, rsetup):
    contents = py_script_template % dict(
        python=_safe_arg(executable),
        dash_S='',
        path=path,
        relative_paths_setup=rsetup,
        )
    return _write_script(dest, contents, 'interpreter')

py_script_template = script_header + '''\
%(relative_paths_setup)s

import sys

sys.path[0:0] = [
    %(path)s,
    ]

_interactive = True
if len(sys.argv) > 1:
    _options, _args = __import__("getopt").getopt(sys.argv[1:], 'ic:m:')
    _interactive = False
    for (_opt, _val) in _options:
        if _opt == '-i':
            _interactive = True
        elif _opt == '-c':
            exec _val
        elif _opt == '-m':
            sys.argv[1:] = _args
            _args = []
            __import__("runpy").run_module(
                 _val, {}, "__main__", alter_sys=True)

    if _args:
        sys.argv[:] = _args
        __file__ = _args[0]
        del _options, _args
        execfile(__file__)

if _interactive:
    del _interactive
    __import__("code").interact(banner="", local=globals())
'''

# These are used only by the newer ``sitepackage_safe_scripts`` function.

def _get_module_file(executable, name):
    """Return a module's file path.

    - executable is a path to the desired Python executable.
    - name is the name of the (pure, not C) Python module.
    """
    cmd = [executable, "-c",
           "import imp; "
           "fp, path, desc = imp.find_module(%r); "
           "fp.close; "
           "print path" % (name,)]
    _proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = _proc.communicate();
    if _proc.returncode:
        logger.info(
            'Could not find file for module %s:\n%s', name, stderr)
        return None
    # else: ...
    res = stdout.strip()
    if res.endswith('.pyc') or res.endswith('.pyo'):
        raise RuntimeError('Cannot find uncompiled version of %s' % (name,))
    if not os.path.exists(res):
        raise RuntimeError(
            'File does not exist for module %s:\n%s' % (name, res))
    return res

def _generate_sitecustomize(dest, executable, initialization='',
                            exec_sitecustomize=False):
    """Write a sitecustomize file with optional custom initialization.

    The created script will execute the underlying Python's
    sitecustomize if exec_sitecustomize is True.
    """
    sitecustomize_path = os.path.join(dest, 'sitecustomize.py')
    sitecustomize = open(sitecustomize_path, 'w')
    if initialization:
        sitecustomize.write(initialization + '\n')
    if exec_sitecustomize:
        real_sitecustomize_path = _get_module_file(
            executable, 'sitecustomize')
        if real_sitecustomize_path:
            real_sitecustomize = open(real_sitecustomize_path, 'r')
            sitecustomize.write(
                '\n# The following is from\n# %s\n' %
                (real_sitecustomize_path,))
            sitecustomize.write(real_sitecustomize.read())
            real_sitecustomize.close()
    sitecustomize.close()
    return sitecustomize_path

def _generate_site(dest, working_set, executable, extra_paths=(),
                   include_site_packages=False, relative_paths=False):
    """Write a site.py file with eggs from working_set.

    extra_paths will be added to the path.  If include_site_packages is True,
    paths from the underlying Python will be added.
    """
    path = _get_path(working_set, extra_paths)
    site_path = os.path.join(dest, 'site.py')
    egg_path_string, preamble = _relative_path_and_setup(
        site_path, path, relative_paths, indent_level=2, omit_os_import=True)
    if preamble:
        preamble = '\n'.join(
            [(line and '    %s' % (line,) or line)
             for line in preamble.split('\n')])
    original_path_setup = ''
    if include_site_packages:
        stdlib, site_paths = _get_system_paths(executable)
        original_path_setup = original_path_snippet % (
            _format_paths((repr(p) for p in site_paths), 2),)
        distribution = working_set.find(
            pkg_resources.Requirement.parse('setuptools'))
        if distribution is not None:
            # We need to worry about namespace packages.
            if relative_paths:
                location = _relativitize(
                    distribution.location,
                    os.path.normcase(os.path.abspath(site_path)),
                    relative_paths)
            else:
                location = repr(distribution.location)
            preamble += namespace_include_site_packages_setup % (location,)
            original_path_setup = (
                addsitedir_namespace_originalpackages_snippet +
                original_path_setup)
    addsitepackages_marker = 'def addsitepackages('
    enableusersite_marker = 'ENABLE_USER_SITE = '
    successful_rewrite = False
    real_site_path = _get_module_file(executable, 'site')
    real_site = open(real_site_path, 'r')
    site = open(site_path, 'w')
    try:
        for line in real_site.readlines():
            if line.startswith(enableusersite_marker):
                site.write(enableusersite_marker)
                site.write('False # buildout does not support user sites.\n')
            elif line.startswith(addsitepackages_marker):
                site.write(addsitepackages_script % (
                    preamble, egg_path_string, original_path_setup))
                site.write(line[len(addsitepackages_marker):])
                successful_rewrite = True
            else:
                site.write(line)
    finally:
        site.close()
        real_site.close()
    if not successful_rewrite:
        raise RuntimeError('Buildout did not successfully rewrite site.py')
    return site_path

namespace_include_site_packages_setup = '''
    setuptools_path = %s
    sys.path.append(setuptools_path)
    known_paths.add(os.path.normcase(setuptools_path))
    import pkg_resources'''

addsitedir_namespace_originalpackages_snippet = '''
            pkg_resources.working_set.add_entry(sitedir)'''

original_path_snippet = '''
    original_paths = [
        %s
        ]
    for path in original_paths:
        addsitedir(path, known_paths)'''

addsitepackages_script = '''\
def addsitepackages(known_paths):
    """Add site packages, as determined by zc.buildout.

    See original_addsitepackages, below, for the original version."""%s
    buildout_paths = [
        %s
        ]
    for path in buildout_paths:
        sitedir, sitedircase = makepath(path)
        if not sitedircase in known_paths and os.path.exists(sitedir):
            sys.path.append(sitedir)
            known_paths.add(sitedircase)%s
    return known_paths

def original_addsitepackages('''

def _generate_interpreter(name, dest, executable, site_py_dest,
                          relative_paths=False):
    """Write an interpreter script, using the site.py approach."""
    full_name = os.path.join(dest, name)
    site_py_dest_string, rpsetup = _relative_path_and_setup(
        full_name, [site_py_dest], relative_paths, omit_os_import=True)
    if rpsetup:
        rpsetup += "\n"
    if sys.platform == 'win32':
        windows_import = '\nimport subprocess'
        # os.exec* is a mess on Windows, particularly if the path
        # to the executable has spaces and the Python is using MSVCRT.
        # The standard fix is to surround the executable's path with quotes,
        # but that has been unreliable in testing.
        #
        # Here's a demonstration of the problem.  Given a Python
        # compiled with a MSVCRT-based compiler, such as the free Visual
        # C++ 2008 Express Edition, and an executable path with spaces
        # in it such as the below, we see the following.
        #
        # >>> import os
        # >>> p0 = 'C:\\Documents and Settings\\Administrator\\My Documents\\Downloads\\Python-2.6.4\\PCbuild\\python.exe'
        # >>> os.path.exists(p0)
        # True
        # >>> os.execv(p0, [])
        # Traceback (most recent call last):
        #  File "<stdin>", line 1, in <module>
        # OSError: [Errno 22] Invalid argument
        #
        # That seems like a standard problem.  The standard solution is
        # to quote the path (see, for instance
        # http://bugs.python.org/issue436259).  However, this solution,
        # and other variations, fail:
        #
        # >>> p1 = '"C:\\Documents and Settings\\Administrator\\My Documents\\Downloads\\Python-2.6.4\\PCbuild\\python.exe"'
        # >>> os.execv(p1, [])
        # Traceback (most recent call last):
        #   File "<stdin>", line 1, in <module>
        # OSError: [Errno 22] Invalid argument
        #
        # We simply use subprocess instead, since it handles everything
        # nicely, and the transparency of exec* (that is, not running,
        # perhaps unexpectedly, in a subprocess) is arguably not a
        # necessity, at least for many use cases.
        execute = 'subprocess.call(argv, env=environ)'
    else:
        windows_import = ''
        execute = 'os.execve(sys.executable, argv, environ)'
    contents = interpreter_template % dict(
        python=_safe_arg(executable),
        dash_S=' -S',
        site_dest=site_py_dest_string,
        relative_paths_setup=rpsetup,
        windows_import=windows_import,
        execute=execute,
        )
    return _write_script(full_name, contents, 'interpreter')

interpreter_template = script_header + '''
import os
import sys%(windows_import)s
%(relative_paths_setup)s
argv = [sys.executable] + sys.argv[1:]
environ = os.environ.copy()
path = %(site_dest)s
if environ.get('PYTHONPATH'):
    path = os.pathsep.join([path, environ['PYTHONPATH']])
environ['PYTHONPATH'] = path
%(execute)s
'''

# End of script generation code.
############################################################################

runsetup_template = """
import sys
sys.path.insert(0, %(setupdir)r)
sys.path.insert(0, %(setuptools)r)
import os, setuptools

__file__ = %(__file__)r

os.chdir(%(setupdir)r)
sys.argv[0] = %(setup)r
execfile(%(setup)r)
"""


class VersionConflict(zc.buildout.UserError):

    def __init__(self, err, ws):
        ws = list(ws)
        ws.sort()
        self.err, self.ws = err, ws

    def __str__(self):
        existing_dist, req = self.err
        result = ["There is a version conflict.",
                  "We already have: %s" % existing_dist,
                  ]
        for dist in self.ws:
            if req in dist.requires():
                result.append("but %s requires %r." % (dist, str(req)))
        return '\n'.join(result)


class MissingDistribution(zc.buildout.UserError):

    def __init__(self, req, ws):
        ws = list(ws)
        ws.sort()
        self.data = req, ws

    def __str__(self):
        req, ws = self.data
        return "Couldn't find a distribution for %r." % str(req)

def _log_requirement(ws, req):
    ws = list(ws)
    ws.sort()
    for dist in ws:
        if req in dist.requires():
            logger.debug("  required by %s." % dist)

def _fix_file_links(links):
    for link in links:
        if link.startswith('file://') and link[-1] != '/':
            if os.path.isdir(link[7:]):
                # work around excessive restriction in setuptools:
                link += '/'
        yield link

_final_parts = '*final-', '*final'
def _final_version(parsed_version):
    for part in parsed_version:
        if (part[:1] == '*') and (part not in _final_parts):
            return False
    return True

def redo_pyc(egg):
    if not os.path.isdir(egg):
        return
    for dirpath, dirnames, filenames in os.walk(egg):
        for filename in filenames:
            if not filename.endswith('.py'):
                continue
            filepath = os.path.join(dirpath, filename)
            if not (os.path.exists(filepath+'c')
                    or os.path.exists(filepath+'o')):
                # If it wasn't compiled, it may not be compilable
                continue

            # OK, it looks like we should try to compile.

            # Remove old files.
            for suffix in 'co':
                if os.path.exists(filepath+suffix):
                    os.remove(filepath+suffix)

            # Compile under current optimization
            try:
                py_compile.compile(filepath)
            except py_compile.PyCompileError:
                logger.warning("Couldn't compile %s", filepath)
            else:
                # Recompile under other optimization. :)
                args = [_safe_arg(sys.executable)]
                if __debug__:
                    args.append('-O')
                args.extend(['-m', 'py_compile', _safe_arg(filepath)])

                if is_jython:
                    subprocess.call([sys.executable, args])
                else:
                    os.spawnv(os.P_WAIT, sys.executable, args)

