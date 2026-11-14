# MR2 Global Update migration workspace

This local repository prepares a history-preserving migration toward Ubuntu 24.04 / ROS 2 Jazzy / Gazebo Harmonic for the Orin NX rover. It is not a completed Jazzy port or a deployed release.

- Provisional source baseline: `74422f36c55140519f111cf6709f1725711b536a`, upstream default branch `2026`.
- Active candidate branch: `migration/jazzy`; local reference tag: `audit/2026-11-14`.
- `upstream` fetches from `https://github.com/URC-kaist/mr2-stack.git`; its push URL is disabled locally. No publishing remote is configured.
- GitHub repository creation is pending. This local clone is not yet a GitHub-recognized fork.
- Source/submodule history is retained. No application port, dependency installation or device update is performed by this preparation.

Use workstations for parallel development and the confirmed fully configurable Orin Nano for native candidate experiments. Keep the Orin NX rover on its operational deployment outside scheduled verification windows. Nano results do not establish NX hardware-encoder, carrier, production-load or field acceptance.

The English-first documentation and Korean translation are prepared in `~/docs/content/docs/global-update-project/`. The local Project blueprint, bilingual issue drafts and repository plan are in `~/docs/project-drafts/global-update/`. Replace these local references with the actual published links at dispatch time.

SW leader `@yongwhan-chung` determines human assignments and capacity. The requester asks Yongwhan to retrieve the Nano; this note does not notify or assign anyone. Joint HW/SW/operator verification and the team's release decision govern eventual cutover.

Before sector ports converge, record the actual operational NX reference, required release profiles, and disposition of pending rover-direct work. API incompatibility may justify isolated implementation/configuration, but this repository does not imply a from-scratch rewrite.

The existing build/deployment instructions describe their historical platform unless explicitly revised in migration work. Build candidate dependencies only in the intended isolated development environment, never as an experiment on the operational NX.

[한국어 안내](MIGRATION.ko.md)
