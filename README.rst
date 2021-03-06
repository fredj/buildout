********
Buildout
********

*Note*: the 1.4.4 release is a release for people who encounter trouble
with the 1.5 line.  By switching to `the associated bootstrap script
<https://raw.github.com/buildout/buildout/master/bootstrap/bootstrap.py>`_
you can stay on 1.4.4 until you are ready to migrate.

.. contents::

The Buildout project provides support for creating applications,
especially Python applications.  It provides tools for assembling
applications from multiple parts, Python or otherwise.  An application
may actually contain multiple programs, processes, and configuration
settings.

The word "buildout" refers to a description of a set of parts and the
software to create and assemble them.  It is often used informally to
refer to an installed system based on a buildout definition.  For
example, if we are creating an application named "Foo", then "the Foo
buildout" is the collection of configuration and application-specific
software that allows an instance of the application to be created.  We
may refer to such an instance of the application informally as "a Foo
buildout".

To get a feel for some of the things you might use buildouts for, see
the `Buildout examples`_.

To lean more about using buildouts, see `Detailed Documentation`_.

To see screencasts, talks, useful links and more documentation, visit
the `Buildout website <http://www.buildout.org>`_.

Recipes
*******

Existing recipes include:

`zc.recipe.egg <http://pypi.python.org/pypi/zc.recipe.egg>`_
   The egg recipe installes one or more eggs, with their
   dependencies.  It installs their console-script entry points with
   the needed eggs included in their paths.

For an extensive list of recipes, look at the `Framework Buildout category
<http://pypi.python.org/pypi?:action=browse&c=512>`_ on the Python Package
Index.

Buildout examples
*****************

Here are a few examples of what you can do with buildouts.  We'll
present these as a set of use cases.

Try out an egg
==============

Sometimes you want to try an egg (or eggs) that someone has released.
You'd like to get a Python interpreter that lets you try things
interactively or run sample scripts without having to do path
manipulations.  If you can and don't mind modifying your Python
installation, you could use easy_install, otherwise, you could create
a directory somewhere and create a buildout.cfg file in that directory
containing::

  [buildout]
  parts = mypython

  [mypython]
  recipe = zc.recipe.egg
  interpreter = mypython
  eggs = theegg

where theegg is the name of the egg you want to try out.

Run buildout in this directory.  It will create a bin subdirectory
that includes a mypython script.  If you run mypython without any
arguments you'll get an interactive interpreter with the egg in the
path. If you run it with a script and script arguments, the script
will run with the egg in its path.  Of course, you can specify as many
eggs as you want in the eggs option.

If the egg provides any scripts (console_scripts entry points), those
will be installed in your bin directory too.

Work on a package
=================

I often work on packages that are managed separately.  They don't have
scripts to be installed, but I want to be able to run their tests
using the `zope.testing test runner
<http://www.python.org/pypi/zope.testing>`_.  In this kind of
application, the program to be installed is the test runner.  A good
example of this is `zc.ngi <http://svn.zope.org/zc.ngi/trunk/>`_.

Here I have a subversion project for the zc.ngi package.  The software
is in the src directory.  The configuration file is very simple::

  [buildout]
  develop = .
  parts = test

  [test]
  recipe = zc.recipe.testrunner
  eggs = zc.ngi

I use the develop option to create a develop egg based on the current
directory.  I request a test script named "test" using the
zc.recipe.testrunner recipe.  In the section for the test script, I
specify that I want to run the tests in the zc.ngi package.

When I check out this project into a new sandbox, I run bootstrap.py
to get setuptools and buildout and to create bin/buildout.  I run
bin/buildout, which installs the test script, bin/test, which I can
then use to run the tests.

This is probably the most common type of buildout.

If I need to run a previous version of buildout, I use the
`--version` option of the bootstrap.py script::

    $ python bootstrap.py --version 1.4.2

The `buildout project <https://github.com/buildout/buildout>`_
is a slightly more complex example of this type of buildout.

Install egg-based scripts
=========================

A variation of the `Try out an egg`_ use case is to install scripts
into your ~/bin directory (on Unix, of course).  My ~/bin directory is
a buildout with a configuration file that looks like::


  [buildout]
  parts = foo bar
  bin-directory = .

  [foo]
  ...

where foo and bar are packages with scripts that I want available.  As
I need new scripts, I can add additional sections.  The bin-directory
option specified that scripts should be installed into the current
directory.

Multi-program multi-machine systems
===================================

Using an older prototype version of the buildout, we've build a number
of systems involving multiple programs, databases, and machines.  One
typical example consists of:

- Multiple Zope instances

- Multiple ZEO servers

- An LDAP server

- Cache-invalidation and Mail delivery servers

- Dozens of add-on packages

- Multiple test runners

- Multiple deployment modes, including dev, stage, and prod,
  with prod deployment over multiple servers

Parts installed include:

- Application software installs, including Zope, ZEO and LDAP
  software

- Add-on packages

- Bundles of configuration that define Zope, ZEO and LDAP instances

- Utility scripts such as test runners, server-control
  scripts, cron jobs.

Questions and Bug Reporting
***************************

Please send questions and comments to the
`distutils SIG mailing list <mailto://distutils-sig@python.org>`_.

Report bugs using the `buildout Issues Tracker
<https://github.com/buildout/buildout/issues>`_.

