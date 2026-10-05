# filepath: release_helpers.py
"""
Helper functions and classes for generating release notes and managing GitHub releases.
"""

import hashlib
import io
import json
import re
import subprocess
from collections import defaultdict, namedtuple
from dataclasses import dataclass, field
from datetime import datetime
from os import environ
from pathlib import Path, PurePath
from typing import Iterable, Optional, Union

from ghapi.all import gh2date
from markdown import markdown
from uritemplate import URITemplate

import githelpers as ghelp
from ghapicache import GhApiCache

# Define namedtuples for data structures
UserInfo = namedtuple("UserInfo", ["name", "institute", "email", "orcid", "github"])
Team = namedtuple("Team", ["description", "members"])
Contributions = namedtuple("Contributions", ["author", "reviewer"])


# Constants for PR categories
CATEGORIES = {
    "enhancement": "New features",
    "bug": "Bug fixes",
    "documentation": "Documentation improvements",
    "minor": "Minor internal changes",
    "removal": "Deprecation and removal",
}

# Team role mapping for Zenodo
TEAM_ROLES = {
    "code-lead": "ProjectManager",
    "core-advisor": "ProjectLeader",
    "core": "ProjectMember",
}


def _load_jsonl(json_path: Path) -> list[dict]:
    result = []
    if not json_path.exists():
        return result

    with open(json_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            result.append(json.loads(line))
    return result


def _save_jsonl(arr: Iterable[dict], json_path: Path):
    with open(json_path, "w") as f:
        for record in arr:
            json.dump(record, f)
            f.write("\n")


class UserCache:
    """Cache for user information from GitHub and local metadata."""

    def __init__(self, api_cache: GhApiCache, json_path: Path):
        """
        Initialize user cache from a JSONL file.

        Args:
            json_path: Path to JSONL file containing user metadata
        """
        self.json_path = json_path
        self._api_cache = api_cache
        self._metadata: dict[str, dict] = {}

        for user_md in _load_jsonl(json_path):
            username = user_md.get("user")
            if not username:
                print("Missing user:", user_md)
                continue
            self._metadata[username] = user_md
        self._cache: dict[str, UserInfo] = {}

    def _load_user_info(self, username: str) -> UserInfo:
        """Load user info from GitHub and local metadata."""
        if not isinstance(username, str):
            raise TypeError(f"user type must be a string: got {username!r}")
        u = self._api_cache.user(username)
        user_md = self._metadata.get(username, {})
        return UserInfo(
            name=user_md.get("name") or u["name"],
            email=u["email"],
            institute=user_md.get("institute"),
            orcid=user_md.get("orcid"),
            github=username,
        )

    def __getitem__(self, username: str) -> UserInfo:
        """Get user info, loading from API if not cached."""
        if username not in self._cache:
            self._cache[username] = self._load_user_info(username)
        return self._cache[username]

    def _user_record(self, username: str) -> dict:
        try:
            user_info = self._cache[username]
        except KeyError:
            return self._metadata[username]
        return {
            "user": username,
            "name": user_info.name,
            "orcid": user_info.orcid,
            "institute": user_info.institute,
        }

    @property
    def users(self):
        return self._metadata.keys()

    def flush(self):
        """Write cached user metadata to disk as JSONL, omitting private fields."""
        _save_jsonl(
            (
                self._user_record(u)
                for u in sorted(set(self._cache) | set(self._metadata), key=lambda s: s.lower())
            ),
            self.json_path,
        )


def get_team(cached: GhApiCache, team_md: dict):
    members = frozenset(u["login"] for u in cached.team(team_md["slug"]))
    return Team(description=team_md["description"], members=members)


def get_last_name(user_info):
    """Extract last name from user info for sorting."""
    if name := user_info.name:
        return name.split(" ")[-1]
    return ""


def format_user(user_info):
    """Format user info for display."""
    return f"{user_info.name} *({user_info.institute})*"


class PullRequestLogParser:
    RE_SUBJECT_PR_SQUASH = re.compile(r"\(#([\d]+)\)$")
    RE_SUBJECT_PR_MERGE = re.compile(r"^Merge pull request #([\d]+)")

    def __init__(self, shamap: Path | None):
        if shamap:
            shamap_data = json.loads(shamap.read_text())
            self.replace_sha = shamap_data["replace"]
            self.ignore_sha = shamap_data["ignore"]
        else:
            self.replace_sha = {}
            self.ignore_sha = {}

    def subject_to_pr(self, subject: str) -> int:
        for expr in [self.RE_SUBJECT_PR_SQUASH, self.RE_SUBJECT_PR_MERGE]:
            if match := expr.search(subject):
                return int(match.group(1))
            
        raise ValueError(f"Can't match log subject to PR: {subject!r}")
            
    def __call__(self, sha: str, subject: str):
        """Extract PR numbers from git log output."""
        try:
            return self.replace_sha[sha]
        except KeyError:
            pass

        try:
            why = self.ignore_sha[sha]
        except KeyError:
            pass
        else:
            raise ValueError(f"Ignore {subject!r} ({sha}): {why}")
        
        try:
            pr = self.subject_to_pr(subject)
        except ValueError as e:
            raise ValueError(f"Failed to parse {sha}: {e}") from e
        
        return pr

def list_contributors(prev_release, new_release, author_map=None):
    """List contributors between two releases.

    Args:
        prev_release: Previous release tag
        new_release: New release tag
        author_map: Optional mapping to normalize author names

    Returns:
        List of (author, count) tuples sorted by count
    """
    if author_map is None:
        author_map = {}

    all_contributors = ghelp.git(
        "log", "--pretty=format:%an", f"{prev_release}..{new_release}"
    )

    author_count = defaultdict(int)
    for auth in all_contributors:
        try:
            # Update from manual author list
            auth = author_map[auth]
        except KeyError:
            pass
        author_count[auth] += 1

    return sorted(author_count.items(), key=lambda kv: kv[1], reverse=True)


@dataclass
class ReleaseMetadata:
    """Metadata for a release."""

    release: str
    merge_bases: list = field(default_factory=list)
    target_branch: str = "develop"

    def as_version(self) -> tuple | None:
        """Convert release string to a version tuple."""
        if self.release is None:
            return None

        match = re.fullmatch(
            r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-.*)",
            self.release,
        )
        if match is None:
            return None

        return tuple(int(match.group(i + 1)) for i in range(3))

    def is_major(self):
        """Check if the release is a major version."""
        version = self.as_version()
        if version is None:
            return False
        if version[0] == 0:
            # Strip leading zero for dev version
            version = version[1:]

        return all(x == 0 for x in version[1:])


def create_local_tag(release_md: ReleaseMetadata):
    """As part of the release process, create a local tag and update the given release object."""
    release_sha = ghelp.git_rev_parse(release_md.target_branch)
    print(f"Updating release target from {release_md.target_branch} to {release_sha}")
    ghelp.git("tag", "-f", "v" + release_md.release, release_sha)
    release_md.target_branch = release_sha


class PullRequestRange:
    """Handle a range of pull requests for a release."""

    def __init__(self, metadata: ReleaseMetadata, parser: PullRequestLogParser):
        """Initialize with release metadata."""
        self.metadata = metadata
        self.pull_ids = self._compute_pull_ids(parser)

    def _compute_pull_ids(self, parse_pr: PullRequestLogParser):
        """Compute the list of pull request IDs for this release."""
        # Compute merge bases and exclude IDs
        exclude_ids = set()
        for ref in self.metadata.merge_bases:
            mb = ghelp.git_merge_base(self.metadata.target_branch, ref)
            for (sha, subj) in ghelp.git_log_subjects(mb, ref):
                try:
                    pr_id = parse_pr(sha, subj)
                except ValueError as e:
                    print(e)
                else:
                    exclude_ids.add(pr_id)

        # Get pull IDs
        first_merge_base = ghelp.git_merge_base(
            self.metadata.target_branch, self.metadata.merge_bases[0]
        )

        pull_ids = []
        for (sha, subj) in ghelp.git_log_subjects(first_merge_base, self.metadata.target_branch, reverse=True):
            try:
                pr_id = parse_pr(sha, subj)
            except ValueError as e:
                print(e)
            else:
                if pr_id not in exclude_ids:
                    pull_ids.append(pr_id)
        return pull_ids


class ContributionCounter:
    """Count contributions (authoring and reviewing) from pull requests."""

    def __init__(self, cached_api):
        """Initialize with a cached GitHub API instance."""
        self.cached_api = cached_api
        self.author_count = defaultdict(int)
        self.reviewer_count = defaultdict(int)

    def __call__(self, pr_id):
        """Process a single pull request and update contribution counts."""
        p, reviews = self.cached_api.pull(pr_id), self.cached_api.reviews(pr_id)

        # Count author contribution
        author = p["user"]["login"]
        self.author_count[author] += 1

        # Count reviewers
        pull_reviewers = {
            r["user"]["login"] for r in reviews if r["state"] == "APPROVED"
        }
        pull_reviewers.discard(author)  # Remove author if they reviewed their own PR

        for reviewer in pull_reviewers:
            self.reviewer_count[reviewer] += 1

        return self

    def sorted(self):
        """Return sorted Contributions namedtuple."""

        def sorted_count(d):
            return dict(sorted(d.items(), key=lambda item: item[1], reverse=True))

        return Contributions(
            author=sorted_count(self.author_count),
            reviewer=sorted_count(self.reviewer_count),
        )


def markdown_to_rst(text):
    return re.sub(r"`([^`]+)`(\S)", r"``\1`` \2", text)


class SortedPulls:
    def __init__(self, cached_api: GhApiCache):
        self.pulls = defaultdict(list)
        self.cached = cached_api

    def add(self, pr_id):
        p = self.cached.pull(pr_id)
        author = p["user"]["login"]
        labels = {lab["name"] for lab in p["labels"]}

        summary = {
            "id": pr_id,
            "title": markdown_to_rst(p["title"]),
            "labels": labels,
            "author": author,
            "merged_at": gh2date(p["merged_at"]),
            "sha": p["merge_commit_sha"][:8],
        }

        for lab in CATEGORIES:
            if lab in labels:
                self.pulls[lab].append(summary)
                return

        raise ValueError(
            "Missing label: #{} ({}): {}".format(
                pr_id, p["title"], labels or "(no labels)"
            )
        )

    def __getitem__(self, key):
        return self.pulls[key]

    def __setitem__(self, key, value):
        self.pulls[key] = value

    def items(self):
        return self.pulls.items()

    def keys(self):
        return self.pulls.keys()

    def values(self):
        return self.pulls.values()


class ReleaseNotes:
    """Generate release notes."""

    def __init__(self, release):
        """Initialize with release metadata and body text."""
        format_fill = vars(release)
        format_fill["today"] = datetime.now()
        format_fill["merge_base"] = release.merge_bases[0]
        self.format_fill = format_fill
        self.notes: list = []

    def title(self, title: str, level=2):
        """Add a title to the notes."""
        self.notes.extend(self.make_title(title, level))
        self.notes.append("")  # Add a blank line after the title

    def itemize(self, iterable, bullet="*"):
        """Add a list of items as bullet points to the notes."""
        self.notes.extend(f"{bullet} {item}" for item in iterable)
        self.notes.append("")

    def sorted_pulls(self, pulls: SortedPulls):
        for lab, title in CATEGORIES.items():
            prs = pulls[lab]
            if not prs:
                continue
            self.title(title)
            self.itemize("{title} *(@{author}, #{id})*".format(**pr) for pr in prs)

    def reviewers(self, reviewers: dict, user_cache: UserCache):
        """Add a list of reviewers to the notes."""
        self.title("Reviewers")
        items = []
        for login, count in reviewers.items():
            ui = user_cache[login]
            items.append(f"{ui.name} *(@{login})*: {count}")
        self.itemize(items)

    def changelog_line(self, project, code):
        """Add a link to the full changelog on GitHub."""
        self.notes.append(
            f"**Full Changelog**: https://github.com/{project}/{code}/compare/"
            + "{merge_base}...v{release}"
        )

    def write(self, fp: io.TextIOWrapper):
        """Write notes to a file-like object."""
        ff = self.format_fill
        fp.writelines(line.format(**ff) + "\n" for line in self.notes)

    def __str__(self):
        with io.StringIO() as output:
            self.write(output)
            return output.getvalue()


class MarkdownNotes(ReleaseNotes):
    """Generate release notes in Markdown format.

    These are suitable for uploading to GitHub."""

    @staticmethod
    def make_title(title, level=2):
        return ["#" * (level) + " " + title]

    def __init__(self, release, body):
        super().__init__(release)
        self.notes.append(body)

    def paragraph(self, text):
        """Add a paragraph of text to the notes."""
        # Split text into lines if it's too long
        for line in text.splitlines():
            self.notes.append(line.strip())
        self.notes.append("")


class RstNotes(ReleaseNotes):
    """Generate release notes in reStructuredText format.

    These should be written to the release notes RST appendix."""

    @staticmethod
    def make_title(title, level=2):
        """Make RST title with underline.

        Args:
            title: Title text
            level: Heading level (0=major, 1=minor, 2=sub)

        Returns:
            List of lines for the title
        """
        char = "=-^~"[level]  # (Series, version, category, UNUSED)
        return [title, char * len(title)]

    def __init__(self, release, body):
        """Initialize with release metadata and body text."""
        super().__init__(release)

        if release.is_major():
            (major, minor, patch) = release.as_version()
            self.notes += self.make_title(f"Series {major}.{minor}", level=0)
            self.paragraph(
                f"Major development version {major}.{minor} can be referenced at :cite:t:`celeritas-{major}-{minor}`."
            )

        self.notes += [
            ".. _release_v{release}:",
            "",
        ]
        self.notes += self.make_title(f"Version {release.release}", level=1)
        self.notes += [
            "*Released {today:%Y/%m/%d}*",
            body,
        ]

    def paragraph(self, text):
        """Add a paragraph of text to the notes."""
        # Split text into lines if it's too long
        for line in text.splitlines():
            self.notes.append(line.strip())
        self.notes.append("")


def find_release(github_api, version: str):
    """
    Find a GitHub release by version number.

    Args:
        github_api: GitHub API instance
        version: Version string (without 'v' prefix)

    Returns:
        Release object if found, None otherwise
    """
    try:
        releases = github_api.repos.list_releases()
        for release in releases:
            if release["tag_name"] == "v" + version:
                return release
            elif release["name"] == "Version " + version:
                return release
        print(f"No release found for version: {version}")
    except Exception as e:
        print(f"Error fetching releases: {str(e)}")
    return None


def create_release(
    github_api,
    metadata: ReleaseMetadata,
    notes,
):
    """
    Create a GitHub release draft.

    Args:
        metadata: ReleaseMetadata instance
        notes: Release notes text
        github_api: GitHub API instance

    Returns:
        Created release JSON object
    """
    # Create the release using the GitHub API
    release = github_api.repos.create_release(
        tag_name=f"v{metadata.release}",
        target_commitish=metadata.target_branch,
        name=f"Version {metadata.release}",
        body=notes,
        draft=True,
        prerelease=False,
    )
    print(f"Draft release {metadata.release} created: {release['html_url']}")
    return release


Tarball = namedtuple("Tarball", ["name", "url", "content"])


def get_tarball(ghapi_cache: GhApiCache, release: dict):
    assets = release["assets"] or []
    assets = [a for a in assets if a["name"].endswith(".tar.gz")]
    if len(assets) == 1:
        url = assets[0]["browser_download_url"]
        return Tarball(
            assets[0]["name"],
            url,
            ghapi_cache.download_file(url),
        )
    elif len(assets) > 1:
        print("Multiple tarballs found in release assets")
        return None

    print("No tarball found in release assets: reloading from GitHub")
    release = ghapi_cache.api.repos.get_release(release_id=release["id"])
    if release["assets"]:
        # Updating found assets loaded externally or previously
        return get_tarball(ghapi_cache, release)
    print("Still no tarball found in release assets")
    return None


def upload_asset(
    ghapi_cache: GhApiCache,
    release: dict,
    data: Union[bytes, Path],
    name: Optional[str] = None,
):
    if isinstance(data, Path):
        if name is None:
            name = data.name
        data = data.read_bytes()
    assert name is not None

    upload_url_template = URITemplate(release["upload_url"])
    upload_url = upload_url_template.expand(name=name)
    suffix = PurePath(name).suffix
    suffix_map = {
        ".gz": "gzip",
        ".pdf": "pdf",
    }
    content_type = "application/" + suffix_map.get(suffix, "octet-stream")

    uploaded = ghapi_cache.api(
        upload_url,
        verb="post",
        headers={"Content-Type": content_type},
        data=data,
    )
    browser_url = uploaded.get("browser_download_url")
    if not isinstance(browser_url, str):
        raise ValueError("failed to upload", uploaded)
    print(f"Uploaded artifact: {browser_url}")
    ghapi_cache.cache_file_to_url(data, browser_url, ext=suffix)
    return Tarball(name=name, url=browser_url, content=data)


def prepare_spack_update(project: str, release_md: ReleaseMetadata, tgz_data: bytes):
    sha256_hash = hashlib.sha256(tgz_data).hexdigest()
    with ghelp.open_pbcopy() as pb:
        pb.write(f'version("{release_md.release}", sha256="{sha256_hash}")\n')
    print("Spack version copied to clipboard!")
    spack = Path(environ["SPACK_ROOT"]) / "bin" / "spack"
    package_dir = Path(
        subprocess.check_output([spack, "location", "-p", project]).decode().strip()
    )
    subprocess.check_call(["open", package_dir / "package.py"])
    return sha256_hash


def ZenodoContribBuilder(ucache: UserCache):
    def make_zenodo_contributor(username, role=None):
        user_info = ucache[username]
        if not user_info.name:
            raise ValueError(f"Missing properties from user {username}: {user_info}")
        contributor_data = {
            "name": user_info.name,
            "affiliation": user_info.institute,
            "orcid": user_info.orcid,
        }
        if role:
            contributor_data["type"] = role
        return contributor_data

    return make_zenodo_contributor


class ZenodoMetadataBuilder:
    def __init__(
        self,
        user_cache: UserCache,
        teams: dict,
        ghapi_cache: Optional[GhApiCache] = None,
    ):
        """
        Initialize the Zenodo metadata generator.

        Args:
            user_cache: Instance of UserCache for user information.
            teams: Dictionary of team metadata.
        """
        self.user_cache = user_cache
        self.teams = teams
        if ghapi_cache is None:
            repo_url = None
            community = None
        else:
            (proj, repo) = (ghapi_cache.owner, ghapi_cache.repo)
            repo_url = f"https://github.com/{proj}/{repo}"
            community = proj
        self.repo_url = repo_url
        self.community = community

        self.make_zcontrib = ZenodoContribBuilder(user_cache)

    def __call__(self, contrib, release_md, gh_release=None):
        """
        Generate Zenodo metadata for a release.

        Args:
            contrib: Contributions object containing authors and reviewers.
            release_md: ReleaseMetadata object for the release.
            gh_release: Optional GitHub release object.

        Returns:
            Dictionary containing Zenodo metadata.
        """
        creators = [self.make_zcontrib(username) for username in contrib.author]

        # Generate contributors list (for non-authors): first, reviewers
        contributors = [
            self.make_zcontrib(username, role="Editor")
            for username in contrib.reviewer.keys()
        ]

        # Finally give credit to active team members
        for team, role in TEAM_ROLES.items():
            for username in self.teams[team].members:
                contributors.append(self.make_zcontrib(username, role))

        result = {
            "upload_type": "software",
            "creators": creators,
            "contributors": contributors,
            "version": release_md.release,
            "imprint_publisher": "Github",
            "license": "apache2.0",  # TODO: doesn't support multiple [, "mit", "cc-by-4.0"],
            "communities": [
                # TODO: community tag seems to be ignored
                {"identifier": self.community}
            ],
            "custom": {
                "code:codeRepository": self.repo_url,
                "code:programmingLanguage": [
                    {"id": "c++", "title": {"en": "C++"}},
                    {"id": "cuda", "title": {"en": "CUDA"}},
                    {"id": "python", "title": {"en": "Python"}},
                ],
            },
        }
        if gh_release is not None:
            result["description"] = markdown(gh_release["body"].replace("\r\n", "\n"))
            result["publication_date"] = (
                gh2date(gh_release["published_at"]).date().isoformat()
            )
        return result
