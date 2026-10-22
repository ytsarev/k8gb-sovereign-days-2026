"""Audience-facing cluster names derived from the demo configuration."""
from core import clean


def cluster_name(config, key):
    return key + " · " + clean(config["clusters"][key]["geotag"])


def cluster_mapping(config):
    return "Geotags: A = %s (primary) · B = %s (secondary)" % tuple(
        clean(config["clusters"][key]["geotag"]) for key in ("A", "B"))
