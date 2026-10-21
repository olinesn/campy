# Notes for Claude sessions working on campy

## Keep docs/ current

`docs/` is campy's wiki: guides for hardware, live tracking, models, calibration and troubleshooting. `docs/hardware-guide.html` is the hardware guide's only source; the published copy (https://claude.ai/artifact/1iFm1x157Wfw4rrHQf2FTk) is made from it. That copy is public ("anyone with the link"), and readers see each republish at once.

**Every session does its own docs work.** No session owns the guide or the wiki. Don't send results to another session for it to write up, and don't wait for one to do it: the session that has the result or the change does all of the steps below.

- **Same PR.** A PR that changes a default, a command, a measured number or a recommendation that a page in `docs/` states also updates that page. Search `docs/` for the number or option you changed. Say in the PR description what changed in the docs, or that nothing in `docs/` needed to.
- **New results without a code change**, such as a rig test or a benchmark run, get a docs PR of their own, opened by the session that got them, even if its task was only notes or measurements.
- **The hardware guide's footer** says its version, its date and the newest PR it reflects ("through pull request N", N being your own PR). Update all three when you change the guide. If another PR took your version number first, take the next one when you merge dev in.
- **After the user merges a PR that changed `docs/hardware-guide.html`**, republish the published copy from dev's file, to the same URL:
  1. `git fetch origin` and save `git show origin/dev:docs/hardware-guide.html` to your scratchpad.
  2. Publish that file with the Artifact tool, passing the guide's URL as `url`.
  3. If the publish is refused because another version is live, it shows you that version and the lines that differ. Check that every line only the live page has is an older form of a line dev changed. If so, apply dev's lines to a copy of the live page (leave out the `<!doctype …><body>` first line and the closing `</body></html>`), confirm the result equals dev's file, and publish it. If it is refused once more as identical content, publish it again unchanged. If the live page has something dev lacks, add it to `docs/` in a PR instead of dropping it.
- **A daily check backs this up.** A scheduled task compares the published guide with dev's file every morning and republishes when dev is ahead, so a guide change isn't stranded when its session is archived before the merge. It doesn't replace the steps above.
- **Everything in `docs/` and in the published guide is public** (the repo is public). No IP addresses, host names, user names or SSH routes. No lab, institution or backup-software names, and no internal nicknames for machines: name them by make and model, so the Windows Intel PC is the "Dell Pro Max Slim FCS1250". Describe trained models by their settings (crop size, filters, max stride), not by their internal names.
