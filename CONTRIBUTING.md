# Contributing to the migration

Start with the [briefing](https://urc-kaist.github.io/docs/docs/global-update-project/en/), [starting card](https://urc-kaist.github.io/docs/docs/global-update-project/en/start/) and [Project](https://github.com/orgs/URC-kaist/projects/2). The Project requires organization access; ask the SW leader about work allocation.

## Branches and review

Use `main` as the candidate integration line. Create a small task branch, link its issue, and open a PR to this repository's `main`. Keep the operational Humble code in `URC-kaist/mr2-stack`; incorporate relevant operational fixes through reviewed merges/cherry-picks with recorded source SHAs. No automatic upstream synchronization is configured.

```bash
git clone --recurse-submodules https://github.com/URC-kaist/mr2-stack-jazzy.git
cd mr2-stack-jazzy
git switch -c task/your-migration-task
```

Agree changes to shared messages, TF, launch contracts and profiles through the integrator. Follow the issue's acceptance prerequisites; preparation may proceed in parallel. People and dates are allocated by the team, not inferred from the starter issues.

## Environments and evidence

Use separate Humble/Jazzy environments, shells and build outputs. Pin dependencies and source/submodule revisions. The first reproducibility work establishes clean recipes; the current application source is not yet claimed to build or run on Jazzy. CI/build coverage is work to establish, not an existing guarantee.

Each PR explains the changed behavior, environment, commands/results, evidence links and remaining target checks. Mark proposed procedures and unrun checks explicitly. Keep implementation completion separate from joint hardware acceptance. Large artifacts live in retained storage with manifest links, not Project fields.

## Hardware

Use the Nano for native experiments and coordinate shared bench changes. Keep development command/network paths isolated from the operational NX rover. NX changes require prepared operator-coordinated windows and recovery time. Physical acceptance and cutover remain joint HW/SW/operator work.
