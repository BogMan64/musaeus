import re

# ruleid: article-strip-regex-outside-artist-form
def strip_article(name):
    return re.sub(r"^the\s+|,\s*the$", "", name)

# ok: article-strip-regex-outside-artist-form
def strip_feat(title):
    return re.sub(r"\(feat\..*?\)", "", title)
