#! /usr/bin/env python

# mklibs.py: An automated way to create a minimal /lib/ directory.
#
# Copyright 2001 by Falk Hueffner <falk@debian.org>
#                 & Goswin Brederlow <goswin.brederlow@student.uni-tuebingen.de>
#
# mklibs.sh by Marcus Brinkmann <Marcus.Brinkmann@ruhr-uni-bochum.de>
# used as template
#
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation; either version 2 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program; if not, write to the Free Software
#   Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

# HOW IT WORKS
#
# - Gather all unresolved symbols and libraries needed by the programs
#   and reduced libraries
# - Gather all symbols provided by the already reduced libraries
#   (none on the first pass)
# - If all symbols are provided we are done
# - go through all libraries and remember what symbols they provide
# - go through all unresolved/needed symbols and mark them as used
# - for each library:
#   - find pic file (if not present copy and strip the so)
#   - compile in only used symbols
#   - strip
# - back to the top

# TODO
# * complete argument parsing as given as comment in main

import string
import re
import sys
import os
import glob
import getopt
from stat import *

DEBUG_NORMAL  = 1
DEBUG_VERBOSE = 2
DEBUG_SPAM    = 3

debuglevel = DEBUG_NORMAL

def debug(level, *msg):
    if debuglevel >= level:
        print string.join(msg)

# return a list of lines of output of the command
def command(command, *args):
    debug(DEBUG_SPAM, "calling", command, string.join(args))
    pipe = os.popen(command + ' ' + ' '.join(args), 'r')
    output = pipe.read().strip()
    status = pipe.close() 
    if status is not None and os.WEXITSTATUS(status) != 0:
        print "Command failed with status", os.WEXITSTATUS(status),  ":", \
               command, string.join(args)
	print "With output:", output
        sys.exit(1)
    return [i for i in output.split('\n') if i]

# Filter a list according to a regexp containing a () group. Return
# a set.
def regexpfilter(list, regexp, groupnr = 1):
    pattern = re.compile(regexp)
    result = set()
    for x in list:
        match = pattern.match(x)
        if match:
            result.add(match.group(groupnr))

    return result

def elf_header(obj):
    if not os.access(obj, os.F_OK):
        raise "Cannot find lib: " + obj
    output = command("mklibs-readelf", "--print-elf-header", obj)
    s = [int(i) for i in output[0].split()]
    return {'class': s[0], 'data': s[1], 'machine': s[2], 'flags': s[3]}

# Return a set of rpath strings for the passed object
def rpath(obj):
    if not os.access(obj, os.F_OK):
        raise "Cannot find lib: " + obj
    output = command("mklibs-readelf", "--print-rpath", obj)
    return [root + "/" + x for x in output]

# Return a set of libraries the passed objects depend on.
def library_depends(obj):
    if not os.access(obj, os.F_OK):
        raise "Cannot find lib: " + obj
    return command("mklibs-readelf", "--print-needed", obj)

# Return a list of libraries the passed objects depend on. The
# libraries are in "-lfoo" format suitable for passing to gcc.
def library_depends_gcc_libnames(obj):
    if not os.access(obj, os.F_OK):
        raise "Cannot find lib: " + obj
    libs = library_depends(obj)
    ret = []
    for i in libs:
        match = re.match("^(((?P<ld>ld\S*)|(lib(?P<lib>\S+))))\.so.*$", i)
        if match:
            if match.group('ld'):
                ret.append(find_lib(match.group(0)))
            elif match.group('lib'):
                ret.append('-l%s' % match.group('lib'))
    return ' '.join(ret)

class Symbol(object):
    def __init__(self, name, version):
        self.name, self.version = name, version

    def __str__(self):
        return "%s@%s" % (self.name, self.version)

class UndefinedSymbol(Symbol):
    def __init__(self, name, weak, version):
        super(UndefinedSymbol, self).__init__(name, version)
        self.weak = weak

# Return undefined symbols in an object as a set of tuples (name, weakness)
def undefined_symbols(obj):
    if not os.access(obj, os.F_OK):
        raise "Cannot find lib" + obj

    result = []
    output = command("mklibs-readelf", "--print-symbols-undefined", obj)
    for line in output:
        name, weak_string, version = line.split()[:3]
        result.append(UndefinedSymbol(name, bool(eval(weak_string)), version))
    return result

class ProvidedSymbol(Symbol):
    def __init__(self, name, version, default_version):
        super(ProvidedSymbol, self).__init__(name, version)
        self.default_version = default_version

    def base_names(self):
        if self.default_version and self.version != "Base":
            return ["%s@%s" % (self.name, self.version), "%s@Base" % self.name]
        return ["%s@%s" % (self.name, self.version)]

    def linker_name(self):
        if self.default_version or self.version == "Base":
            return self.name
        return ["%s@%s" % (self.name, self.version)]

# Return a set of symbols provided by a library
def provided_symbols(obj):
    if not os.access(obj, os.F_OK):
        raise "Cannot find lib" + obj

    result = []
    output = command("mklibs-readelf", "--print-symbols-provided", obj)
    for line in output:
        name, weak, version, default_version_string = line.split()[:4]
        result.append(ProvidedSymbol(name, version, bool(eval(default_version_string))))
    return result
    
# Return real target of a symlink
def resolve_link(file):
    debug(DEBUG_SPAM, "resolving", file)
    while S_ISLNK(os.lstat(file)[ST_MODE]):
        new_file = os.readlink(file)
        if new_file[0] != "/":
            file = os.path.join(os.path.dirname(file), new_file)
        else:
            file = new_file
    debug(DEBUG_SPAM, "resolved to", file)
    return file

# Find complete path of a library, by searching in lib_path
def find_lib(lib):
    for path in lib_path:
        if os.access(path + "/" + lib, os.F_OK):
            return path + "/" + lib

    return ""

# Find a PIC archive for the library
def find_pic(lib):
    base_name = so_pattern.match(lib).group(1)
    for path in lib_path:
        for file in glob.glob(path + "/" + base_name + "_pic.a"):
            if os.access(file, os.F_OK):
                return resolve_link(file)
    return ""

# Find a PIC .map file for the library
def find_pic_map(lib):
    base_name = so_pattern.match(lib).group(1)
    for path in lib_path:
        for file in glob.glob(path + "/" + base_name + "_pic.map"):
            if os.access(file, os.F_OK):
                return resolve_link(file)
    return ""

def extract_soname(so_file):
    soname_data = command("mklibs-readelf", "--print-soname", so_file)
    if soname_data:
        return soname_data.pop()
    return ""

def usage(was_err):
    if was_err:
        outfd = sys.stderr
    else:
        outfd = sys.stdout
    print >> outfd, "Usage: mklibs [OPTION]... -d DEST FILE ..."
    print >> outfd, "Make a set of minimal libraries for FILE(s) in DEST."
    print >> outfd, "" 
    print >> outfd, "  -d, --dest-dir DIRECTORY     create libraries in DIRECTORY"
    print >> outfd, "  -D, --no-default-lib         omit default libpath (", ':'.join(default_lib_path), ")"
    print >> outfd, "  -L DIRECTORY[:DIRECTORY]...  add DIRECTORY(s) to the library search path"
    print >> outfd, "  -l LIBRARY                   add LIBRARY always"
    print >> outfd, "      --ldlib LDLIB            use LDLIB for the dynamic linker"
    print >> outfd, "      --libc-extras-dir DIRECTORY  look for libc extra files in DIRECTORY"
    print >> outfd, "      --target TARGET          prepend TARGET- to the gcc and binutils calls"
    print >> outfd, "      --root ROOT              search in ROOT for library rpaths"
    print >> outfd, "  -v, --verbose                explain what is being done"
    print >> outfd, "  -h, --help                   display this help and exit"
    sys.exit(was_err)

def version(vers):
    print "mklibs: version ",vers
    print ""

#################### main ####################
## Usage: ./mklibs.py [OPTION]... -d DEST FILE ...
## Make a set of minimal libraries for FILE ... in directory DEST.
## 
## Options:
##   -L DIRECTORY               Add DIRECTORY to library search path.
##   -D, --no-default-lib       Do not use default lib directories of /lib:/usr/lib
##   -n, --dry-run              Don't actually run any commands; just print them.
##   -v, --verbose              Print additional progress information.
##   -V, --version              Print the version number and exit.
##   -h, --help                 Print this help and exit.
##   --ldlib                    Name of dynamic linker (overwrites environment variable ldlib)
##   --libc-extras-dir          Directory for libc extra files
##   --target                   Use as prefix for gcc or binutils calls
## 
##   -d, --dest-dir DIRECTORY   Create libraries in DIRECTORY.
## 
## Required arguments for long options are also mandatory for the short options.

# Clean the environment
vers="0.12"
os.environ['LC_ALL'] = "C"

# Argument parsing
opts = "L:DnvVhd:r:l:"
longopts = ["no-default-lib", "dry-run", "verbose", "version", "help",
            "dest-dir=", "ldlib=", "libc-extras-dir=", "target=", "root="]

# some global variables
lib_rpath = []
lib_path = []
dest_path = "DEST"
ldlib = "LDLIB"
include_default_lib_path = "yes"
default_lib_path = ["/lib/", "/usr/lib/", "/usr/X11R6/lib/"]
libc_extras_dir = "/usr/lib/libc_pic"
target = ""
root = ""
force_libs = []
so_pattern = re.compile("((lib|ld).*)\.so(\..+)*")
script_pattern = re.compile("^#!\s*/")

try:
    optlist, proglist = getopt.getopt(sys.argv[1:], opts, longopts)
except getopt.GetoptError, msg:
    print >> sys.stderr, msg
    usage(1)

for opt, arg in optlist:
    if opt in ("-v", "--verbose"):
        if debuglevel < DEBUG_SPAM:
            debuglevel = debuglevel + 1
    elif opt == "-L":
        lib_path.extend(string.split(arg, ":"))
    elif opt in ("-d", "--dest-dir"):
        dest_path = arg
    elif opt in ("-D", "--no-default-lib"):
        include_default_lib_path = "no"
    elif opt == "--ldlib":
        ldlib = arg
    elif opt == "--libc-extras-dir":
        libc_extras_dir = arg
    elif opt == "--target":
        target = arg + "-"
    elif opt in ("-r", "--root"):
        root = arg
    elif opt in ("-l",):
        force_libs.append(arg)
    elif opt in ("--help", "-h"):
	usage(0)
        sys.exit(0)
    elif opt in ("--version", "-V"):
        version(vers)
        sys.exit(0)
    else:
        print "WARNING: unknown option: " + opt + "\targ: " + arg

if include_default_lib_path == "yes":
    lib_path.extend(default_lib_path)

if ldlib == "LDLIB":
    ldlib = os.getenv("ldlib")

objects = {}  # map from inode to filename
for prog in proglist:
    inode = os.stat(prog)[ST_INO]
    if objects.has_key(inode):
        debug(DEBUG_SPAM, prog, "is a hardlink to", objects[inode])
    elif so_pattern.match(prog):
        debug(DEBUG_SPAM, prog, "is a library")
    elif script_pattern.match(open(prog).read(256)):
        debug(DEBUG_SPAM, prog, "is a script")
    else:
        objects[inode] = prog

if not ldlib:
    for obj in objects.values():
        output = command("mklibs-readelf", "--print-interp", obj)
        if output:
            ldlib = output.pop()
	if ldlib:
	    break

if not ldlib:
    sys.exit("E: Dynamic linker not found, aborting.")

debug(DEBUG_NORMAL, "I: Using", ldlib, "as dynamic linker.")

# Check for rpaths
for obj in objects.values():
    rpath_val = rpath(obj)
    if rpath_val:
        if root:
            for rpath_elem in rpath_val:
                if not rpath_elem in lib_rpath:
                    if debuglevel >= DEBUG_VERBOSE:
                        print "Adding rpath " + rpath_elem + " for " + obj
                    lib_rpath.append(rpath_elem)
        else:
            print "warning: " + obj + " may need rpath, but --root not specified"

lib_path.extend(lib_rpath)

passnr = 1
available_libs = []
previous_pass_unresolved = set()
while 1:
    debug(DEBUG_NORMAL, "I: library reduction pass", `passnr`)
    if debuglevel >= DEBUG_VERBOSE:
        print "Objects:",
        for obj in objects.values():
            print obj[string.rfind(obj, '/') + 1:],
        print

    passnr = passnr + 1
    # Gather all already reduced libraries and treat them as objects as well
    small_libs = []
    for lib in regexpfilter(os.listdir(dest_path), "(.*-so-stripped)$"):
        obj = dest_path + "/" + lib
        small_libs.append(obj)
        inode = os.stat(obj)[ST_INO]
        if objects.has_key(inode):
            debug(DEBUG_SPAM, obj, "is hardlink to", objects[inode])
        else:
            objects[inode] = obj

    # DEBUG
    for obj in objects.values():
        small_libs.append(obj)
        debug(DEBUG_VERBOSE, "Object:", obj)

    # calculate what symbols and libraries are needed
    needed_symbols = {}
    libraries = set(force_libs)
    for obj in objects.values():
        for symbol in undefined_symbols(obj):
            debug(DEBUG_SPAM, "needed_symbols adding %s, weak: %s" % (symbol, symbol.weak))
            needed_symbols[str(symbol)] = symbol
        libraries.update(library_depends(obj))

    # calculate what symbols are present in small_libs and available_libs
    present_symbols = {}
    checked_libs = small_libs
    checked_libs.extend(available_libs)
    checked_libs.append(ldlib)
    for lib in checked_libs:
        for symbol in provided_symbols(lib):
            debug(DEBUG_SPAM, "present_symbols adding %s" % symbol)
            names = symbol.base_names()
            for name in names:
                present_symbols[name] = symbol

    # are we finished?
    num_unresolved = 0
    unresolved = set()
    for name in needed_symbols:
        if not name in present_symbols:
            debug(DEBUG_SPAM, "Still need: %s" % name)
            unresolved.add(name)
            num_unresolved = num_unresolved + 1

    debug (DEBUG_NORMAL, `len(needed_symbols)`, "symbols,",
           `num_unresolved`, "unresolved")

    if num_unresolved == 0:
        break

    if unresolved == previous_pass_unresolved:
        # No progress in last pass. Verify all remaining symbols are weak.
        for name in unresolved:
            if not needed_symbols[name].weak:
                raise "Unresolvable symbol %s" % name
        break

    previous_pass_unresolved = unresolved

    library_symbols = {}
    library_symbols_used = {}
    symbol_provider = {}

    # WORKAROUND: Always add libgcc on old-abi arm
    header = elf_header(find_lib(libraries.copy().pop()))
    if header['machine'] == 40 and header['flags'] & 0xff000000 == 0:
        libraries.add('libgcc_s.so.1')

    # Calculate all symbols each library provides
    for library in libraries:
        path = find_lib(library)
        if not path:
            sys.exit("Library not found: " + library + " in path: "
                    + ':'.join(lib_path))
        symbols = provided_symbols(path)
        library_symbols[library] = {}
        library_symbols_used[library] = set()
        for symbol in symbols:
            for name in symbol.base_names():
                if name in symbol_provider:
                    # in doubt, prefer symbols from libc
                    if re.match("^libc[\.-]", library):
                        library_symbols[library][name] = symbol
                        symbol_provider[name] = library
                    else:
                        debug(DEBUG_SPAM, "duplicate symbol %s in %s and %s" % (symbol, symbol_provider[name], library))
                else:
                    library_symbols[library][name] = symbol
                    symbol_provider[name] = library

    # which symbols are actually used from each lib
    for name in needed_symbols:
        if not name in symbol_provider:
            if not needed_symbols[name].weak:
                raise "No library provides non-weak %s" % symbol
        else:
            lib = symbol_provider[name]
            library_symbols_used[lib].add(library_symbols[lib][name])

    # reduce libraries
    for library in libraries:
        debug(DEBUG_VERBOSE, "reducing", library)
        debug(DEBUG_SPAM, "using: " + ' '.join([str(i) for i in library_symbols_used[library]]))
        so_file = find_lib(library)
        if root and (re.compile("^" + root).search(so_file)):
            debug(DEBUG_VERBOSE, "no action required for " + so_file)
            if not so_file in available_libs:
                debug(DEBUG_VERBOSE, "adding " + so_file + " to available libs")
                available_libs.append(so_file)
            continue
        so_file_name = os.path.basename(so_file)
        if not so_file:
            sys.exit("File not found:" + library)
        pic_file = find_pic(library)
        if not pic_file:
            # No pic file, so we have to use the .so file, no reduction
            debug(DEBUG_VERBOSE, "No pic file found for", so_file, "; copying")
            command(target + "objcopy", "--strip-unneeded -R .note -R .comment",
                    so_file, dest_path + "/" + so_file_name + "-so-stripped")
        else:
            # we have a pic file, recompile
            debug(DEBUG_SPAM, "extracting from:", pic_file, "so_file:", so_file)
            soname = extract_soname(so_file)
            if soname == "":
                debug(DEBUG_VERBOSE, so_file, " has no soname, copying")
                continue
            debug(DEBUG_SPAM, "soname:", soname)

            symbols = set()
            extra_flags = []
            extra_pre_obj = []
            extra_post_obj = []

            symbols.update(library_symbols_used[library])

            # libc.so.6 needs its soinit.o and sofini.o as well as the pic
            if soname in ("libc.so.6", "libc.so.6.1"):
                # force dso_handle.os to be included, otherwise reduced libc
                # may segfault in ptmalloc_init due to undefined weak reference
                extra_pre_obj.append(libc_extras_dir + "/soinit.o")
                extra_post_obj.append(libc_extras_dir + "/sofini.o")
                symbols.add(ProvidedSymbol('__dso_handle', 'Base', True))

            map_file = find_pic_map(library)
            if map_file:
                extra_flags.append("-Wl,--version-script=" + map_file)

            # compile in only used symbols
            cmd = []
            cmd.append("-nostdlib -nostartfiles -shared -Wl,-soname=" + soname)
            cmd.extend(["-u%s" % a.linker_name() for a in symbols])
            cmd.extend(["-o", dest_path + "/" + so_file_name + "-so"])
            cmd.extend(extra_pre_obj)
            cmd.append(pic_file)
            cmd.extend(extra_post_obj)
            cmd.extend(extra_flags)
            cmd.append("-lgcc")
            cmd.extend(["-L%s" % a for a in [dest_path] + lib_path])
            cmd.append(library_depends_gcc_libnames(so_file))
            command(target + "gcc", *cmd)

            # strip result
            command(target + "objcopy", "--strip-unneeded -R .note -R .comment",
                      dest_path + "/" + so_file_name + "-so",
                      dest_path + "/" + so_file_name + "-so-stripped")
            ## DEBUG
            debug(DEBUG_VERBOSE, so_file, "\t", `os.stat(so_file)[ST_SIZE]`)
            debug(DEBUG_VERBOSE, dest_path + "/" + so_file_name + "-so", "\t",
                  `os.stat(dest_path + "/" + so_file_name + "-so")[ST_SIZE]`)
            debug(DEBUG_VERBOSE, dest_path + "/" + so_file_name + "-so-stripped",
                  "\t", `os.stat(dest_path + "/" + so_file_name + "-so-stripped")[ST_SIZE]`)

# Finalising libs and cleaning up
for lib in regexpfilter(os.listdir(dest_path), "(.*)-so-stripped$"):
    os.rename(dest_path + "/" + lib + "-so-stripped", dest_path + "/" + lib)
for lib in regexpfilter(os.listdir(dest_path), "(.*-so)$"):
    os.remove(dest_path + "/" + lib)

# Canonicalize library names.
for lib in regexpfilter(os.listdir(dest_path), "(.*so[.\d]*)$"):
    this_lib_path = dest_path + "/" + lib
    if os.path.islink(this_lib_path):
        debug(DEBUG_VERBOSE, "Unlinking %s." % lib)
        os.remove(this_lib_path)
        continue
    soname = extract_soname(this_lib_path)
    if soname:
        debug(DEBUG_VERBOSE, "Moving %s to %s." % (lib, soname))
        os.rename(dest_path + "/" + lib, dest_path + "/" + soname)

# Make sure the dynamic linker is present and is executable
ld_file_name = os.path.basename(ldlib)
ld_file = find_lib(ld_file_name)

if not os.access(dest_path + "/" + ld_file_name, os.F_OK):
    debug(DEBUG_NORMAL, "I: stripping and copying dynamic linker.")
    command(target + "objcopy", "--strip-unneeded -R .note -R .comment",
            ld_file, dest_path + "/" + ld_file_name)

os.chmod(dest_path + "/" + ld_file_name, 0755)
