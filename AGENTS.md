# Notes for coding agents

## Releases: the bootstrapper and Automator ship separately

This repository releases two products, each from its own tag on `main`:

| Product | Tag | What the tag pipeline does |
|---|---|---|
| Bootstrapper | `tag-vX.Y.Z` (annotated, `beta-*` for a beta) | Linux and Windows images, `deploy`, sets `latestVersionGA` in the portal agent catalog |
| Automator | `automator-X.Y.Z` | builds, signs and publishes `gluesync-automator-linux` and `gluesync-automator-windows.exe` |

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
