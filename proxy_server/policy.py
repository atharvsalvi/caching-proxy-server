"""Freshness rules for this shared cache (RFC 9111 sections 4 and 5)."""

from email.utils import parsedate_to_datetime
import re
from time import time
from urllib.request import parse_http_list

from proxy_server import config


def header(headers, name):
    return next((v for k, v in headers if k.lower() == name), None)


def directives(headers):
    result = {}
    for key, line in headers:
        if key.lower() != "cache-control":
            continue
        for item in parse_http_list(line):
            name, _, value = item.partition("=")
            name = name.strip().lower()
            # Duplicate freshness directives are ambiguous. Treat them as zero.
            result[name] = None if name in result else value.strip()
    return result


def seconds(value):
    if value and len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    if value and value.isascii() and value.isdigit():
        digits = value.lstrip("0")
        if len(digits) > 10:
            return 2147483647
        return min(int(digits or "0"), 2147483647)
    return 0


def http_date(value):
    if value:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is not None:
                return date.timestamp()
        except (ValueError, OverflowError, OSError, TypeError):
            pass
    return None


def validators(headers):
    result = {}
    tag = header(headers, "etag")
    modified = header(headers, "last-modified")
    # Only a single quoted entity tag is a validator. In particular, never
    # turn a malformed ETag: * into If-None-Match: * (matches any version).
    if tag and re.fullmatch(r'(?:W/)?"[\x21\x23-\x7e\x80-\xff]*"', tag):
        result["If-None-Match"] = tag
    if http_date(modified) is not None:
        result["If-Modified-Since"] = modified
    return result


def freshness(headers, requested_at, received_at):
    rules = directives(headers)
    date = http_date(header(headers, "date"))
    if date is None:
        date = received_at
    if "no-cache" in rules:
        lifetime = 0
    elif "s-maxage" in rules:
        lifetime = seconds(rules["s-maxage"])
    elif "max-age" in rules:
        lifetime = seconds(rules["max-age"])
    elif header(headers, "expires") is not None:
        expires = http_date(header(headers, "expires"))
        lifetime = max(0, expires - date) if expires is not None else 0
    else:
        lifetime = config.CACHE_TTL
    age_value = header(headers, "age")
    # Invalid upstream age must not grant extra freshness.
    if age_value is not None and not (age_value.isascii() and age_value.isdigit()):
        lifetime = 0
    age = max(0, received_at - date, seconds(age_value) + max(0, received_at - requested_at))
    return {"received_at": received_at, "initial_age": age, "lifetime": lifetime}


def age(entry, now):
    return entry["initial_age"] + max(0, now - entry["received_at"])


def fresh(entry, request_rules):
    if entry is None:
        return False
    current_age = age(entry, time())
    if "max-age" in request_rules and current_age >= seconds(request_rules["max-age"]):
        return False
    return current_age + seconds(request_rules.get("min-fresh")) < entry["lifetime"]


def grace_seconds(headers):
    rules = directives(headers)
    if {"no-cache", "no-store", "private", "must-revalidate", "proxy-revalidate", "s-maxage"}.intersection(rules):
        return 0
    return seconds(rules.get("stale-while-revalidate"))


def can_serve_stale(entry, request_rules):
    if entry is None or {"no-cache", "no-store", "min-fresh", "max-age"}.intersection(request_rules):
        return False
    stale_for = age(entry, time()) - entry["lifetime"]
    return 0 <= stale_for < grace_seconds(entry["headers"])
