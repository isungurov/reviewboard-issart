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
                                        RepositoryNotFoundError, \
                                        SCMError, UnknownRevision, \
                                        UnmergedCommitsFound


# Register these URI schemes so we can handle them properly.
urlparse.uses_netloc.append('git')


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
        except FileNotFoundError:
            return False

    def parse_diff_revision(self, file_str, revision_str):
        revision = revision_str
        if file_str == "/dev/null":
            revision = PRE_CREATION
        return file_str, revision

    def get_diffs_use_absolute_paths(self):
        return True

    def get_fields(self):
        return ['diff_path']

    def get_parser(self, data):
        return GitDiffParser(data)

    def get_branches_diff(self, branch1, branch2):
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
        pass
        #self.client.fetch()
        #self.client.checkout('master')
        #self.client.pull('origin', 'master')

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
            (i, file) = self._parse_diff(i)
            if file:
                self.files.append(file)
        return self.files

    def _parse_diff(self, i):
        """
        Parses out one file from a Git diff
        """
        if self.lines[i].startswith("diff --git"):
            # First check if it is a new file with no content or
            # a file mode change with no content or
            # a deleted file with no content
            # then skip
            try:
                if ((self.lines[i + 1].startswith("new file mode") or
                     self.lines[i + 1].startswith("old mode") or
                     self.lines[i + 1].startswith("deleted file mode")) and
                    self.lines[i + 3].startswith("diff --git")):
                    i += 3
                    return i, None
            except IndexError, x:
                # This means this is the only bit left in the file
                i += 3
                return i, None

            # Now we have a diff we are going to use so get the filenames + commits
            file = File()
            file.data = self.lines[i] + "\n"
            file.binary = False
            diffLine = self.lines[i].split()
            try:
                # Need to remove the "a/" and "b/" prefix
                remPrefix = re.compile("^[a|b]/");
                file.origFile = remPrefix.sub("", diffLine[-2])
                file.newFile = remPrefix.sub("", diffLine[-1])
            except ValueError:
                raise DiffParserError(
                    "The diff file is missing revision information",
                    i)
            i += 1

            # We have no use for recording this info so skip it
            if self.lines[i].startswith("new file mode") \
               or self.lines[i].startswith("deleted file mode"):
                i += 1
            elif self.lines[i].startswith("old mode") \
                 and self.lines[i + 1].startswith("new mode"):
                i += 2

            # Get the revision info
            if i < len(self.lines) and self.lines[i].startswith("index "):
                indexRange = self.lines[i].split(None, 2)[1]
                file.origInfo, file.newInfo = indexRange.split("..")
                if self.pre_creation_regexp.match(file.origInfo):
                    file.origInfo = PRE_CREATION
                i += 1

            # Get the changes
            while i < len(self.lines):
                if self.lines[i].startswith("diff --git"):
                    return i, file

                if self.lines[i].startswith("Binary files") or \
                   self.lines[i].startswith("GIT binary patch"):
                    file.binary = True
                    return i + 1, file

                if i + 1 < len(self.lines) and \
                   (self.lines[i].startswith('--- ') and \
                     self.lines[i + 1].startswith('+++ ')):
                    if self.lines[i].split()[1] == "/dev/null":
                        file.origInfo = PRE_CREATION

                file.data += self.lines[i] + "\n"
                i += 1

            return i, file
        return i + 1, None


class GitClient(object):
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
    