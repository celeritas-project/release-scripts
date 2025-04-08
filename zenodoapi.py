# Copyright Celeritas contributors: see top-level COPYRIGHT file for details
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""
Classes for interacting with Zenodo API and managing depositions.
"""

import requests
from typing import Dict, Any, List, Optional, Callable


class Zenodo:
    """Main client for interacting with the Zenodo REST API."""

    # Default to the sandbox API for testing
    api_url = "https://sandbox.zenodo.org/api"

    def __init__(self, access_token: str):
        """Initialize with Zenodo access token.

        Args:
            access_token: The Zenodo API access token string
            token_path: Path to a file containing the token (used if access_token not provided)
        """
        self.params = {"access_token": access_token}

    def request(
        self, func: Callable, *args, params: Optional[dict] = None, **kwargs
    ) -> Any:
        """Make a request to the Zenodo API.

        Args:
            func: Function to call
            *args: Positional arguments for the function
            **kwargs: Keyword arguments for the function

        Returns:
            Response data
        """
        if params is not None:
            params = params.copy()
            params.update(self.params)
        else:
            params = self.params
        r = func(*args, params=params, **kwargs)
        r.raise_for_status()
        return r.json()

    def create_deposition(
        self, metadata: Dict[str, Any], community=None
    ) -> "ZenodoDeposition":
        """Create a new deposition with the given metadata.

        Args:
            metadata: Metadata for the deposition

        Returns:
            ZenodoDeposition object wrapping the new deposition
        """
        deposition_data = self.request(
            requests.post,
            f"{self.api_url}/deposit/depositions",
            json={"metadata": metadata},
        )
        print(
            "Created deposition {id} at {links[html]} : {metadata[title]}".format(
                **deposition_data
            )
        )
        return ZenodoDeposition(self, deposition_data)

    def get_deposition(self, id) -> "ZenodoDeposition":
        deposition_data = self.request(
            requests.get,
            f"{self.api_url}/deposit/depositions/{id}",
        )
        return ZenodoDeposition(self, deposition_data)

    def find_deposition(self, title) -> "ZenodoDeposition":
        """Find a deposition by title.

        Args:
            release_md: Release metadata object with a 'release' attribute

        Returns:
            ZenodoDeposition object if found, None if not found

        Raises:
            ValueError: If multiple matches found
        """
        depositions = self.request(
            requests.get,
            f"{self.api_url}/deposit/depositions",
            params={"q": title, "page": 1, "size": 100},
        )

        if len(depositions) == 0:
            return

        # Check for exact match
        exact = [d for d in depositions if d["metadata"]["title"] == title]
        if exact:
            # Bring me the sword of exact zero
            return ZenodoDeposition(self, exact[0])

        print(f"No exact match for {title}: found titles:")
        for d in depositions:
            print(f"- {d['metadata']['title']}: {d['links']['html']}")

        return

    def get_licenses(self) -> Dict[str, Dict[str, Any]]:
        """Get available license information.

        Returns:
            Dictionary mapping license IDs to license details
        """
        hits = self.request(requests.get, f"{self.api_url}/licenses/")
        # TODO: need to page through these; only the first few are returned
        license_data = hits["hits"]["hits"]
        return {item["id"]: item for item in license_data}


class ZenodoFile:
    """Class for interacting with a specific Zenodo deposition."""

    def __init__(self, client: Zenodo, data: Dict[str, Any]):
        """Initialize with a Zenodo client and deposition data.

        Args:
            client: Zenodo API client instance
            data: Deposition data from API
        """
        self.client = client
        self.data = data

    @property
    def filename(self) -> str:
        """Get the filename of this file."""
        return self.data["filename"]

    def delete(self) -> None:
        """Delete this file from the deposition."""
        self.client.request(
            requests.delete,
            f"{self.data['links']['self']}",
        )
        print(f"Deleted {self.filename} from {self.data['links']['bucket']}")

    def __repr__(self) -> str:
        """Return string representation of this file."""
        return f'ZenodoFile(filename="{self.filename}")'


class ZenodoDeposition:
    """Class for interacting with a specific Zenodo deposition."""

    def __init__(self, client: Zenodo, data: Dict[str, Any]):
        """Initialize with a Zenodo client and deposition data.

        Args:
            client: Zenodo API client instance
            data: Deposition data from API
        """
        self.client = client
        self.data = data
        self.id = data["id"]

    def __repr__(self) -> str:
        """Return string representation of this deposition."""
        return (
            f'ZenodoDeposition(id={self.id}, title="{self.data["metadata"]["title"]}")'
        )

    def upload(self, content: bytes, name: str) -> Dict[str, Any]:
        """Upload an artifact to this deposition."""
        try:
            bucket_url = self.links["bucket"]
        except KeyError:
            # Try old file API
            uploaded = self.client.request(
                requests.post,
                f"{self.links['self']}/files",
                data={"name": name},
                files={"file": content},
            )
            print("Uploaded", uploaded)
        else:
            uploaded = self.client.request(
                requests.put, f"{bucket_url}/{name}", data=content
            )
            print(f"Uploaded {uploaded['key']}: version {uploaded['version_id']}")
        return uploaded

    def get_files(self) -> List[ZenodoFile]:
        """Get all files in this deposition.

        Returns:
            List of ZenodoFile objects
        """
        files = self.data.get("files")
        if not files:
            files = self.client.request(requests.get, self.links["files"])
        if not files:
            return []
        return [ZenodoFile(self.client, f) for f in files]

    def get_latest_draft(self) -> "ZenodoDeposition":
        """Get the latest draft of this deposition.

        Returns:
            New ZenodoDeposition object or none if not in draft
        """
        latest_draft = self.client.request(requests.get, self.links["latest_draft"])
        return ZenodoDeposition(self.client, latest_draft)

    def get_latest_version(self) -> "ZenodoDeposition":
        """Get the latest version of this deposition."""
        vers = self.client.request(requests.get, self.links["latest"])
        return ZenodoDeposition(self.client, vers)

    def create_new_version(self) -> "ZenodoDeposition":
        """Create a new version of this deposition.

        Returns:
            New ZenodoDeposition object
        """
        if self.data["state"] == "draft":
            return None
        # NOTE: even though the
        result = self.client.request(requests.post, self.links["newversion"], json={})
        print(f"Created new version {result['id']}: {result['links']['html']}")
        return ZenodoDeposition(self.client, result)

    def refresh(self) -> None:
        """Reload this deposition's metadata."""
        if self.data["state"] == "draft":
            self.data = self.links["self"]
        else:
            self.data = self.client.get_deposition(self.id).data
        return self

    def update(self, metadata: Dict[str, Any]) -> None:
        """Update this deposition's metadata.

        Args:
            metadata: New metadata
        """
        self.data = self.client.request(
            requests.put,
            self.links["self"],
            json={"metadata": metadata},
        )
        print(f"Updated deposition at {self.html} : {self.data['metadata']['title']}")

    @property
    def links(self) -> dict:
        """Get the HTML link for this deposition."""
        return self.data["links"]

    @property
    def html(self) -> str:
        """Get the HTML link for this deposition."""
        return self.data["links"]["html"]

    @property
    def md(self) -> str:
        """Get the metadata for this deposition."""
        return self.data["metadata"]
