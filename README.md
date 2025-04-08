# Celeritas release scripts

Automate code releases with GitHub API, Zenodo, Zotero.

These unpolished scripts are used as infrastructure for
[Celeritas](https://github.com/celeritas-project/celeritas), letting us build
and publish releases with thorough release notes and meticulous attribution.


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
