# Notes for Claude sessions working on campy

## Keep docs/ current

`docs/` is campy's wiki: guides for hardware, live tracking, models, calibration and troubleshooting. `docs/hardware-guide.html` is the hardware guide's only source; the published copy (https://claude.ai/artifact/1iFm1x157Wfw4rrHQf2FTk) is made from it.

- **Same PR.** A PR that changes a default, a command, a measured number or a recommendation that a page in `docs/` states also updates that page. Search `docs/` for the number or option you changed. Say in the PR description what changed in the docs, or that nothing in `docs/` needed to.
- **The hardware guide's footer** says its version, its date and the newest PR it reflects. Update all three when you change the guide.
- **New results without a code change**, such as a rig test, get a docs PR of their own.
- **After the user merges a PR that changed `docs/hardware-guide.html`**, republish the artifact from dev's copy of that file, to the same URL. Read it first with the Artifact tool's `read` action, so the link stays the same.
- **Everything in `docs/` is public** (the repo is public). No IP addresses, host names, user names or SSH routes. No lab, institution or backup-software names, and no internal nicknames for machines: name them by make and model, so the Windows Intel PC is the "Dell Pro Max Slim FCS1250". Describe trained models by their settings (crop size, filters, max stride), not by their internal names.
