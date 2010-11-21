import datetime
import logging
import os
import re
import subprocess
import urllib2
import urlparse
import time
import signal
import cStringIO


# Python 2.5+ provides urllib2.quote, whereas Python 2.4 only
# provides urllib.quote.
try:
    from urllib2 import quote as urllib_quote
except ImportError:
    from urllib import quote as urllib_quote

from django.utils.translation import ugettext_lazy as _
from djblets.util.filesystem import is_exe_in_path

from django.utils.translation import ugettext as _

from reviewboard.diffviewer.parser import DiffParser, DiffParserError, File
from reviewboard.scmtools.core import SCMTool, HEAD, PRE_CREATION, Log
from reviewboard.scmtools.errors import FileNotFoundError, \
                                        InvalidRevisionFormatError, \
                                        RepositoryNotFoundError, \
                                        SCMError, UnknownRevision, \
                                        UnmergedCommitsFound


GIT_DIFF_EMPTY_CHANGESET_SIZE = 3
GIT_DIFF_PREFIX = re.compile('^[ab]/')


# Register these URI schemes so we can handle them properly.
urlparse.uses_netloc.append('git')


class ShortSHA1Error(InvalidRevisionFormatError):
    def __init__(self, path, revision, *args, **kwargs):
        InvalidRevisionFormatError.__init__(
            self,
            path=path,
            revision=revision,
            detail='The SHA1 is too short. Make sure the diff is generated '
                   'with `git diff --full-index`.',
            *args, **kwargs)


class GitTool(SCMTool):
    """
    You can only use this tool with a locally available git repository.
    The repository path should be to the .git directory (important if
    you do not have a bare repositry).
    """
    name = "Git"
    supports_raw_file_urls = True
    dependencies = {
        'executables': ['git']
    }
    last_update = datetime.datetime.fromordinal(1)
    UPDATE_INTERVAL = datetime.timedelta(seconds=5)

    def __init__(self, repository):
        SCMTool.__init__(self, repository)
        self.client = GitClient(repository.path, repository.raw_file_url)

    def get_file(self, path, revision=HEAD):
        if revision == PRE_CREATION:
            return ""

        return self.client.get_file(path, revision)

    def file_exists(self, path, revision=HEAD):
        if revision == PRE_CREATION:
            return False

        try:
            return self.client.get_file_exists(path, revision)
        except (FileNotFoundError, InvalidRevisionFormatError):
            return False

    def parse_diff_revision(self, file_str, revision_str):
        revision = revision_str

        if file_str == "/dev/null":
            revision = PRE_CREATION
        elif revision != PRE_CREATION:
            self.client.validate_sha1_format(file_str, revision)

        return file_str, revision

    def get_diffs_use_absolute_paths(self):
        return True

    def get_fields(self):
        return ['diff_path']

    def get_parser(self, data):
        return GitDiffParser(data)

    def get_branches_diff(self, branch1, branch2):
        self.update_cache()
        not_merged_commits = self.get_log(branch2, branch1)
        if len(not_merged_commits) != 0:
            raise UnmergedCommitsFound(not_merged_commits)
        return self.client.diff(branch1, branch2)

    def get_branch_log(self, branch, limit=None):
        return self.get_log('master', branch, limit)

    def get_log(self, from_rev, to_rev, limit=None):
        content = self.client.log(from_rev, to_rev, limit)
        entries = content.split('@@@')[:-1]
        p = self.client.log_pattern

        result = [Log(p.search(e).groupdict()) for e in entries]

        return result

    def get_filenames_in_branch(self, branch):
        content = self.client.get_changed_filenames('master', branch)
        result = ['/' + f for f in content.split('\n')]

        return result

    def get_branches(self):
        return self.client.get_branches()

    def update_cache(self):
        now = datetime.datetime.now()
        if (now - self.last_update) >= self.UPDATE_INTERVAL:
            self.last_update = now
            self.client.fetch()

    def is_valid_revision(self, rev):
        try:
            print self.client.name_rev(rev)
            is_valid = True
        except SCMError, e:
            if not str(e).find('Could not get commit') > -1:
                raise
            is_valid = False

        return is_valid

    def normalize_branch_name(self, branch):
        if not branch.startswith('origin/'):
            return 'origin/' + branch

        return branch

    @classmethod
    def check_repository(cls, path, username=None, password=None):
        """
        Performs checks on a repository to test its validity.

        This should check if a repository exists and can be connected to.
        This will also check if the repository requires an HTTPS certificate.

        The result is returned as an exception. The exception may contain
        extra information, such as a human-readable description of the problem.
        If the repository is valid and can be connected to, no exception
        will be thrown.
        """
        super(GitTool, cls).check_repository(path, username, password)

        client = GitClient(path)

        if not client.is_valid_repository():
            raise RepositoryNotFoundError()

        # TODO: Check for an HTTPS certificate. This will require pycurl.


class GitDiffParser(DiffParser):
    """
    This class is able to parse diffs created with Git
    """
    pre_creation_regexp = re.compile("^0+$")

    def parse(self):
        """
        Parses the diff, returning a list of File objects representing each
        file in the diff.
        """
        self.files = []
        i = 0
        while i < len(self.lines):
            i, file_info = self._parse_diff(i)
            if file_info:
                self._ensure_file_has_required_fields(file_info)
                self.files.append(file_info)
        return self.files

    def _parse_diff(self, linenum):
        """
        Parses out one file from a Git diff
        """
        if self.lines[linenum].startswith("diff --git"):
            return self._parse_git_diff(linenum)
        else:
            return linenum + 1, None

    def _parse_git_diff(self, linenum):
        # First check if it is a new file with no content or
        # a file mode change with no content or
        # a deleted file with no content
        # then skip

        try:
            if self._is_empty_change(linenum):
                linenum += GIT_DIFF_EMPTY_CHANGESET_SIZE
                return linenum, None
        except IndexError:
            # This means this is the only bit left in the file
            linenum += GIT_DIFF_EMPTY_CHANGESET_SIZE
            return linenum, None

        # Now we have a diff we are going to use so get the filenames + commits
        file_info = File()
        file_info.data = self.lines[linenum] + "\n"
        file_info.binary = False
        diff_line = self.lines[linenum].split()

        try:
            # Need to remove the "a/" and "b/" prefix
            file_info.origFile = GIT_DIFF_PREFIX.sub("", diff_line[-2])
            file_info.newFile = GIT_DIFF_PREFIX.sub("", diff_line[-1])
        except ValueError:
            raise DiffParserError('The diff file is missing revision '
                                  'information', linenum)
        linenum += 1

        # We have no use for recording this info so skip it
        if self._is_newfile_or_deleted_change(linenum):
            linenum += 1
        elif self._is_mode_change(linenum):
            linenum += 2

        if self._is_index_range_line(linenum):
            index_range = self.lines[linenum].split(None, 2)[1]

            if '..' in index_range:
                file_info.origInfo, file_info.newInfo = index_range.split("..")

            if self.pre_creation_regexp.match(file_info.origInfo):
                file_info.origInfo = PRE_CREATION

            linenum += 1

        # Get the changes
        while linenum < len(self.lines):
            if self._is_git_diff(linenum):
                return linenum, file_info

            if self._is_binary_patch(linenum):
                file_info.binary = True
                return linenum + 1, file_info

            if self._is_diff_fromfile_line(linenum):
                if self.lines[linenum].split()[1] == "/dev/null":
                    file_info.origInfo = PRE_CREATION

            file_info.data += self.lines[linenum] + "\n"
            linenum += 1

        return linenum, file_info

    def _is_empty_change(self, linenum):
        next_diff_start = self.lines[linenum + GIT_DIFF_EMPTY_CHANGESET_SIZE]
        next_line = self.lines[linenum + 1]
        return ((next_line.startswith("new file mode") or
                 next_line.startswith("old mode") or
                 next_line.startswith("deleted file mode"))
                and next_diff_start.startswith("diff --git"))

    def _is_newfile_or_deleted_change(self, linenum):
        line = self.lines[linenum]

        return (line.startswith("new file mode")
                or line.startswith("deleted file mode"))

    def _is_mode_change(self, linenum):
        return (self.lines[linenum].startswith("old mode")
                and self.lines[linenum + 1].startswith("new mode"))

    def _is_index_range_line(self, linenum):
        return (linenum < len(self.lines) and
                self.lines[linenum].startswith("index "))

    def _is_git_diff(self, linenum):
        return self.lines[linenum].startswith('diff --git')

    def _is_binary_patch(self, linenum):
        line = self.lines[linenum]

        return (line.startswith("Binary files") or
                line.startswith("GIT binary patch"))

    def _is_diff_fromfile_line(self, linenum):
        return (linenum + 1 < len(self.lines) and
                (self.lines[linenum].startswith('--- ') and
                    self.lines[linenum + 1].startswith('+++ ')))

    def _ensure_file_has_required_fields(self, file_info):
        """
        This is needed so that there aren't explosions higher up
        the chain when the web layer is expecting a string object.

        """
        for attr in ('origInfo', 'newInfo', 'data'):
            if getattr(file_info, attr) is None:
                setattr(file_info, attr, '')


class GitClient(object):
    FULL_SHA1_LENGTH = 40
	
	# TODO: Move to configuration
    PROCESS_TIMEOUT = 5 * 60

    schemeless_url_re = re.compile(
        r'^(?P<username>[A-Za-z0-9_\.-]+@)?(?P<hostname>[A-Za-z0-9_\.-]+):'
        r'(?P<path>.*)')

    def __init__(self, path, raw_file_url=None):
        if not is_exe_in_path('git'):
            # This is technically not the right kind of error, but it's the
            # pattern we use with all the other tools.
            raise ImportError

        self.path = self._normalize_git_url(path)
        self.raw_file_url = raw_file_url
        self.git_dir = None

        url_parts = urlparse.urlparse(self.path)

        if url_parts[0] == 'file':
            self.git_dir = url_parts[2]

    def is_valid_repository(self):
        """Checks if this is a valid Git repository."""

        try:
            self.run_git('ls-remote', self.path, 'HEAD')
        except SCMError, e:
            logging.error("Git: Failed to find valid repository %s: %s" %
                          (self.path, str(e)))
            return False

        return True

    def get_file(self, path, revision):
        if self.raw_file_url:
            self.validate_sha1_format(path, revision)

            # First, try to grab the file remotely.
            try:
                url = self._build_raw_url(path, revision)
                return urllib2.urlopen(url).read()
            except Exception, e:
                logging.error("Git: Error fetching file from %s: %s" % (url, e))
                raise SCMError("Error fetching file from %s: %s" % (url, e))
        else:
            return self._cat_file(path, revision, "blob")

    def get_file_exists(self, path, revision):
        if self.raw_file_url:
            self.validate_sha1_format(path, revision)

            # First, try to grab the file remotely.
            try:
                url = self._build_raw_url(path, revision)
                return urllib2.urlopen(url).geturl()
            except urllib2.HTTPError, e:
                if e.code != 404:
                    logging.error("Git: HTTP error code %d when fetching "
                                  "file from %s: %s" % (e.code, url, e))
            except Exception, e:
                logging.error("Git: Error fetching file from %s: %s" % (url, e))

            return False
        else:
            contents = self._cat_file(path, revision, "-t")
            return contents and contents.strip() == "blob"

    def validate_sha1_format(self, path, sha1):
        """Validates that a SHA1 is of the right length for this repository."""
        if self.raw_file_url and len(sha1) != self.FULL_SHA1_LENGTH:
            raise ShortSHA1Error(path, sha1)

    def _build_raw_url(self, path, revision):
        url = self.raw_file_url
        url = url.replace("<revision>", revision)
        url = url.replace("<filename>", urllib_quote(path))
        return url

    def _cat_file(self, path, revision, option):
        """
        Call git-cat-file(1) to get content or type information for a
        repository object.

        If called with just "commit", gets the content of a blob (or
        raises an exception if the commit is not a blob).

        Otherwise, "option" can be used to pass a switch to git-cat-file,
        e.g. to test or existence or get the type of "commit".
        """
        commit = self._resolve_head(revision, path)

        try:
            contents = self.run_git('cat-file', option, commit)
        except SCMError, e:
            if str(e).find("fatal: Not a valid object name") > -1:
                raise FileNotFoundError(commit)
            else:
                raise

        return contents

    def _resolve_head(self, revision, path):
        if revision == HEAD:
            if path == "":
                raise SCMError("path must be supplied if revision is %s" % HEAD)
            return "HEAD:%s" % path
        else:
            return str(revision)

    def _normalize_git_url(self, path):
        if path.startswith('file://'):
            return path

        url_parts = urlparse.urlparse(path)
        scheme = url_parts[0]
        netloc = url_parts[1]

        if scheme and netloc:
            return path

        m = self.schemeless_url_re.match(path)

        if m:
            path = m.group('path')

            if not path.startswith('/'):
                path = '/' + path

            return 'ssh://%s%s%s' % (m.group('username'),
                                     m.group('hostname'),
                                     path)

        return "file://" + path

    def diff(self, from_rev, to_rev):
        contents = self.run_git('diff', '--full-index', '..'.join((str(from_rev), str(to_rev))))
        return contents

    log_pattern = re.compile(
            r'(?P<revision>[a-z0-9]{40})\n(?P<author>[\w ]+)\n(?P<message>.+)',
            re.DOTALL)

    def log(self, from_rev, to_rev, limit=None):
        args = ['log', '--pretty=format:"%H%n%an%n%s%b@@@"']
        if limit:
            args.append('-' + str(limit))

        args.append('..'.join((str(from_rev), str(to_rev))))
        contents = self.run_git(*args)

        return contents

    def get_changed_filenames(self, from_rev, to_rev):
        contents = self.run_git('diff', '--name-only', '..'.join((str(from_rev), str(to_rev))))
        return contents

    def get_branches(self):
        contents = self.run_git('branch', '-a')
        branches = [b[10:] if b.startswith('  remotes/') else b[2:] for b in contents.split('\n')]

        return branches
    
    def pull(self, remote, branch):
        self.run_git('pull', remote, branch)

    def fetch(self):
        self.run_git('fetch')

    def checkout(self, branch):
        self.run_git('checkout', branch)

    def name_rev(self, rev):
        return self.run_git('name-rev', rev)

    def get_var(self, varname):
        try:
            result = self.run_git('config', varname)
        except SCMError, e:
            if len(e) == 0:
                result = None
            else:
                raise

        return result

    def run_git(self, *args):
        git_args = ['git', '--git-dir=%s' % self.git_dir]
        for a in args:
            git_args.append(a)

#        print git_args
        p = subprocess.Popen(
            git_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            close_fds=(os.name != 'nt'),
            cwd=self.git_dir
        )

        outstream = cStringIO.StringIO()

        fin_time = time.time() + self.PROCESS_TIMEOUT
        while p.poll() == None and fin_time > time.time():
            outstream.write(p.stdout.read())
            time.sleep(1)
        outstream.write(p.stdout.read())

        content = outstream.getvalue()
#        print '>>>', content, '<<<'
        outstream.close()

        if fin_time < time.time():
            os.kill(p.pid, signal.SIGKILL)
            raise OSError("GIT process timeout has been reached")

        errmsg = p.stderr.read()
#        print '!>>>', errmsg, '<<<!'

        if p.returncode:
            if errmsg.find('unknown revision'):
                raise UnknownRevision(errmsg)
            raise SCMError(errmsg)

        return content
    
