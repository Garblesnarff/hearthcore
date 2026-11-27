# Upstream Attribution & License Notice (`mod-playerbots-companions/`)

The C++ source files in this directory are derived from **AzerothCore** and **mod-playerbots**
and are distributed under the **GNU General Public License v2.0 (GPL-2.0)** — see [`LICENSE`](./LICENSE)
and [`AUTHORS`](./AUTHORS) in this directory.

## Upstream Projects & Base Commit

- **AzerothCore WotLK**: <https://github.com/azerothcore/azerothcore-wotlk> (AGPL-3.0 / GPL-2.0 compatible core)
- **mod-playerbots**: <https://github.com/mod-playerbots/mod-playerbots> (GPL-2.0)
- **Derived upstream `mod-playerbots` commit**: `405f8119` (`origin/master` — *"Merge pull request #3039 from mod-playerbots/test-staging"*)

## Modifications

The files mirrored here include Hearthguard's custom `Companion*` subsystem extensions
(`CompanionBotMgr`, `CompanionBrain`, `CompanionCraft`, `CompanionDuty`, `CompanionLife`,
`CompanionQuest`, `CompanionLeadStrategy`, `CompanionCraftStrategy`, `ReviveFromCorpseAction`,
and `.companion` command integration) built on top of `mod-playerbots` commit `405f8119`.
