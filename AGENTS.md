# Notes for coding agents

## Releases: the bootstrapper, Automator and the DBMoto converter ship separately

This repository releases three products, each from its own tag on `main`:

| Product | Tag | What the tag pipeline does |
|---|---|---|
| Bootstrapper | `tag-vX.Y.Z` (annotated, `beta-*` for a beta) | Linux and Windows images, `deploy`, sets `latestVersionGA` in the portal agent catalog |
| Automator | `automator-X.Y.Z` | builds, signs and publishes `gluesync-automator-linux` and `gluesync-automator-windows.exe` |
| Syniti Replicate (DBMoto) metadata converter (`dbmoto-converter/`) | `dbmoto-vX.Y.Z` | deploys the converter API to AWS Lambda (`deploy_lambda`) and to the Azure mirror |

## Every release gets release notes in the Gluesync docs

Each bootstrapper, Automator or DBMoto converter release must have its release notes pushed to the Gluesync docs.
A release is not done until they are. The docs live in
[gluesync-docs-contents](https://gitlab.com/molo17srl/products/gluesync/gluesync-docs-contents) (default branch
`master`), on the current docs version's pages (today `src/gluesync/v2.3/modules/gs-modules/pages/`):

| Product | Page |
|---|---|
| Bootstrapper | `bootstrapper.adoc` |
| Automator | `automator.adoc` |
| DBMoto converter | `convert-db-moto-metadata-xml.adoc` |

- Add one `=== X.Y.Z` section per version, newest first, above the previous one, with `Released: _Month D, YYYY_` and
  one bullet per user-visible change. An Automator release ends with `* Embeds Bootstrapper X.Y.Z;`.
- Follow the tone in that repository's `.cursor/rules/release-notes-tone-of-voice.mdc`: describe what changes for the
  user, not the code.
- If earlier releases are missing from the page, add them too. The docs must not skip a version.
- Push on a `docs/<product>-<version>` branch and open a merge request to `master`. Several products released together
  can share one branch and MR (e.g. `docs/bootstrapper-2.8.26-automator-1.4.32`).
- Write the notes from the merge requests in the release (`git log --merges <previous tag>..<new tag>`), not from the
  tag message alone.

Automator is not only `automator_app/`. It imports the bootstrapper's own modules and bundles them in its binaries:
`create_all_entities`, `export_template_from_corehub`, `courier_*`, `commons`, `gs1_import`, `import_plan`,
`import_options`, `function_collection`, `create_user_defined_functions`, `fan_in`, `ai_settings`, `agent_overrides`,
and what they import in turn. So a bootstrapper fix in one of those modules does not reach Automator users until an
`automator-*` tag is released.

**Rule.** When you change bootstrapper code, or release a bootstrapper version, check whether the change also affects
Automator. If it does, tell the user and propose releasing Automator too. Do not push an `automator-*` tag without the
user's go-ahead.

How to check:

```bash
git fetch --tags origin
LAST=$(git tag -l 'automator-[0-9]*' --sort=-creatordate | head -1)
git diff --stat "$LAST" origin/main -- '*.py' utils agents.json 'automator*.spec' gluesync-automator-linux.spec \
  ':!integration-tests' ':!tests' ':!dbmoto-converter' ':!scripts'
```

Anything listed there besides tests is shipped by the next Automator release. Name the changes (with their MRs) when
you propose it, and propose the next patch version after `$LAST`.
