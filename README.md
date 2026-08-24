# OpenLogi, packaged for Flathub

An unofficial Flatpak package for [OpenLogi](https://github.com/AprilNEA/OpenLogi),
built from upstream release tarballs with no network access during the build.

**Packaging only. No application code is changed.** The one edit made to the
tarball is a dependency declaration rewritten to name a revision Cargo.lock
already pins, which cargo needs spelled out before it will build offline; see
[pin-git-revs.py](#pin-git-revspy). Application bugs belong
[upstream](https://github.com/AprilNEA/OpenLogi/issues); packaging bugs belong here.

## Why this repo exists separately

[`openlogi-flatpak`](https://github.com/AalmanSadath/openlogi-flatpak) already
packages OpenLogi and publishes it as a signed OSTree repository on Cloudflare
R2. That manifest lets cargo reach the network during the build, which is the
simple thing to do when you own the builder.

Flathub does not allow it:

> There is no network access during the build process. This means using
> `--share=network` inside build-args will not work.

Every difference between the two manifests follows from that one sentence. Rather
than make the working manifest carry Flathub's constraints, the offline build
lives here and is exercised on its own schedule.

| | `openlogi-flatpak` | this repo |
|---|---|---|
| Rust | rustup, downloaded during the build | `org.freedesktop.Sdk.Extension.rust-stable` |
| crates | resolved by cargo over the network | `cargo-sources.json`, declared up front |
| icons | rescaled with ffmpeg at build time | pre-scaled and committed |
| output | signed OSTree repo + release bundles | a Flathub submission |

## Building locally

```sh
flatpak install -y flathub \
  org.freedesktop.Platform//25.08 \
  org.freedesktop.Sdk//25.08 \
  org.freedesktop.Sdk.Extension.rust-stable//25.08 \
  org.freedesktop.Sdk.Extension.llvm20//25.08

flatpak-builder --user --install --force-clean build org.openlogi.OpenLogi.yml
flatpak run org.openlogi.OpenLogi
```

It is a long build. Roughly a thousand crates, and the GPUI stack is the long
pole.

To prove the offline claim rather than assume it, fetch and build in two passes:

```sh
flatpak-builder --user --force-clean --download-only  build org.openlogi.OpenLogi.yml
flatpak-builder --user --force-clean --disable-download build org.openlogi.OpenLogi.yml
```

The second command fails if anything is missing from `cargo-sources.json`, which
is the failure Flathub's builders would hit.

## Regenerating cargo-sources.json

`cargo-sources.json` is generated from the release's `Cargo.lock` and must be
regenerated whenever the version in the manifest moves.
`.github/workflows/update.yml` does it, and proves the result by building
offline; this is the same thing by hand:

```sh
version=v0.7.10
mkdir -p src
curl -fsSL "https://github.com/AprilNEA/OpenLogi/archive/refs/tags/${version}.tar.gz" \
  | tar -xz --strip-components=1 -C src

# Order matters. The generator reads the lock, so the lock has to be pinned
# first or the config it writes describes a branch instead of a commit.
python3 tools/pin-git-revs.py src
python3 tools/flatpak-cargo-generator.py src/Cargo.lock --git-tarballs \
  -o cargo-sources.json
```

`--git-tarballs` is not optional in practice. Without it the git dependencies
become `type: git` sources and flatpak-builder clones each repository in full,
and one of them is `zed-industries/zed`, a monorepo pulled in for the `gpui`
crates. With it they are codeload snapshots instead.

`tools/flatpak-cargo-generator.py` is vendored from
[flatpak-builder-tools](https://github.com/flatpak/flatpak-builder-tools) (MIT)
at the commit recorded in `tools/GENERATOR_COMMIT`, so a regeneration cannot
change behaviour underneath a release without that file changing too.

It carries one local change, kept as `tools/monorepo-package-selection.patch`
so the delta from upstream stays auditable and can be reapplied when the pin
moves. Upstream keys its package map on crate name alone while walking the whole
repository, so in a monorepo the last directory the walk happens to visit wins.
zed contains both `crates/gpui` (the real thing, 0.2.2) and
`tooling/lints/test_fixture/gpui` (a lint fixture, 0.0.0), and the fixture was
what got vendored. Cargo then reported:

    failed to select a version for the requirement `gpui = "*"` (locked to 0.2.2)
    candidate versions found which didn't match: 0.0.0

The patch keys on name *and* version and picks the one Cargo.lock asks for,
failing loudly if no candidate matches. Worth sending upstream.

### pin-git-revs.py

A second wrinkle, and the reason for `tools/pin-git-revs.py`. Upstream declares

    gpui = { git = "https://github.com/zed-industries/zed" }

with no revision, which is a branch-tracking source, and cargo will not use a
vendored replacement for one:

    the source https://github.com/zed-industries/zed requires a lock file to be
    present first before it can be used against vendored source code

The commit is not unknown, Cargo.lock records it in the source fragment. The
script promotes that value into an explicit `rev` in the lock and in every
manifest that declares the dependency, including the vendored copies of
gpui-component and gpui-updater, which depend on gpui the same unpinned way. It
invents nothing; every revision it writes comes from the lock.

The manifest runs it before cargo, and it must also be run before generating
`cargo-sources.json` so the two agree.

## Host setup

Identical to the other package, and unavoidable: a Flatpak cannot write to
`/etc`, so OpenLogi's udev rules have to be installed on the host or no devices
are detected at all. The rules ship inside the application:

```sh
flatpak run --command=cat org.openlogi.OpenLogi \
  /app/share/openlogi/udev/70-openlogi.rules \
  | sudo tee /etc/udev/rules.d/70-openlogi.rules

echo uinput | sudo tee /etc/modules-load.d/openlogi.conf
sudo modprobe uinput
sudo udevadm control --reload-rules
sudo udevadm trigger
```

Solaar, the other Logitech manager on Flathub, carries the same requirement.

## Not submitted yet

Outstanding before this can go to Flathub:

- **Screenshots.** At least one is required and the metainfo has none.
- **Whether a third-party submission is the right route at all.** Flathub rejects
  third-party submissions when upstream distributes an official Flatpak
  elsewhere. Upstream does not today, but
  [AprilNEA/OpenLogi#767](https://github.com/AprilNEA/OpenLogi/pull/767) proposes
  that they do.

## Licence

Packaging files are MIT OR Apache-2.0, matching upstream. `tools/` is MIT, from
flatpak-builder-tools. The application and its brand assets are upstream's.
