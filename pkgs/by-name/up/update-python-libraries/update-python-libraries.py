#!/usr/bin/env python3

"""
Update a Python package expression by passing in the `.nix` file, or the directory containing it.
You can pass in multiple files or paths.

You'll likely want to use
``
  $ ./update-python-libraries ../../pkgs/development/python-modules/**/default.nix
``
to update all non-pinned libraries in that folder.
"""

import argparse
import collections
import json
import logging
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor as Pool
from typing import Any, Optional

import requests
from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion
from packaging.version import Version as _Version

INDEX = "https://pypi.io/pypi"
"""url of PyPI"""

EXTENSIONS = ["tar.gz", "tar.bz2", "tar", "zip", ".whl"]
"""Permitted file extensions. These are evaluated from left to right and the first occurance is returned."""

PRERELEASES = False

BULK_UPDATE = False

NIX = "nix"
NIX_PREFETCH_URL = "nix-prefetch-url"
NIX_PREFETCH_GIT = "nix-prefetch-git"
GIT = "git"

NIXPKGS_ROOT = (
    subprocess.check_output([GIT, "rev-parse", "--show-toplevel"])
    .decode("utf-8")
    .strip()
)

logging.basicConfig(level=logging.INFO)


class Version(_Version, collections.abc.Sequence):
    def __init__(self, version):
        super().__init__(version)
        # We cannot use `str(Version(0.04.21))` because that becomes `0.4.21`
        # https://github.com/avian2/unidecode/issues/13#issuecomment-354538882
        self.raw_version = version

    def __getitem__(self, i):
        return self._version.release[i]

    def __len__(self):
        return len(self._version.release)

    def __iter__(self):
        yield from self._version.release


def _get_values(attribute, text):
    """Match attribute in text and return all matches.

    :returns: List of matches.
    """
    regex = rf'{re.escape(attribute)}\s+=\s+"(.*)";'
    regex = re.compile(regex)
    values = regex.findall(text)
    return values


def _get_attr_value(attr_path: str) -> Optional[Any]:
    try:
        response = subprocess.check_output(
            [
                NIX,
                "--extra-experimental-features",
                "nix-command",
                "eval",
                "-f",
                f"{NIXPKGS_ROOT}/default.nix",
                "--json",
                f"{attr_path}",
            ],
            stderr=subprocess.DEVNULL,
        )
        return json.loads(response.decode())
    except (subprocess.CalledProcessError, ValueError):
        return None


def _get_unique_value(attribute, text):
    """Match attribute in text and return unique match.

    :returns: Single match.
    """
    values = _get_values(attribute, text)
    n = len(values)
    if n > 1:
        raise ValueError("found too many values for {}".format(attribute))
    elif n == 1:
        return values[0]
    else:
        raise ValueError("no value found for {}".format(attribute))


def _get_line_and_value(attribute, text, value=None):
    """Match attribute in text. Return the line and the value of the attribute."""
    if value is None:
        regex = rf"({re.escape(attribute)}\s+=\s+\"(.*)\";)"
    else:
        regex = rf"({re.escape(attribute)}\s+=\s+\"({re.escape(value)})\";)"
    regex = re.compile(regex)
    results = regex.findall(text)
    n = len(results)
    if n > 1:
        raise ValueError("found too many values for {}".format(attribute))
    elif n == 1:
        return results[0]
    else:
        raise ValueError("no value found for {}".format(attribute))


def _replace_value(attribute, value, text, oldvalue=None):
    """Search and replace value of attribute in text."""
    if oldvalue is None:
        old_line, old_value = _get_line_and_value(attribute, text)
    else:
        old_line, old_value = _get_line_and_value(attribute, text, oldvalue)
    new_line = old_line.replace(old_value, value)
    new_text = text.replace(old_line, new_line)
    return new_text


def _fetch_page(url):
    r = requests.get(url)
    if r.status_code == requests.codes.ok:
        return r.json()
    else:
        raise ValueError("request for {} failed".format(url))


def _fetch_github(url):
    headers = {}
    token = os.environ.get("GITHUB_API_TOKEN")
    if token:
        headers["Authorization"] = f"token {token}"
    r = requests.get(url, headers=headers)

    if r.status_code == requests.codes.ok:
        return r.json()
    else:
        raise ValueError("request for {} failed".format(url))


def _hash_to_sri(algorithm, value):
    """Convert a hash to its SRI representation"""
    return (
        subprocess.check_output(
            [
                NIX,
                "--extra-experimental-features",
                "nix-command",
                "hash",
                "to-sri",
                "--type",
                algorithm,
                value,
            ]
        )
        .decode()
        .strip()
    )


def _skip_bulk_update(attr_name: str) -> bool:
    return bool(_get_attr_value(f"{attr_name}.skipBulkUpdate"))


SEMVER = {
    "major": 0,
    "minor": 1,
    "patch": 2,
}


def _determine_latest_version(current_version, target, versions):
    """Determine latest version, given `target`."""
    current_version = Version(current_version)

    def _parse_versions(versions):
        for v in versions:
            try:
                yield Version(v)
            except InvalidVersion:
                pass

    versions = _parse_versions(versions)

    index = SEMVER[target]

    ceiling = list(current_version[0:index])
    if len(ceiling) == 0:
        ceiling = None
    else:
        ceiling[-1] += 1
        ceiling = Version(".".join(map(str, ceiling)))

    # We do not want prereleases
    versions = SpecifierSet(prereleases=PRERELEASES).filter(versions)

    if ceiling is not None:
        versions = SpecifierSet(f"<{ceiling}").filter(versions)

    return (max(sorted(versions))).raw_version


def _get_latest_version_pypi(attr_path, package, extension, current_version, target):
    """Get latest version and hash from PyPI."""
    url = "{}/{}/json".format(INDEX, package)
    json = _fetch_page(url)

    versions = {
        version
        for version, releases in json["releases"].items()
        if not all(release["yanked"] for release in releases)
    }
    version = _determine_latest_version(current_version, target, versions)

    try:
        releases = json["releases"][version]
    except KeyError as e:
        raise KeyError(
            "Could not find version {} for {}".format(version, package)
        ) from e
    for release in releases:
        if release["filename"].endswith(extension):
            # TODO: In case of wheel we need to do further checks!
            sha256 = release["digests"]["sha256"]
            break
    else:
        sha256 = None
    return version, sha256, None


def _get_latest_version_github(attr_path, package, extension, current_version, target):
    def strip_prefix(tag):
        return re.sub("^[^0-9]*", "", tag)

    def get_prefix(string):
        matches = re.findall(r"^([^0-9]*)", string)
        return next(iter(matches), "")

    try:
        homepage = subprocess.check_output(
            [
                NIX,
                "--extra-experimental-features",
                "nix-command",
                "eval",
                "-f",
                f"{NIXPKGS_ROOT}/default.nix",
                "--raw",
                f"{attr_path}.src.meta.homepage",
            ]
        ).decode("utf-8")
    except Exception as e:
        raise ValueError(f"Unable to determine homepage: {e}")
    owner_repo = homepage[len("https://github.com/") :]  # remove prefix
    owner, repo = owner_repo.split("/")

    url = f"https://api.github.com/repos/{owner}/{repo}/releases"
    all_releases = _fetch_github(url)
    releases = list(filter(lambda x: not x["prerelease"], all_releases))

    if len(releases) == 0:
        raise ValueError(f"{homepage} does not contain any stable releases")

    versions = map(lambda x: strip_prefix(x["tag_name"]), releases)
    version = _determine_latest_version(current_version, target, versions)

    release = next(filter(lambda x: strip_prefix(x["tag_name"]) == version, releases))
    prefix = get_prefix(release["tag_name"])

    fetcher = _get_attr_value(f"{attr_path}.src.fetcher")
    if fetcher is not None and fetcher.endswith("nix-prefetch-git"):
        # some attributes require using the fetchgit
        git_fetcher_args = []
        if _get_attr_value(f"{attr_path}.src.fetchSubmodules"):
            git_fetcher_args.append("--fetch-submodules")
        if _get_attr_value(f"{attr_path}.src.fetchLFS"):
            git_fetcher_args.append("--fetch-lfs")
        if _get_attr_value(f"{attr_path}.src.leaveDotGit"):
            git_fetcher_args.append("--leave-dotGit")

        algorithm = "sha256"
        cmd = [
            NIX_PREFETCH_GIT,
            f"https://github.com/{owner}/{repo}.git",
            "--hash",
            algorithm,
            "--rev",
            f"refs/tags/{release['tag_name']}",
        ]
        cmd.extend(git_fetcher_args)
        response = subprocess.check_output(cmd)
        document = json.loads(response.decode())
        hash = _hash_to_sri(algorithm, document[algorithm])
    else:
        try:
            hash = (
                subprocess.check_output(
                    [
                        NIX_PREFETCH_URL,
                        "--type",
                        "sha256",
                        "--unpack",
                        f"{release['tarball_url']}",
                    ],
                    stderr=subprocess.DEVNULL,
                )
                .decode("utf-8")
                .strip()
            )
        except (subprocess.CalledProcessError, UnicodeError):
            # this may fail if they have both a branch and a tag of the same name, attempt tag name
            tag_url = str(release["tarball_url"]).replace(
                "tarball", "tarball/refs/tags"
            )
            try:
                hash = (
                    subprocess.check_output(
                        [NIX_PREFETCH_URL, "--type", "sha256", "--unpack", tag_url],
                        stderr=subprocess.DEVNULL,
                    )
                    .decode("utf-8")
                    .strip()
                )
            except subprocess.CalledProcessError:
                raise ValueError("nix-prefetch-url failed")

    return version, hash, prefix


FETCHERS = {
    "fetchFromGitHub": _get_latest_version_github,
    "fetchPypi": _get_latest_version_pypi,
    "fetchurl": _get_latest_version_pypi,
}


DEFAULT_SETUPTOOLS_EXTENSION = "tar.gz"


FORMATS = {
    "setuptools": DEFAULT_SETUPTOOLS_EXTENSION,
    "wheel": "whl",
    "pyproject": "tar.gz",
    "flit": "tar.gz",
}


def _determine_fetcher(text):
    # Count occurrences of fetchers.
    nfetchers = sum(
        text.count("src = {}".format(fetcher)) for fetcher in FETCHERS.keys()
    )
    if nfetchers == 0:
        raise ValueError("no fetcher.")
    elif nfetchers > 1:
        raise ValueError("multiple fetchers.")
    else:
        # Then we check which fetcher to use.
        for fetcher in FETCHERS.keys():
            if "src = {}".format(fetcher) in text:
                return fetcher


def _determine_extension(text, fetcher):
    """Determine what extension is used in the expression.

    If we use:
    - fetchPypi, we check if format is specified.
    - fetchurl, we determine the extension from the url.
    - fetchFromGitHub we simply use `.tar.gz`.
    """
    if fetcher == "fetchPypi":
        try:
            src_format = _get_unique_value("format", text)
        except ValueError:
            src_format = None  # format was not given

        try:
            extension = _get_unique_value("extension", text)
        except ValueError:
            extension = None  # extension was not given

        if extension is None:
            if src_format is None:
                src_format = "setuptools"
            elif src_format == "other":
                raise ValueError("Don't know how to update a format='other' package.")
            extension = FORMATS[src_format]

    elif fetcher == "fetchurl":
        url = _get_unique_value("url", text)
        extension = os.path.splitext(url)[1]
        if "pypi" not in url:
            raise ValueError("url does not point to PyPI.")

    elif fetcher == "fetchFromGitHub":
        extension = "tar.gz"

    return extension


def _update_package(path, target):
    # Read the expression
    with open(path, "r") as f:
        text = f.read()

    # Determine pname. Many files have more than one pname
    pnames = _get_values("pname", text)

    # Determine version.
    version = _get_unique_value("version", text)

    # First we check how many fetchers are mentioned.
    fetcher = _determine_fetcher(text)

    extension = _determine_extension(text, fetcher)

    # Attempt a fetch using each pname, e.g. backports-zoneinfo vs backports.zoneinfo
    successful_fetch = False
    for pname in pnames:
        # when invoked as an updateScript, UPDATE_NIX_ATTR_PATH will be set
        # this allows us to work with packages which live outside of python-modules
        attr_path = os.environ.get("UPDATE_NIX_ATTR_PATH", f"python3Packages.{pname}")

        if BULK_UPDATE and _skip_bulk_update(attr_path):
            raise ValueError(f"Bulk update skipped for {pname}")
        elif _get_attr_value(f"{attr_path}.cargoDeps") is not None:
            raise ValueError(f"Cargo dependencies are unsupported, skipping {pname}")
        try:
            new_version, new_sha256, prefix = FETCHERS[fetcher](
                attr_path, pname, extension, version, target
            )
            successful_fetch = True
            break
        except ValueError:
            continue

    if not successful_fetch:
        raise ValueError(f"Unable to find correct package using these pnames: {pnames}")

    if new_version == version:
        logging.info("Path {}: no update available for {}.".format(path, pname))
        return False
    elif Version(new_version) <= Version(version):
        raise ValueError("downgrade for {}.".format(pname))
    if not new_sha256:
        raise ValueError("no file available for {}.".format(pname))

    text = _replace_value("version", new_version, text)

    # hashes from pypi are 16-bit encoded sha256's, normalize it to sri to avoid merge conflicts
    # sri hashes have been the default format since nix 2.4+
    sri_hash = _hash_to_sri("sha256", new_sha256)

    # retrieve the old output hash for a more precise match
    if old_hash := _get_attr_value(f"{attr_path}.src.outputHash"):
        # fetchers can specify a sha256, or a sri hash
        try:
            text = _replace_value("hash", sri_hash, text, old_hash)
        except ValueError:
            text = _replace_value("sha256", sri_hash, text, old_hash)
    else:
        raise ValueError(f"Unable to retrieve old hash for {pname}")

    if fetcher == "fetchFromGitHub":
        # in the case of fetchFromGitHub, it's common to see `rev = version;` or `rev = "v${version}";`
        # in which no string value is meant to be substituted. However, we can just overwrite the previous value.
        regex = r"((?:rev|tag)\s+=\s+[^;]*;)"
        regex = re.compile(regex)
        matches = regex.findall(text)
        n = len(matches)

        if n == 0:
            raise ValueError("Unable to find rev value for {}.".format(pname))
        else:
            # forcefully rewrite rev, incase tagging conventions changed for a release
            match = matches[0]
            text = text.replace(match, f'tag = "{prefix}${{version}}";')
            # incase there's no prefix, just rewrite without interpolation
            text = text.replace('"${version}";', "version;")

            # update changelog to reference the src.tag
            if result := re.search("changelog = \"[^\"]+\";", text):
                cl_old = result[0]
                cl_new = re.sub(r"v?\$\{(version|src.rev)\}", "${src.tag}", cl_old)
                text = text.replace(cl_old, cl_new)

    with open(path, "w") as f:
        f.write(text)

        logging.info(
            "Path {}: updated {} from {} to {}".format(
                path, pname, version, new_version
            )
        )

    result = {
        "path": path,
        "target": target,
        "pname": pname,
        "old_version": version,
        "new_version": new_version,
        #'fetcher'       : fetcher,
    }

    return result


def _update(path, target):
    # We need to read and modify a Nix expression.
    if os.path.isdir(path):
        path = os.path.join(path, "default.nix")

    # If a default.nix does not exist, we quit.
    if not os.path.isfile(path):
        logging.info("Path {}: does not exist.".format(path))
        return False

    # If file is not a Nix expression, we quit.
    if not path.endswith(".nix"):
        logging.info("Path {}: does not end with `.nix`.".format(path))
        return False

    try:
        return _update_package(path, target)
    except ValueError as e:
        logging.warning("Path {}: {}".format(path, e))
        return False


def _commit(path, pname, old_version, new_version, pkgs_prefix="python: ", **kwargs):
    """Commit result."""

    msg = f"{pkgs_prefix}{pname}: {old_version} -> {new_version}"

    if changelog := _get_attr_value(f"{pkgs_prefix}{pname}.meta.changelog"):
        msg += f"\n\n{changelog}"

    msg += "\n\nThis commit was automatically generated using update-python-libraries."

    try:
        subprocess.check_call([GIT, "add", path])
        subprocess.check_call([GIT, "commit", "-m", msg])
    except subprocess.CalledProcessError as e:
        subprocess.check_call([GIT, "checkout", path])
        raise subprocess.CalledProcessError(f"Could not commit {path}") from e

    return True


def main():
    epilog = """
environment variables:
  GITHUB_API_TOKEN\tGitHub API token used when updating github packages
    """
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=epilog
    )
    parser.add_argument("package", type=str, nargs="+")
    parser.add_argument("--target", type=str, choices=SEMVER.keys(), default="major")
    parser.add_argument(
        "--commit", action="store_true", help="Create a commit for each package update"
    )
    parser.add_argument(
        "--use-pkgs-prefix",
        action="store_true",
        help="Use python3Packages.${pname}: instead of python: ${pname}: when making commits",
    )

    args = parser.parse_args()
    target = args.target

    packages = list(map(os.path.abspath, args.package))

    if len(packages) > 1:
        global BULK_UPDATE
        BULK_UPDATE = True

    logging.info("Updating packages...")

    # Use threads to update packages concurrently
    with Pool() as p:
        results = list(filter(bool, p.map(lambda pkg: _update(pkg, target), packages)))

    logging.info("Finished updating packages.")

    commit_options = {}
    if args.use_pkgs_prefix:
        logging.info("Using python3Packages. prefix for commits")
        commit_options["pkgs_prefix"] = "python3Packages."

    # Commits are created sequentially.
    if args.commit:
        logging.info("Committing updates...")
        # list forces evaluation
        list(map(lambda x: _commit(**x, **commit_options), results))
        logging.info("Finished committing updates")

    count = len(results)
    logging.info("{} package(s) updated".format(count))


if __name__ == "__main__":
    main()
