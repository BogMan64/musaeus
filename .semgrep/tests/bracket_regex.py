import re

# ruleid: bracket-regex-outside-brackets-module
_BAD = re.compile(r"[\(\[].*?[\)\]]")

# ok: bracket-regex-outside-brackets-module
_FINE = re.compile(r"\d{4}")
