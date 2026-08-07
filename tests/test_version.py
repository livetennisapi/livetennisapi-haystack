from importlib.metadata import version

import livetennisapi_haystack


def test_dunder_version_matches_package_metadata():
    """__init__.__version__ and pyproject.toml must never drift apart again."""
    assert livetennisapi_haystack.__version__ == version("livetennisapi-haystack")
