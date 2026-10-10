import re


def safe_id(identifier):
    return re.sub(r"[^A-Za-z0-9.-]", "_", identifier)
