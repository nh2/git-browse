import os
import subprocess


class GitCommit(object):
    """
    Stores simple information about a single Git commit.
    """
    def __init__(self, sha, author, message):
        self.sha = sha
        self.author = author
        self.message = message


class GitBlameLine(object):
    """
    Stores the blame output for a single line of a file.
    """
    def __init__(self, sha, line, current, original_line, final_line):
        self.sha = sha
        self.line = line
        self.current = current
        self.original_line = original_line
        self.final_line = final_line


class GitFileHistory(object):
    """
    Responsible for following the history of a single file, moving around
    within that history, and giving information about the state of the file
    at a particular revision or the differences between revisions.

    Most operations are relative to the current commit, which can be changed
    with the previous and next mthods and accessed through the current_commit
    property.
    """

    def __init__(self, path, start_commit):
        if not verify_revision(start_commit):
            raise ValueError('%s is not a valid commit, branch, tag, etc.' % (
                start_commit,
            ))

        if not verify_file(path):
            raise ValueError('"%s" is not tracked by git' % (path, ))

        self.path = path

        p = os.popen('git log %s --follow --pretty="%s" -- %s' % (
            start_commit,
            '%H%n%an%n%s%n',
            self.path,
        ))
        output = p.read().split('\n\n')

        self.commits = [GitCommit(*c.split('\n', 2)) for c in output if c]
        self._index = 0
        self._blame = None

        self._line_mappings = {}

    @property
    def current_commit(self):
        return self.commits[self._index]

    def next(self):
        """
        Moves to the next commit that touched this file, returning False
        if we're already at the last commit that touched the file.
        """
        if self._index <= 0:
            return False

        self._index -= 1
        self._blame = None
        return True

    def prev(self):
        """
        Moves to the previous commit that touched this file, returning False
        if we're already at the first commit that touched the file.
        """
        if self._index >= len(self.commits) - 1:
            return False

        self._index += 1
        self._blame = None
        return True

    def blame(self):
        """
        Returns blame information for this file at the current commit as
        a list of GitBlameLine objects.
        """
        if self._blame:
            return self._blame

        lines = []

        p = os.popen('git blame -p %s %s' % (
            self.path,
            self.current_commit.sha,
        ))

        while True:
            header = p.readline()
            if not header:
                break

            # Header format:
            # commit_sha original_line final_line[ lines_in_group]
            sha, original_line, final_line = header.split(' ')[:3]

            line = p.readline()

            # Skip any addition headers describing the commit
            while not line.startswith('\t'):
                line = p.readline()

            lines.append(GitBlameLine(
                sha=sha,
                line=line[1:],
                current=(sha == self.current_commit.sha),
                original_line=original_line,
                final_line=final_line,
            ))

        self._blame = lines
        return self._blame

    def line_mapping(self, start, finish):
        """
        Returns a dict that represents how lines have moved between versions
        of a file. The keys are the line numbers in the version of the file
        at start, the values are where those lines have ended up in the version
        at finish.

        For example if at start the file is two lines, and at
        finish a new line has been inserted between the two the mapping
        would be:
            {1:1, 2:3}

        Deleted lines are represented by None. For example, if at start the
        file were two lines, and the first had been deleted by finish:
            {1:None, 2:1}
        """

        key = start + '/' + finish
        if key in self._line_mappings:
            return self._line_mappings[key]

        forward, backward = self._build_line_mappings(start, finish)
        self._line_mappings[start + '/' + finish] = forward
        self._line_mappings[finish + '/' + start] = backward

        return forward

    def _build_line_mappings(self, start, finish):
        forward = {}
        backward = {}

        # We use `diff` to track blocks of added, deleted and unchanged lines
        # in order to build the line mapping.
        # Its `--old/new/unchanged-group-format` flags make this very easy;
        # it generates output like this:
        #    u 8
        #    o 3
        #    n 4
        #    u 1
        # for a diff in which the first 8 lines are unchanged, then 3 deleted,
        # then 4 added and then 1 unchanged.
        # Below, we parse this output.
        #
        # In order to get the file contents of the two commits into `diff`,
        # we use the equivalent of bash's /dev/fd/N based process subsititution,
        # which would look like this:
        #    diff <(git show commit1:file) <(git show commit2:file)
        # (this works on all platforms where bash process substitution works).

        p_start = os.popen('git show %s:%s' % (start, self.path))
        p_finish = os.popen('git show %s:%s' % (finish, self.path))

        p_diff = subprocess.Popen([
            'diff',
            '/dev/fd/' + str(p_start.fileno()),
            '/dev/fd/' + str(p_finish.fileno()),
            '--old-group-format=o %dn\n',  # lower case n for old file
            '--new-group-format=n %dN\n',  # upper case N for new file
            '--unchanged-group-format=u %dN\n',  # for unchanged it doesn't matter if n or N
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        (out, err) = p_diff.communicate()
        assert err == ''

        # Unfortunately, splitting the empty string in Python still gives us a singleton
        # empty line (`''.split('\n') == ['']`), so we handle that case here.
        diff_lines = [] if out == '' else out.strip().split('\n')

        start_ln = 0
        finish_ln = 0

        for line in diff_lines:
            assert len(line) >= 3
            # Parse the output created with `diff` above.
            typ, num_lines_str = line.split(' ')
            num_lines = int(num_lines_str)

            if typ == 'u':  # unchanged lines, advance both sides
                for i in range(num_lines):
                    forward[start_ln] = finish_ln
                    backward[finish_ln] = start_ln
                    start_ln += 1
                    finish_ln += 1
            elif typ == 'o':  # old/deleted lines, advance left side as they only exist there
                for i in range(num_lines):
                    forward[start_ln] = None
                    start_ln += 1
            elif typ == 'n':  # new/added lines, advance right side as they only exist there
                for i in range(num_lines):
                    backward[finish_ln] = None
                    finish_ln += 1

        p = os.popen('git show %s:%s' % (start, self.path))
        start_len = len(p.readlines())

        p = os.popen('git show %s:%s' % (finish, self.path))
        finish_len = len(p.readlines())

        # Make sure the mappings stretch the the beginning and end of
        # the files.
        while start_ln <= start_len and finish_ln <= finish_len:
            forward[start_ln] = finish_ln
            backward[finish_ln] = start_ln
            start_ln += 1
            finish_ln += 1

        return forward, backward


def verify_revision(rev):
    """
    Verifies that a revision is valid in the current working directory,
    and returns True or False accordingly.

    Errors are not supressed, so if the revision is bad or the CWD isn't
    a Git repository then Git's error message will be output.
    """
    status = os.system('git rev-parse --verify --no-revs %s' % (
        rev
    ))
    return status == 0


def verify_file(path):
    """
    Verifies that a given file is tracked by Git and returns true or false
    accordingly.
    """
    p = os.popen('git ls-files -- %s' % path)
    matching_files = p.readlines()
    return len(matching_files) > 0
