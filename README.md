# Celeritas release scripts

Automate code releases with GitHub API, Zenodo, Zotero.

These unpolished scripts are used as infrastructure for
[Celeritas](https://github.com/celeritas-project/celeritas), letting us build
and publish releases with thorough release notes and meticulous attribution.

## Environment setup

The repository includes a reusable Hatch environment in `pyproject.toml`.
Install [Hatch](https://hatch.pypa.io/) and create the environment with:

```console
python3 -m pip install hatch
hatch env create
```

The environment includes the runtime dependencies and the notebook tooling
used by the release notebooks (`jupyter`, `ipykernel`, `IPython`, and `tqdm`).
To register it as a kernel for VS Code or another Jupyter client, run:

```console
hatch run python -m ipykernel install --user \
    --name celeritas-release-scripts \
    --display-name "Python (Celeritas release scripts)"
```

Then select **Python (Celeritas release scripts)** as the notebook kernel.
The local modules in this repository are imported from the repository root;
run notebooks with this directory as the working directory.

The release workflow also expects:

- `git`, plus a local checkout of the target project;
- `cmake` and `ninja` when building documentation;
- GitHub API access, using `GHAPI_TOKEN` or `~/.config/ghapi-celeritas-token`;
- a Zenodo token in `~/.config/zenodo-token`;
- macOS `pbcopy` for copying generated release notes to the clipboard.

Run the syntax check with:

```console
hatch run check
```

## Zenodo releases

The `all-zenodo-release` notebook is how I constructed all the [Zenodo
releases](https://zenodo.org/communities/celeritas), using major versions as
"concept" releases and using version updates for the patches.

## Git helpers

These are some simple wrappers that use null separators if possible.

## Github API

The `ghapicache` class caches `ghapi` queries in a file for faster retrieval
multiple times (especially useful for debugging) or when offline (yes I've done
some of this work on an airplane...)

## Zenodo API

There's no Zenodo REST API wrapper like there is for github or even zotero.
Their API description is just slightly wrong, and there are a few bugs. This
class simplifies access to Zenodo functionality.

## Release notes

These are the scripts I use to construct release notes from a git commit range.
