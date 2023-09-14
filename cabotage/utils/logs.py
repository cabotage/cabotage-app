import re


def filter_secrets(string):
    return re.sub("x-access-token:.+@github.com", "github.com", string)
