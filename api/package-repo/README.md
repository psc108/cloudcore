# Host-level package repo — expensive to rebuild

Everything else in this directory is gitignored, host-local build output —
not source, and not cheap to regenerate like `api/images/`, `api/instances/`,
or `api/volumes/`. A rebuild needs a live throwaway CloudCore instance, a
full `apt-get` pass against the real Ubuntu archive plus the Adoptium and
Kismet third-party repos, and several GB of real downloads (the Wikipedia
ZIM artifact alone is ~2.2GB). Expect 15-20+ minutes and real bandwidth
each time.

This file is the one thing in here that's actually tracked by git (see
`.gitignore`'s `api/package-repo/*` / `!api/package-repo/README.md` pair) —
specifically so it survives a `git clean -xfd` and leaves a marker behind
explaining what used to be here and how to get it back, instead of the
directory just silently going empty.

Rebuild with:

    CLOUDCORE_API_URL=http://127.0.0.1:8080 CLOUDCORE_API_TOKEN=dev-token \
      bash api/build-package-repo.sh jammy

Served by the `cloudcore-package-repo` systemd service
(`api/setup-package-repo.sh` installs it) at `http://192.168.100.1:8090/`.
Do not run `api/teardown-network.sh` while this service is active without
`--force` — it will silently cut every guest off from the repo.
