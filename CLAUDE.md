# Notes for Claude sessions working on campy

## Keep docs/ current

`docs/` is campy's wiki: guides for hardware, live tracking, models, calibration and troubleshooting. Two of its pages are HTML, each the only source of a published copy made from it:

| Source in `docs/` | Published copy |
|---|---|
| `docs/hardware-guide.html` (the hardware guide) | https://claude.ai/artifact/1iFm1x157Wfw4rrHQf2FTk |
| `docs/chronicrecord-run-book.html` (the ChronicRecord run book: how to run a multi-day Pico bout session with fiber photometry on that rig) | https://claude.ai/artifact/NFxr3z2Vvpc9G7gFnK255L |

Both copies are public ("anyone with the link"), and readers see each republish at once. Below, "a published page" means either one. Edit the source in `docs/`, never the published copy alone.

**Every session does its own docs work.** No session owns the guide or the wiki. Don't send results to another session for it to write up, and don't wait for one to do it: the session that has the result or the change does all of the steps below.

- **Same PR.** A PR that changes a default, a command, a measured number or a recommendation that a page in `docs/` states also updates that page. Search `docs/` for the number or option you changed. Say in the PR description what changed in the docs, or that nothing in `docs/` needed to.
- **New results without a code change**, such as a rig test or a benchmark run, get a docs PR of their own, opened by the session that got them, even if its task was only notes or measurements.
- **A published page's footer** says its version, its date and the newest PR it reflects ("through pull request N", N being your own PR). Update all three when you change the page. If another PR took your version number first, take the next one when you merge dev in.
- **The run book follows the ChronicRecord rig.** A PR that changes what a ChronicRecord session does or shows (`chronicrecord/`, the Pico, the panel's ChronicRecord steps or checks, the copy step, the rig's profile or disk), or a new result from that rig (a session, a photometry level, a wiring check), updates `docs/chronicrecord-run-book.html` too: its open questions at the top, the step it changes, and "What hasn't been tested yet".
- **After the user merges a PR that changed a published page's source**, republish that page from dev's file, to the same URL:
  1. `git fetch origin` and save `git show origin/dev:docs/<page>.html` to your scratchpad.
  2. Publish that file with the Artifact tool, passing the page's URL as `url`.
  3. If the publish is refused because another version is live, it shows you that version and the lines that differ. Check that every line only the live page has is an older form of a line dev changed. If so, apply dev's lines to a copy of the live page (leave out the `<!doctype …><body>` first line and the closing `</body></html>`), confirm the result equals dev's file, and publish it. If it is refused once more as identical content, publish it again unchanged. If the live page has something dev lacks, add it to `docs/` in a PR instead of dropping it.
- **A daily check backs this up.** A scheduled task compares each published page with dev's file every morning and republishes when dev is ahead, so a change isn't stranded when its session is archived before the merge. It doesn't replace the steps above.
- **Everything in `docs/` and in the published pages is public** (the repo is public). No IP addresses, host names, user names or SSH routes. No lab, institution or backup-software names, and no internal nicknames for machines: name them by make and model, so the Windows Intel PC is the "Dell Pro Max Slim FCS1250". Describe trained models by their settings (crop size, filters, max stride), not by their internal names.
