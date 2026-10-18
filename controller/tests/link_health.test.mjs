// Tests for the device page's link health line: _linkHealthDetail().
//
//     node controller/tests/link_health.test.mjs
//
// The verdict itself is the controller's (em_link_health: the fleet's own
// recorded hours, since a threshold chosen in advance is the one nobody runs).
// What is decided here is what the page does with it — which level paints
// which colour, and the quiet case: a verdict the controller could not make
// ("not enough history yet", "not measured recently") is not a diagnosis, and
// drawing it would put a health line on every new and offline device until
// there was no difference between a line and a decoration.
//
// Same lifting as link_row.test.mjs: dashboard.jsx compiles to one classic
// script with no module boundary to import across, so the alternative is a
// second copy that drifts.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const HERE = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(HERE, "..", "static", "dashboard.jsx"), "utf8");
function lift(name) {
  const start = src.indexOf(`function ${name}`);
  if (start < 0) throw new Error(`dashboard.jsx no longer defines ${name}()`);
  let depth = 0, i = src.indexOf("{", start);
  for (; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") { depth--; if (depth === 0) break; }
  }
  return eval(`(${src.slice(start, i + 1)})`);
}
const linkHealthDetail = lift("_linkHealthDetail");

let failed = 0;
const check = (name, cond) => { if (!cond) { failed++; console.error(`FAIL: ${name}`); } };

// The controller's own sentences, one per level, as em_link_health builds them.
const ok   = { level: 'ok',    ratePct: 1.0,
               text: '1% of pings delayed over 200 ms',
               note: 'within the 10% bar the rest of this fleet set' };
const warn = { level: 'warn',  ratePct: 12.0,
               text: '12% of pings delayed over 200 ms, worst 4.8 s',
               note: '2 of the last 6 hours above the 10% bar the rest of this fleet set' };
const bad  = { level: 'error', ratePct: 25.0,
               text: '25% of pings delayed over 200 ms, worst 8.1 s',
               note: '2 of the last 6 hours above the 19% bar — the worst 1% of this fleet\'s hours' };

check("a health line is not red", linkHealthDetail(ok).sev === "ok");
check("a warn verdict is amber", linkHealthDetail(warn).sev === "warn");
check("an error verdict is red", linkHealthDetail(bad).sev === "bad");

// The number and the reason are the controller's and are shown unchanged: the
// page is a reader, not a second opinion that could disagree with the API.
check("the sentence is shown as it arrives", linkHealthDetail(warn).text === warn.text);
check("the bar it was measured against comes with it", linkHealthDetail(warn).note === warn.note);

// Absence, in all the shapes it arrives in: no history, no recent history, a
// device whose payload predates the field, and a level this build has never
// heard of. None of them is a diagnosis.
check("no verdict at all is nothing", linkHealthDetail(null) === null);
check("a device with no linkHealth shows nothing", linkHealthDetail(undefined) === null);
check("a verdict with no level shows nothing",
      linkHealthDetail({ level: null, text: 'Not enough history yet',
                         note: '2 of the last 6 hours measured' }) === null);
check("an unknown level shows nothing",
      linkHealthDetail({ level: 'urgent', text: 'x', note: 'y' }) === null);

// And it has to reach the page: the Status tab's link panel, off the device
// payload the API carries — not computed again there.
const status = src.slice(src.indexOf("{tab === 'status' &&"),
                         src.indexOf("{tab === 'activity' &&"));
check("the Status tab reads the verdict from the device payload",
      status.includes("_linkHealthDetail(device.linkHealth)"));
check("the line names itself", status.includes("Link health · "));
check("nothing else draws one",
      (src.match(/_linkHealthDetail\(/g) || []).length === 2);

if (failed) {
  console.error(`\n${failed} failure(s)`);
  process.exit(1);
}
console.log("link_health: all checks passed");
