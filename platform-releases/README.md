# Release Manifests

Each Platform Release has one Release Manifest here, `<Platform Version>.json`, tagged
`platform-release/<Platform Version>`. `dp-instance upgrade --to <Platform Version>` reads it.
A published manifest and its tag never change.

| Field | Meaning |
| --- | --- |
| `platform_version` | The Platform Version, `YYYY.N` or `YYYY.N.P`. |
| `dp_instance` | `minimum`, the oldest `dp-instance` version this release runs; an older one stops with the exact install command. |
| `dp_instance_commit` | The `dp-instance` commit that an older `dp-instance`, one that reads no `dp_instance`, installs. |
| `sources` | Each template the release covers and its exact release commit. The template's release notes are in that commit, `docs/releases/<Platform Version>.md`. |
| `order` | The repositories in the order they run. An instance without a repository skips it. |

Every Platform Version is a required stop; fix releases never are. An instance moves to the
next Platform Version from any release of its current one.
