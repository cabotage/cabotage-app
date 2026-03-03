import hashlib
import re
import secrets

from unidecode import unidecode

_punct_re = re.compile(r'[\t !"#$%&\'()*\-/<=>?@\[\\\]^_`{|},.]+')


def slugify(text, delim="-"):
    """Generates an ASCII-only slug."""
    result = []
    for word in _punct_re.split(text.lower()):
        result.extend(unidecode(word).split())
    return str(delim.join(result))


def generate_k8s_identifier(slug, hex_bytes=4):
    """Generate a k8s-safe identifier: '{slug_prefix}-{random_hex}'.

    Truncates slug to keep total length <= 40 chars.
    """
    hex_suffix = secrets.token_hex(hex_bytes)  # 8 hex chars
    max_prefix_len = 40 - len(hex_suffix) - 1  # room for hyphen
    prefix = slug[:max_prefix_len].rstrip("-")
    return f"{prefix}-{hex_suffix}"


def safe_k8s_name(*parts, max_len=63):
    """Join parts with hyphens, truncating with a hash suffix if too long."""
    name = "-".join(parts)
    if len(name) <= max_len:
        return name
    digest = hashlib.sha256(name.encode()).hexdigest()[:8]
    return name[: max_len - 9].rstrip("-") + "-" + digest


class DictDiffer(object):
    """
    Calculate the difference between two dictionaries as:
    (1) items added
    (2) items removed
    (3) keys same in both but changed values
    (4) keys same in both and unchanged values

    Adapted from https://github.com/hughdbrown/dictdiffer/blob/f1041907faf2f33d477d6c79edd2bf7a8dc1dc86/dictdiffer/__init__.py

    The MIT License (MIT)

    Copyright (c) 2013, Hugh Brown.

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in
    all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
    THE SOFTWARE.
    """

    def __init__(self, current_dict, past_dict, ignored_keys=None):
        if ignored_keys is None:
            ignored_keys = []
        self.current_dict, self.past_dict = current_dict, past_dict
        self.current_keys, self.past_keys = [
            set([k for k in d.keys() if k not in ignored_keys])
            for d in (current_dict, past_dict)
        ]
        self.intersect = self.current_keys.intersection(self.past_keys)

    def added(self):
        return self.current_keys - self.intersect

    def removed(self):
        return self.past_keys - self.intersect

    def changed(self):
        return set(
            o for o in self.intersect if self.past_dict[o] != self.current_dict[o]
        )

    def unchanged(self):
        return set(
            o for o in self.intersect if self.past_dict[o] == self.current_dict[o]
        )

    def has_changes(self):
        return self.added() or self.removed() or self.changed()

    def __repr__(self):
        return (
            "<DictDiffer "
            f"Added: {self.added()}, "
            f"Removed: {self.removed()}, "
            f"Changed: {self.changed()}"
            ">"
        )

    @property
    def asdict(self):
        return {
            "added": list(self.added()),
            "removed": list(self.removed()),
            "changed": list(self.changed()),
        }
