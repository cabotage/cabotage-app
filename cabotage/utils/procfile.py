# -*- coding: utf-8 -*-

"""Parser for Procfiles.

Implements `Smartmob RFC 1 <http://smartmob-rfc.readthedocs.org/en/latest/1-procfile.html>`_.

Modified from https://github.com/smartmob-project/procfile/blob/338756d5b645f17aa2366c34afa3b7a58d880796/procfile/__init__.py

Copyright (c) 2015 strawboss contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

* The above copyright notice and this permission notice shall be included in
  all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""


import re


_PROCFILE_LINE = re.compile(
    "".join(
        [
            r"^(?P<process_type>.+?):\s*",
            r"(?:env(?P<environment>(?:\s+.+?=.+?)+)\s+)?",
            r"(?P<command>.+)$",
        ]
    )
)


def _find_duplicates(items):
    seen = {}
    duplicates = []
    for i, item in items:
        if item in seen:
            duplicates.append((i, item, seen[item]))
        else:
            seen[item] = i
    return duplicates


def _group_lines(lines):
    start, group = (0, [])
    for i, line in enumerate(lines):
        if line.rstrip().endswith("\\"):
            group.append(line[:-1])
        else:
            if group:
                group.append(line.lstrip())
            else:
                group.append(line)
            yield start, "".join(group)
            start, group = (i + 1, [])
    if group:
        yield start, "".join(group[:-1]) + group[-1].rstrip()


def _parse_procfile_line(line):
    line = line.strip()
    match = _PROCFILE_LINE.match(line)
    if match is None:
        raise ValueError('Invalid profile line "%s".' % line)
    parts = match.groupdict()
    environment = parts["environment"]
    if environment:
        environment = [
            tuple(variable.strip().split("=", 1))
            for variable in environment.strip().split(" ")
        ]
    else:
        environment = []
    return (
        parts["process_type"],
        parts["command"],
        environment,
    )


def loads(content):
    """Load a Procfile from a string."""
    lines = _group_lines(line for line in content.split("\n"))
    lines = [(i, _parse_procfile_line(line)) for i, line in lines if line.strip()]
    errors = []
    # Reject files with duplicate process types (no sane default).
    duplicates = _find_duplicates(((i, line[0]) for i, line in lines))
    for i, process_type, j in duplicates:
        errors.append(
            "".join(
                [
                    'Line %d: duplicate process type "%s": ',
                    "already appears on line %d.",
                ]
            )
            % (i + 1, process_type, j + 1)
        )
    # Reject commands with duplicate variables (no sane default).
    for i, line in lines:
        process_type, env = line[0], line[2]
        duplicates = _find_duplicates(((0, var[0]) for var in env))
        for _, variable, _ in duplicates:
            errors.append(
                "".join(
                    [
                        'Line %d: duplicate variable "%s" ',
                        'for process type "%s".',
                    ]
                )
                % (i + 1, variable, process_type)
            )
    # Done!
    if errors:
        raise ValueError(errors)
    return {k: {"cmd": cmd, "env": env} for _, (k, cmd, env) in lines}


def load(stream):
    """Load a Procfile from a file-like object."""
    return loads(stream.read().decode("utf-8"))


def loadfile(path):
    """Load a Procfile from a file."""
    with open(path, "rb") as stream:
        return load(stream)
