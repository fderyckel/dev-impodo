# Design QA evidence

**Source visual truth**

- Selected option 1 with collapsible sidebar:
  `C:\Users\francois.deRyckel\.codex\generated_images\019fb21c-02e2-7e73-9eeb-4902e6995a7c\call_bxeImqcYzIubyKp0WSND50vR.png`
- Latest user correction: keep the New project action visually clear of the red orbit and replace the creator credit with a Luxembourg attribution.

**Implementation evidence**

- Intended route: Projects
- Intended viewport: 1440 x 1024 CSS pixels at device scale factor 1
- Implementation screenshot: unavailable because permission to launch Microsoft Edge for the one-shot capture was declined.
- Source and implementation pixel dimensions: comparison unavailable.
- State: expanded desktop sidebar with populated project list.

**Full-view comparison evidence**

- Blocked. No valid browser-rendered implementation screenshot was captured after the correction.

**Focused region comparison evidence**

- Blocked. The Projects hero/action region and footer attribution could not be compared visually.

**Findings**

- [P2] Browser-rendered spacing verification is unavailable.
  Location: Projects hero and `.projects-home .app-main`.
  Evidence: the orbit position was moved from the upper-right corner to `calc(100% + 260px) -90px`, but the corrected screen could not be captured.
  Impact: the exact clearance between the New project action and the decorative orbit remains unverified at the target viewport.
  Fix: open Impodo in Microsoft Edge and confirm the action remains on the neutral background at desktop and narrow widths.

- [P2] Footer asset rendering is unavailable.
  Location: `.creator-credit` and `.luxembourg-flag`.
  Evidence: template and focused HTTP test confirm the new text and flag URL, but no browser capture confirms its final baseline alignment.
  Impact: a minor alignment issue could remain.
  Fix: visually confirm the Luxembourg flag aligns with the text baseline.

**Required fidelity surfaces**

- Fonts and typography: unchanged from the selected implementation; browser rendering not recaptured.
- Spacing and layout rhythm: orbit position corrected in CSS; visual clearance remains unverified.
- Colors and visual tokens: unchanged.
- Image quality and asset fidelity: real Luxembourg SVG flag asset from the `flag-icons` library added; browser rendering remains unverified.
- Copy and content: changed to `Made in Luxembourg` followed by the Luxembourg flag and `by FdR`.

**Primary interactions tested**

- Focused local browser security and page-rendering test passed.
- Sidebar browser interaction was not retested because the Edge capture permission was declined.
- Browser console errors were not checked.

**Comparison history**

- User-reported P2: New project action overlapped the red orbit.
- Fix: shifted the orbit farther beyond the right edge while retaining its scale.
- Post-fix visual evidence: blocked by unavailable browser capture.

**Implementation checklist**

- [x] Move the decorative orbit clear of the primary action.
- [x] Replace the creator credit.
- [x] Add and license the Luxembourg flag asset.
- [x] Update the focused page-rendering test.
- [ ] Capture and inspect the corrected page in Microsoft Edge.

**Follow-up polish**

- None identified without the browser-rendered comparison.

final result: blocked
