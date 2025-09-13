# Copyright Celeritas contributors: see top-level COPYRIGHT file for details
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""
Cache API calls from GitHub.
"""

import json
import atexit
import hashlib
import requests

from os import environ
from pathlib import Path
from typing import Any, Dict, Tuple, Callable, Optional

from ghapi.all import GhApi

from fastcore.xtras import obj2dict


def _load_ghapi_token():
    try:
        return environ["GHAPI_TOKEN"]
    except KeyError:
        pass
    try:
        with open(Path.home() / ".config/ghapi-celeritas-token") as f:
            return f.read().strip()
    except Exception as e:
        print(f"Failed to load token: {e}")

    return None


DEFAULTS = dict(
    owner="celeritas-project",
    repo="celeritas",
    token=_load_ghapi_token(),
)


class GhApiCache:
    def __init__(
        self,
        *,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
        token: Optional[str] = None,
        cache_file: Optional[Path] = None,
        **kwargs: Any,
    ) -> None:
        """
        Initializes the cache wrapper around a ghapi instance with
        class methods for specific API endpoints.
        Lazily accesses DEFAULTS at instantiation time.
        """
        owner = owner if owner is not None else DEFAULTS["owner"]
        repo = repo if repo is not None else DEFAULTS["repo"]
        token = token if token is not None else DEFAULTS["token"]
        if cache_file is None:
            cache_file = Path(f"data/ghapicache-{owner}-{repo}.json")
        self.cache_file = cache_file

        self.api = GhApi(owner=owner, repo=repo, token=token, **kwargs)
        self.owner: str = owner or ""
        self.repo: str = repo

        # Create downloads directory
        self.downloads_dir: Path = self.cache_file.parent / "ghapicache-downloads"
        self.downloads_dir.mkdir(parents=True, exist_ok=True)

        self.cache: Dict[str, Dict[str, Any]] = {}
        self.dirty = False
        self._load_cache()

        # Ensure files cache exists
        if "files" not in self.cache:
            self.cache["files"] = {}
            self.dirty = True

        atexit.register(self.flush)

    def issue(self, issue_id: int) -> Any:
        return self._cached_request("issue", self.api.issues.get, issue_id)
    
    def pull(self, pr_id: int) -> Any:
        return self._cached_request("pull", self.api.pulls.get, pr_id)

    def reviews(self, pr_id: int) -> Any:
        return self._cached_request("reviews", self.api.pulls.list_reviews, pr_id)

    def user(self, username: str) -> Any:
        return self._cached_request("user", self.api.users.get_by_username, username)

    def team(self, team: str, org: Optional[str] = None) -> Any:
        if org is None:
            org = self.owner
        return self._cached_request(
            "team", self.api, f"/orgs/{org}/teams/{team}/members"
        )

    def labels(
        self, prid: int, owner: Optional[str] = None, repo: Optional[str] = None
    ) -> Any:
        if owner is None:
            owner = self.owner
        if repo is None:
            repo = self.repo
        return self._cached_request(
            "labels", self.api, f"/repos/{owner}/{repo}/issues/{prid}/labels"
        )

    def org_members(self, org: Optional[str] = None) -> Any:
        if org is None:
            org = self.owner
        return self._cached_request("org_members", self.api.orgs.list_members, org)

    def teams(self, org: Optional[str] = None) -> Any:
        if org is None:
            org = self.owner
        return self._cached_request("team", self.api.teams.list, org)

    def download_file(self, url: str, content_type: Optional[str] = None, ext: Optional[str] = None) -> Path:
        """
        Downloads a file from the given URL and caches it by content hash in the
        ghapicache-downloads directory. Maps URL to content hash in the cache.

        Args:
            url: The URL to download from
            ext: Optional file extension (if not provided, will try to extract from URL)

        Returns:
            Path to the cached file
        """
        assert url is not None
        # Check if URL is already in our files cache
        if filename := self.cache["files"].get(url):
            file_path = self.downloads_dir / filename
            if file_path.exists():
                # File exists in cache, return it
                print(f"Loading {url} from cached file {file_path}")
                with open(file_path, "rb") as f:
                    return f.read()

        # File not in cache or cache entry invalid, download it
        print(f"Downloading {url}")
        headers = {}
        if content_type is not None:
            headers["Accept"] = content_type
        r = requests.get(url, headers=headers)
        r.raise_for_status()
        self.cache_file_to_url(r.content, url, ext)
        return r.content

    def cache_file_to_url(self, content: bytes, url: str, ext: Optional[str] = None) -> bytes:
        if url in self.cache["files"]:
            content_hash = self.cache["files"][url]
        else:
            content_hash = hashlib.sha1(content).hexdigest()

        # Determine file extension
        if ext is None:
            # Try to extract extension from URL
            path = Path(url.partition("?")[0])  # Remove query params
            ext = path.suffix or ""
        elif ext:
            # Get just the final extension
            ext = "." + ext.split(".")[-1]

        # Create filename from content hash
        filename = f"{content_hash}{ext}"
        file_path = self.downloads_dir / filename

        # Save the file
        if not file_path.exists():
            with open(file_path, "wb") as f:
                f.write(content)

        # Update the cache
        self.cache["files"][url] = filename
        self.dirty = True

        return content

    def purge(self):
        """Clear the cache and delete the cache file."""
        self.cache = {}
        self.dirty = False
        try:
            self.cache_file.unlink()
        except Exception as e:
            print("Failed to delete cache file:", e)
        else:
            print("Deleted cache file:", self.cache_file)

    def flush(self):
        if not self.dirty:
            return
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_file, "w") as f:
                json.dump(self.cache, f)
        except Exception as e:
            print("Failed to save cache:", e)
        else:
            print("Saved cache to:", self.cache_file)
            self.dirty = False

    def _load_cache(self):
        try:
            with open(self.cache_file, "r") as f:
                self.cache = json.load(f)
        except Exception as e:
            self.cache = {}
            print("Failed to load cache:", e)

    def subkey(self, *args, **kwargs) -> str:
        try:
            key_data = (args, sorted(kwargs.items()))
            return json.dumps(key_data, sort_keys=True, default=str)
        except Exception:
            return str((args, kwargs))

    def _cached_request(self, category: str, func: Callable, *args, **kwargs) -> Any:
        # Ensure the category exists in the cache, then use a local variable for performance.
        cat_cache = self.cache.setdefault(category, {})
        subkey = self.subkey(*args, **kwargs)
        try:
            response = cat_cache[subkey]
        except KeyError:
            cat_cache[subkey] = response = obj2dict(func(*args, **kwargs))
            self.dirty = True
        return response

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.flush()

