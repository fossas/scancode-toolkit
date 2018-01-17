#
# Copyright (c) 2018 nexB Inc. and others. All rights reserved.
# http://nexb.com and https://github.com/nexB/scancode-toolkit/
# The ScanCode software is licensed under the Apache License version 2.0.
# Data generated with ScanCode require an acknowledgment.
# ScanCode is a trademark of nexB Inc.
#
# You may not use this software except in compliance with the License.
# You may obtain a copy of the License at: http://apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software distributed
# under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
# CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.
#
# When you publish or redistribute any data created with ScanCode or any ScanCode
# derivative work, you must accompany this data with the following acknowledgment:
#
#  Generated with ScanCode and provided on an "AS IS" BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, either express or implied. No content created from
#  ScanCode should be considered or used as legal advice. Consult an Attorney
#  for any legal advice.
#  ScanCode is a free software code scanning tool from nexB Inc. and others.
#  Visit https://github.com/nexB/scancode-toolkit/ for support and download.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import codecs
from collections import deque
from collections import OrderedDict
from functools import partial
import json
from os import walk as os_walk
from os.path import abspath
from os.path import exists
from os.path import expanduser
from os.path import join
from os.path import normpath
from time import time
import traceback
import sys

import attr
import yg.lockfile  # @UnresolvedImport

from commoncode.filetype import is_file as filetype_is_file
from commoncode.filetype import is_special

from commoncode.fileutils import as_posixpath
from commoncode.fileutils import create_dir
from commoncode.fileutils import delete
from commoncode.fileutils import file_name
from commoncode.fileutils import fsdecode
from commoncode.fileutils import fsencode
from commoncode.fileutils import get_temp_dir
from commoncode.fileutils import parent_directory
from commoncode.functional import iter_skip
from commoncode.timeutils import time2tstamp
from commoncode import ignore
from commoncode.system import on_linux

from scancode import cache_dir
from scancode import scans_cache_dir

# Python 2 and 3 support
try:
    # Python 2
    unicode
    str_orig = str
    bytes = str  # @ReservedAssignment
    str = unicode  # @ReservedAssignment
except NameError:
    # Python 3
    unicode = str  # @ReservedAssignment


"""
This module provides Codebase and Resource objects as an abstraction for files
and directories used throughout ScanCode. ScanCode deals with a lot of these as
they are the basic unit of processing.

A Codebase is a tree of Resource. A Resource represents a file or directory and
holds file information as attributes and scans (optionally cached on-disk). This
module handles all the details of walking files, path handling and caching
scans.
"""


# Tracing flags
TRACE = False

def logger_debug(*args):
    pass

if TRACE:
    import logging

    logger = logging.getLogger(__name__)
    # logging.basicConfig(level=logging.DEBUG, stream=sys.stdout)
    logging.basicConfig(stream=sys.stdout)
    logger.setLevel(logging.DEBUG)

    def logger_debug(*args):
        return logger.debug(' '.join(isinstance(a, unicode)
                                     and a or repr(a) for a in args))


# A global cache of codebase objects, keyed by a unique integer ID.
# We use this weird structure such that a Resource object can reference its
# parent codebase object without actually storing it as an instance variable.
# Instead a Resource only has a pointer to a codebase id and can fetch it from
# this cache with an id lookup.
# This cache is updated when a new codebase object is created or destroyed
# TODO: consider using a class variable instead of a module variable?
_CODEBASES = {}

_cache_lock_file = join(cache_dir, 'codebases-lockfile')


def add_codebase(codebase, cache_lock_file=_cache_lock_file):
    """
    Add codebase to codebase cache in a thread- and multiprocess-safe way.
    Return the codebase integer id.
    """
    try:
        # acquire lock and wait until timeout to get a lock or die
        with yg.lockfile.FileLock(cache_lock_file, timeout=10):
            global _CODEBASES
            if _CODEBASES:
                for cid, cached_codebase in _CODEBASES.items():
                    if codebase is cached_codebase:
                        return cid
                # get a new cid
                new_cid = max(_CODEBASES.viewkeys()) + 1
            else:
                # or create a new cid
                new_cid = 1

            _CODEBASES[new_cid] = codebase
            return new_cid

    except yg.lockfile.FileLockTimeout:
        raise


def del_codebase(cid, cache_lock_file=_cache_lock_file):
    """
    Delete codebase from the codebase cache in a thread- and multiprocess-safe way.
    Return the deleted codebase object or None.
    """
    try:
        # acquire lock and wait until timeout to get a lock or die
        with yg.lockfile.FileLock(cache_lock_file, timeout=10):
            global _CODEBASES
            return _CODEBASES.pop(cid, None)
    except yg.lockfile.FileLockTimeout:
        raise


def get_codebase(cid):
    """
    Return a codebase object with a `cid` codebaset id or None.
    """
    global _CODEBASES
    return _CODEBASES.get(cid)


class Codebase(object):
    """
    Represent a codebase being scanned. A Codebase is a tree of Resources.
    """

    # TODO: add populate progress manager!!!

    def __init__(self, location, use_cache=True, cache_base_dir=scans_cache_dir):
        """
        Initialize a new codebase rooted at the `location` existing file or
        directory.

        If `use_cache` is True, scans will be cached on-disk in a file for each
        Resource in a new unique directory under `cache_base_dir`. Otherwise,
        scans are kept as Resource attributes.
        """
        start = time()
        self.original_location = location

        if on_linux:
            location = fsencode(location)
        else:
            location = fsdecode(location)
        location = abspath(normpath(expanduser(location)))

        # TODO: we should also accept to create "virtual" codebase without a
        # backing filesystem location
        assert exists(location)

        # FIXME: what if is_special(location)???
        self.location = location
        self.base_location = parent_directory(location)

        self.is_file = filetype_is_file(location)

        # list of resources in topdown order where the position is the index of
        # the resource. The first index, 0, is also the root
        self.resources = []
        self.root = None

        # list of errors from collecting the codebase details (such as
        # unreadable file, etc)
        self.errors = []

        # mapping of scan summary data and statistics at the codebase level such
        # as ScanCode version, notice, command options, etc.
        # This is populated automatically as the scan progresses.
        self.summary = OrderedDict()

        # total processing time from start to finish, across all stages.
        # This is populated automatically.
        self.total_time = 0

        # mapping of timings for scan stage as {stage: time in seconds as float}
        # This is populated automatically.
        self.timings = OrderedDict()

        # setup cache
        self.use_cache = use_cache
        self.cache_base_dir = self.cache_dir = None
        self.cache_base_dir = cache_base_dir
        if use_cache:
            # this is unique to this run and valid for the lifetime of this codebase
            self.cache_dir = get_cache_dir(cache_base_dir)
            create_dir(self.cache_dir)

        # this updates the global cache using a file lock
        self.cid = add_codebase(self)

        self.populate()

        self.timings['inventory'] = time() - start
        files_count, dirs_count = self.resource_counts()
        self.summary['initial_files_count'] = files_count
        self.summary['initial_dirs_count'] = dirs_count

        # Flag set to True if file information was requested for results output
        self.with_info = False

        # Flag set to True is strip root was requested for results output
        self.strip_root = False
        # Flag set to True is full root was requested for results output
        self.full_root = False

        # set of resource rid to exclude from outputs
        # This is populated automatically.
        self.filtered_rids = set()


    def populate(self):
        """
        Populate this codebase with Resource objects for this codebase by
        walking its `location` topdown, returning directories then files, each
        in sorted order.
        """
        # clear things
        self.resources = []
        resources = self.resources

        resources_append = resources.append

        cid = self.cid
        rloc = self.location
        rid = 0
        self.root = root = Resource(
            name=file_name(rloc), rid=rid, pid=None, cid=cid,
            is_file=self.is_file, use_cache=self.use_cache)
        resources_append(root)
        if TRACE: logger_debug('Codebase.collect: root:', root)

        if self.is_file:
            # there is nothing else to do
            return

        res_by_loc = {rloc: root}

        def err(error):
            self.errors.append(
                'ERROR: cannot collect files: %(error)s\n' % dict(error=error)
                + traceback.format_exc()
            )

        # we always ignore VCS and some filetypes.
        ignored = partial(ignore.is_ignored, ignores=ignore.ignores_VCS)

        # TODO: this is where we would plug archive walking??
        for top, dirs, files in os_walk(rloc, topdown=True, onerror=err):

            if is_special(top) or ignored(top):
                # note: by design the root location is NEVER ignored
                if TRACE: logger_debug(
                    'Codebase.collect: walk: top ignored:', top, 'ignored:',
                    ignored(top), 'is_special:', is_special(top))
                continue

            parent = res_by_loc[top]

            if TRACE: logger_debug('Codebase.collect: parent:', parent)

            for name in sorted(dirs):
                loc = join(top, name)

                if is_special(loc) or ignored(loc):
                    if TRACE: logger_debug(
                        'Codebase.collect: walk: dir ignored:', loc, 'ignored:',
                        ignored(loc), 'is_special:', is_special(loc))
                    continue

                rid += 1
                res = parent._add_child(name, rid, is_file=False)
                res_by_loc[loc] = res
                resources_append(res)
                if TRACE: logger_debug('Codebase.collect: dir:', res)

            for name in sorted(files):
                loc = join(top, name)

                if is_special(loc) or ignored(loc):
                    if TRACE: logger_debug(
                        'Codebase.collect: walk: file ignored:', loc, 'ignored:',
                        ignored(loc), 'is_special:', is_special(loc))
                    continue

                rid += 1
                res = parent._add_child(name, rid, is_file=True)
                res_by_loc[loc] = res
                resources_append(res)
                if TRACE: logger_debug('Codebase.collect: file:', res)


    def walk(self, topdown=True, sort=False, skip_root=False):
        """
        Yield all Resources for this Codebase.
        Walks the tree top-down in pre-order traversal if `topdown` is True.
        Walks the tree bottom-up in post-order traversal if `topdown` is False.
        If `sort` is True, each level is sorted by Resource name, directories
        first then files.
        If `skip_root` is True, the root resource is not returned.
        """
        # single resources without children
        if not self.root.children_rids:
            return [self.root]

        return self.root.walk(topdown, sort, skip_root)

    def get_resource(self, rid):
        """
        Return the Resource with `rid` or None if it does not exists.
        """
        try:
            return self.resources[rid]
        except IndexError:
            pass

    def add_resource(self, name, parent, is_file=False):
        """
        Create and return a new Resource object as a child of the
        `parent` resource.
        """
        return parent.add_child(name, is_file)

    def _get_next_rid(self):
        """
        Return the next available resource id.
        """
        return len([r for r in self.resources if r is not None])

    def remove_resource(self, resource):
        """
        Remove the `resource` Resource object and all its children from the
        resource tree. Return a list of the removed Resource ids.
        """
        if resource.pid is None:
            raise Exception(
                'Cannot remove the root resource from codebase:', repr(resource))
        rids = [res.rid for res in resource.walk(topdown=True)]
        resources = self.resources
        for rid in rids:
            resources[rid] = None

        parent = resource.parent()
        if parent:
            try:
                parent.children_rids.remove(resource.rid)
            except ValueError:
                if TRACE:
                    logger_debug(
                        'Codebase.remove_resource() failed for Resource:', resource,
                        'at location:', resource.get_path(absolute=True, decode=True))
        return rids

    def counts(self, update=True, skip_root=False):
        """
        Return a tuple of counters (files_count, dirs_count, size) for this
        codebase.
        If `update` is True, update the codebase counts before returning.
        Do not include the root Resource in the counts if `skip_root` is True.
        """
        if update:
            self.update_counts()
        root = self.root

        if skip_root and not self.is_file:
            counts = [(c.files_count, c.dirs_count, c.size) for c in root.children()]
            files_count, dirs_count, size = map(sum, zip(*counts))
        else:
            files_count = root.files_count
            dirs_count = root.dirs_count
            size = root.size
            if self.is_file:
                files_count += 1
            else:
                dirs_count += 1
        return files_count, dirs_count, size

    def update_counts(self):
        """
        Update files_count, dirs_count and size attributes of each Resource in
        this codebase based on the current state.
        """
        # note: we walk bottom up to update things in the proper order
        for resource in self.walk(topdown=False):
            resource._update_children_counts()

    def resource_counts(self, resources=None):
        """
        Return a tuple of quick counters (files_count, dirs_count) for this
        codebase or an optional list of resources.
        """
        resources = resources or self.resources
        
        files_count = 0
        dirs_count = 0
        for res in resources:
            if res is None:
                continue
            if res.is_file:
                files_count += 1
            else:
                dirs_count += 1
        return files_count, dirs_count

    def get_resources_with_rid(self):
        """
        Return an iterable of (rid, resource) for all the resources.
        The order is topdown.
        """
        for rid, res in enumerate(self.resources):
            if res is None:
                continue
            yield rid, res

    def clear(self):
        """
        Purge the codebase cache(s) by deleting the corresponding cached data
        files and in-memodyr structures.
        """
        delete(self.cache_dir)
        del_codebase(self.cid)


@attr.attributes(slots=True)
class Resource(object):
    """
    A resource represent a file or directory with essential "file information"
    and the scanned data details.

    A Resource is a tree that models the fileystem tree structure.

    In order to support lightweight and smaller objects that can be serialized
    and deserialized (such as pickled in multiprocessing) without pulling in a
    whole object tree, a Resource does not store its related objects directly:
    the Codebase it belongs to, its parent Resource and its Resource children
    objects are stored only as integer ids. Querying the Resource relationships
    and walking the Resources tree requires to lookup the corresponding object
    by id in the codebase object.
    """
    # the file or directory name in the OS preferred representation (either
    # bytes on Linux and Unicode elsewhere)
    name = attr.ib()

    # a integer resource id
    rid = attr.ib(type=int, repr=False)

    # the root of a Resource tree has a pid==None by convention
    pid = attr.ib(type=int, repr=False)

    # a integer codebase id
    cid = attr.ib(default=None, type=int, repr=False)

    is_file = attr.ib(default=False, type=bool)

    # a list of rids
    children_rids = attr.ib(default=attr.Factory(list), repr=False)

    errors = attr.ib(default=attr.Factory(list), repr=False)

    # a mapping of scan result. Used when scan result is not cached
    _scans = attr.ib(default=attr.Factory(OrderedDict), repr=False)

    # True is the cache is used. Set at creation time from the codebase settings
    use_cache = attr.ib(default=None, type=bool, repr=False)
    # tuple of cache keys: dir and file name
    cache_keys = attr.ib(default=None, repr=False)

    # external data to serialize
    type = attr.ib(default=None, repr=False)
    base_name = attr.ib(default=None, repr=False)
    extension = attr.ib(default=None, repr=False)
    date = attr.ib(default=None, repr=False)
    sha1 = attr.ib(default=None, repr=False)
    md5 = attr.ib(default=None, repr=False)
    mime_type = attr.ib(default=None, repr=False)
    file_type = attr.ib(default=None, repr=False)
    programming_language = attr.ib(default=None, repr=False)
    is_binary = attr.ib(default=False, type=bool, repr=False)
    is_text = attr.ib(default=False, type=bool, repr=False)
    is_archive = attr.ib(default=False, type=bool, repr=False)
    is_media = attr.ib(default=False, type=bool, repr=False)
    is_source = attr.ib(default=False, type=bool, repr=False)
    is_script = attr.ib(default=False, type=bool, repr=False)

    # These attributes are re/computed for directories and files with children
    size = attr.ib(default=0, type=int, repr=False)
    files_count = attr.ib(default=0, type=int, repr=False)
    dirs_count = attr.ib(default=0, type=int, repr=False)

    # Duration in seconds as float to run all scans for this resource
    scan_time = attr.ib(default=0, repr=False)
    # mapping of timings for each scan as {scan_key: duration in seconds as a float}
    scan_timings = attr.ib(default=None, repr=False)

    def __attrs_post_init__(self):
        # build simple cache keys for this resource based on the hex
        # representation of the resource id: they are guaranteed to be unique
        # within a codebase.
        if self.use_cache is None and hasattr(self.codebase, 'use_cache'):
            self.use_cache = self.codebase.use_cache
        hx = '%08x' % self.rid
        if on_linux:
            hx = fsencode(hx)
        self.cache_keys = hx[-2:], hx

    def is_root(self):
        return self.pid is None

    def _update_children_counts(self):
        """
        Compute counts and update self with these counts from direct children.
        """
        files, dirs, size = self._children_counts()
        if not self.is_file:
            # only set the size for directories
            self.size = size
        self.files_count = files
        self.dirs_count = dirs

    def _children_counts(self):
        """
        Return a tuple of counters (files_count, dirs_count, size) for the
        direct children of this Resource.

        Note: because certain files such as archives can have children, they may
        have a files and dirs counts. The size of a directory is aggregated size
        of its files (including the count of files inside archives).
        """
        files_count = dirs_count = size = 0
        if not self.children_rids:
            return files_count, dirs_count, size

        for res in self.children():
            files_count += res.files_count
            dirs_count += res.dirs_count
            if res.is_file:
                files_count += 1
            else:
                dirs_count += 1
            size += res.size
        return files_count, dirs_count, size

    @property
    def codebase(self):
        """
        Return this Resource codebase from the global cache.
        """
        return get_codebase(self.cid)

    def _get_cached_path(self, create=False):
        """
        Return the path where to get/put a data in the cache given a path.
        Create the directories if requested.
        Will fail with an Exception if the codebase `use_cache` is False.
        """
        if self.use_cache:
            cache_sub_dir, cache_file_name = self.cache_keys
            parent = join(self.codebase.cache_dir, cache_sub_dir)
            if create and not exists(parent):
                create_dir(parent)
            return join(parent, cache_file_name)

    def get_scans(self, _cached_path=None):
        """
        Return a `scans` mapping. Fetch from the cache if the codebase
        `use_cache` is True.
        """
        if not self.use_cache:
            return self._scans

        if not _cached_path:
            _cached_path = self._get_cached_path(create=False)

        if not exists(_cached_path):
            return OrderedDict()

        # TODO: consider messagepack or protobuf for compact/faster processing
        with codecs.open(_cached_path, 'r', encoding='utf-8') as cached:
            return json.load(cached, object_pairs_hook=OrderedDict)

    def put_scans(self, scans, update=True):
        """
        Save the `scans` mapping of scan results for this resource. Does nothing
        if `scans` is empty or None.
        Return the saved mapping of `scans`, possibly updated or empty.
        If `update` is True, existing scans are updated with `scans`.
        If `update` is False, `scans` overwrites existing scans.
        If `self.use_cache` is True, `scans` are saved in the cache.
        Otherwise they are saved in this resource object.
        """
        if TRACE:
            logger_debug('put_scans: scans:', scans, 'update:', update,
                         'use_cache:', self.use_cache)

        if not scans:
            return OrderedDict()

        if not self.use_cache:
            if update:
                self._scans.update(scans)
            else:
                self._scans.clear()
                self._scans.update(scans)

            if TRACE: logger_debug('put_scans: merged:', self._scans)
            return self._scans

        # from here on we use_cache!
        self._scans.clear()
        cached_path = self._get_cached_path(create=True)
        if update:
            existing = self.get_scans(cached_path)
            if TRACE: logger_debug(
                'put_scans: cached_path:', cached_path, 'existing:', existing)

            existing.update(scans)

            if TRACE: logger_debug('put_scans: merged:', existing)
        else:
            existing = scans

        # TODO: consider messagepack or protobuf for compact/faster processing
        with codecs.open(cached_path, 'wb', encoding='utf-8') as cached_file:
            json.dump(existing, cached_file, check_circular=False)

        return existing

    def walk(self, topdown=True, sort=False, skip_root=False):
        """
        Yield Resources for this Resource tree.
        Walks the tree top-down in pre-order traversal if `topdown` is True.
        Walks the tree bottom-up in post-order traversal if `topdown` is False.
        If `sort` is True, each level is sorted by Resource name, directories
        first then files.
        If `skip_root` is True, the root resource is not returned.
        """
        # single root resource without children
        if self.pid == None and not self.children_rids:
            return [self]

        walked = self._walk(topdown, sort)
        if skip_root:
            skip_first = skip_last = False
            if topdown:
                skip_first = True
            else:
                skip_last = True
            walked = iter_skip(walked, skip_first, skip_last)
        return walked

    def _walk(self, topdown=True, sort=False):
        if topdown:
            yield self

        children = self.children()
        if sort and children:
            sorter = lambda r: (r.is_file, r.name)
            children.sort(key=sorter)

        for child in children:
            for subchild in child._walk(topdown, sort):
                yield subchild

        if not topdown:
            yield self

    def add_child(self, name, is_file=False):
        """
        Create and return a child Resource. Add this child to the codebase
        resources and to this Resource children.
        """
        rid = self.codebase._get_next_rid()
        child = self._add_child(name, rid, is_file)
        self.codebase.resources.append(rid)
        return child

    def _add_child(self, name, rid, is_file=False):
        """
        Create a child Resource with `name` and a `rid` Resource id and add its
        id to this Resource children. Return the created child.
        """
        res = Resource(name=name, rid=rid, pid=self.rid, cid=self.cid,
                       is_file=is_file, use_cache=self.use_cache)
        self.children_rids.append(rid)
        return res

    def children(self):
        """
        Return a sequence of direct children Resource objects for this Resource
        or None.
        """
        resources = self.codebase.resources
        return [resources[rid] for rid in self.children_rids]

    def parent(self):
        """
        Return the parent Resource object for this Resource or None.
        """
        if self.pid is not None:
            return self.codebase.resources[self.pid]

    def ancestors(self):
        """
        Return a sequence of ancestor Resource objects from root to self.
        """
        resources = self.codebase.resources
        ancestors = deque()
        ancestors_append = ancestors.appendleft
        current = self
        # walk up the tree parent tree: only the root as a pid==None
        while current.pid is not None:
            ancestors_append(current)
            current = resources[current.pid]
        ancestors_append(current)
        return list(ancestors)

    def get_path(self, absolute=False, strip_root=False, decode=False, posix=False):
        """
        Return a path to self using the preferred OS encoding (bytes on Linux,
        Unicode elsewhere) or Unicode is decode=True.

        - If `absolute` is True, return an absolute path. Otherwise return a
          relative path where the first segment is the root name.

        - If `strip_root` is True, return a relative path without the first root
          segment. Ignored if `absolute` is True.

        - If `decode` is True, return a Unicode path decoded using the filesytem
          encoding.

        - If `posix` is True, ensure that the path uses POSIX slash as
        separators, otherwise use the native path separators.
        """
        ancestors = self.ancestors()
        segments = [a.name for a in ancestors]
        if absolute:
            base_location = self.codebase.base_location
            if posix:
                base_location = as_posixpath(base_location)
            segments.insert(0, base_location)

        elif strip_root:
            if len(segments) > 1:
                # we cannot ever strip the root from the root when there is only
                # one resource!
                segments = segments[1:]

        path = join(*segments)
        if posix:
            path = as_posixpath(path)

        if decode:
            path = fsdecode(path)
        return path

    def set_info(self, info):
        """
        Set `info` file information for this Resource.
        Info is a list of mappings of file information.
        """
        if not info:
            return
        for inf in info:
            for key, value in inf.items():
                setattr(self, key, value)

    def to_dict(self, full_root=False, strip_root=False, with_info=False):
        """
        Return a mapping of representing this Resource and its scans.
        """
        res = OrderedDict()
        res['path'] = fsdecode(
            self.get_path(absolute=full_root, strip_root=strip_root,
                          decode=True, posix=True))
        if with_info:
            res['type'] = self.type
            res['name'] = fsdecode(self.name)
            res['base_name'] = fsdecode(self.base_name)
            res['extension'] = self.extension and fsdecode(self.extension)
            res['date'] = self.date
            res['size'] = self.size
            res['sha1'] = self.sha1
            res['md5'] = self.md5
            res['files_count'] = self.files_count
            res['dirs_count'] = self.dirs_count
            res['mime_type'] = self.mime_type
            res['file_type'] = self.file_type
            res['programming_language'] = self.programming_language
            res['is_binary'] = self.is_binary
            res['is_text'] = self.is_text
            res['is_archive'] = self.is_archive
            res['is_media'] = self.is_media
            res['is_source'] = self.is_source
            res['is_script'] = self.is_script
        res['scan_errors'] = self.errors
        res.update(self.get_scans())
        return res


def get_cache_dir(cache_base_dir):
    """
    Return a new, created and unique cache storage directory path rooted at the
    `cache_base_dir` in the OS- preferred representation (either bytes on Linux
    and Unicode elsewhere).
    """
    create_dir(cache_base_dir)
    # create a unique temp directory in cache_dir
    prefix = time2tstamp() + u'-'
    cache_dir = get_temp_dir(cache_base_dir, prefix=prefix)
    if on_linux:
        cache_dir = fsencode(cache_dir)
    else:
        cache_dir = fsdecode(cache_dir)
    return cache_dir
