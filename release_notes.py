# filepath: release_helpers.py
"""
Helper functions and classes for generating release notes and managing GitHub releases.
"""

import io
import json
from datetime import datetime
from collections import defaultdict, namedtuple
from pathlib import Path
from typing import Dict, Optional
from markdown import markdown
from ghapi.all import gh2date
from dataclasses import dataclass, field
import githelpers as ghelp
from ghapicache import GhApiCache
from uritemplate import URITemplate

# Define namedtuples for data structures
UserInfo = namedtuple("UserInfo", ["name", "institute", "email", "orcid"])
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


class UserCache:
    """Cache for user information from GitHub and local metadata."""

    def __init__(self, api_cache: GhApiCache, json_path: Path):
        """
        Initialize user cache from a JSON file.

        Args:
            json_path: Path to JSON file containing user metadata
        """
        self.json_path = json_path
        with open(json_path) as f:
            user_md = json.load(f)
        self.name_fixup = user_md.get("github_names", {})
        self.institutes = user_md.get("institute", {})
        self.orcid = user_md.get("orcid", {})
        self._api_cache = api_cache
        self._cache: Dict[str, UserInfo] = {}

    def _load_user_info(self, username: str) -> UserInfo:
        """Load user info from GitHub and local metadata."""

        u = self._api_cache.user(username)
        return UserInfo(
            name=self.name_fixup.get(username, u["name"]),
            email=u["email"],
            institute=self.institutes.get(username),
            orcid=self.orcid.get(username),
        )

    def __getitem__(self, username: str) -> UserInfo:
        """Get user info, loading from API if not cached."""
        if username not in self._cache:
            self._cache[username] = self._load_user_info(username)
        return self._cache[username]


def get_team(cached: GhApiCache, team_md: dict):
    members = frozenset(u["login"] for u in cached.team(team_md["slug"]))
    return Team(description=team_md["description"], members=members)


def get_last_name(user_info):
    """Extract last name from user info for sorting."""
    return user_info.name.split(" ")[-1]


def format_user(user_info):
    """Format user info for display."""
    return f"{user_info.name} *({user_info.institute})*"


def parse_log_pulls(logs):
    """Extract PR numbers from git log output."""
    for line in logs:
        pr = ghelp.subject_to_pr(line)
        if pr:
            yield pr


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

    release: Optional[str] = None
    merge_bases: list = field(default_factory=list)
    target_branch: str = "develop"

    @classmethod
    def from_comprehensive_version(cls, major, minor=0, patch=None):
        "This includes all authors since the previous release split off"
        if patch is None:
            # Development release
            prev = f"0.{major}.0"
            major, minor, patch = 0, major, minor
        else:
            prev = f"{major}.{minor}.0"

        release = f"{major}.{minor}.{patch}"
        merge_bases = [f"v{prev}-dev^"]

        return cls(
            release=release, merge_bases=merge_bases, target_branch="v" + release
        )

    def as_version(self):
        """Convert release string to a version tuple."""
        if self.release is None:
            return None
        return tuple(int(x) for x in self.release.split("."))

    def is_major(self):
        """Check if the release is a major version."""
        version = self.as_version()
        if version is None:
            return False
        if version[0] == 0:
            # Strip leading zero for dev version
            version = version[1:]

        return all(x == 0 for x in version[1:])


class PullRequestRange:
    """Handle a range of pull requests for a release."""

    def __init__(self, metadata: ReleaseMetadata):
        """Initialize with release metadata."""
        self.metadata = metadata
        self.exclude_ids = set()
        self.pull_ids = self._compute_pull_ids()

    def _compute_pull_ids(self):
        """Compute the list of pull request IDs for this release."""
        # Compute merge bases and exclude IDs
        exclude_ids = set()
        for ref in self.metadata.merge_bases:
            mb = ghelp.git_merge_base(self.metadata.target_branch, ref)
            exclude_ids.update(parse_log_pulls(ghelp.git_log_subjects(mb, ref)))

        # Get pull IDs
        first_merge_base = ghelp.git_merge_base(
            self.metadata.target_branch, self.metadata.merge_bases[0]
        )
        pull_ids = list(
            parse_log_pulls(
                ghelp.git_log_subjects(first_merge_base, self.metadata.target_branch)
            )
        )
        return [p for p in pull_ids if p not in exclude_ids][::-1]


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
            "title": p["title"].replace("`", "``"),
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
        return [title, char * len(title), ""]

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


def create_release(github_api, metadata: ReleaseMetadata, notes):
    """
    Create a GitHub release.

    Args:
        metadata: ReleaseMetadata instance
        notes: Release notes text
        github_api: GitHub API instance

    Returns:
        Created release object
    """
    # Create the release using the GitHub API
    release = github_api.repos.create_release(
        tag_name=f"v{metadata.release}",
        target_commitish=metadata.target_branch,
        name=f"Version {metadata.release}",
        body=notes,
        draft=True,  # Create as draft first to review
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


def get_or_upload_tarball(ghapi_cache: GhApiCache, release: dict):
    """
    Manage the release artifact:
    - If an artifact is already attached, download it into memory.
    - If no artifact is attached, download the release tarball, upload it as an artifact.

    Args:
        release: GitHub release object

    Returns:
        Tuple of (browser_download_url, artifact_content)
    """
    found = get_tarball(ghapi_cache, release)
    if found:
        print("Found tarball in release assets")
        return found

    # No tarball found, download the release tarball
    # and upload it as an artifact
    print("Downloading release tarball")
    tarball_url = release["tarball_url"]
    tarball_content = ghapi_cache.download_file(tarball_url)

    # Upload the tarball as an artifact
    print("Uploading release tarball")
    upload_url_template = URITemplate(release["upload_url"])
    suffix = ".tar.gz"
    tag_name = release["tag_name"].lstrip("v")
    name = f"{ghapi_cache.repo}-{tag_name}{suffix}"
    upload_url = upload_url_template.expand(name=name)
    content_type = "application/gzip"

    uploaded = ghapi_cache.api(
        upload_url,
        verb="post",
        headers={"Content-Type": content_type},
        data=tarball_content,
    )
    browser_url = uploaded["browser_download_url"]
    print(f"Uploaded artifact: {browser_url}")
    ghapi_cache.cache_file_to_url(tarball_content, browser_url, ext=suffix)
    return Tarball(name=name, url=browser_url, content=tarball_content)


def ZenodoContribBuilder(ucache: UserCache):
    def make_zenodo_contributor(username, role=None):
        user_info = ucache[username]
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
