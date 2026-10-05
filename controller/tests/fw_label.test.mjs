// Tests for _firmwareLabel() in dashboard.jsx — the "(at last connect)"
// qualifier on the firmware version (#357).
//
//     node controller/tests/fw_label.test.mjs
//
// `firmware_ver` is written when a device REGISTERS, not when an update
// completes, because the register message is the only place the controller
// learns what a device is actually running. So between an OTA finishing and
// the device reconnecting, the panel reported the version the device was
// about to leave behind and said nothing. The value was right; the framing
// was not.
//
// The qualifier names a CONNECT rather than a time on purpose.
// `touch_device_seen` refreshes `last_seen` on every stats tick, so a relative
// timestamp would be wrong for every connected device and right only for one
// that is offline — the opposite of the reassurance a timestamp looks like it
// gives. That is the assertion most likely to be undone by a later
// "improvement", so it is here.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const HERE = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(HERE, "..", "static", "dashboard.jsx"), "utf8");

function liftFunction(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`dashboard.jsx no longer defines ${name}()`);
  let depth = 0;
  for (let i = src.indexOf("{", start); i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}" && --depth === 0) return src.slice(start, i + 1);
  }
  throw new Error(`could not find the end of ${name}`);
}

function liftConst(name) {
  const start = src.indexOf(`const ${name} =`);
  if (start < 0) throw new Error(`dashboard.jsx no longer defines ${name}`);
  // Slice to the statement's own semicolon. Searching for the next newline
  // would run past it when the const is the last thing on its line, and
  // swallow the function that follows.
  const end = src.indexOf(";", start);
  if (end < 0) throw new Error(`could not find the end of ${name}`);
  return src.slice(start, end + 1);
}

const { _firmwareLabel, FW_QUALIFIER } = await import(
  "data:text/javascript;base64," + Buffer.from(
    [liftConst("FW_QUALIFIER"), liftFunction("_firmwareLabel")].join("\n")
    + "\nexport { _firmwareLabel, FW_QUALIFIER };"
  ).toString("base64"));

let failures = 0;
function check(name, got, want) {
  if (got === want) return;
  failures++;
  console.error(`FAIL: ${name}\n      got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
}

check("a version is qualified", _firmwareLabel("2.25.0"), "2.25.0 (at last connect)");
check("a dev build keeps its shape", _firmwareLabel("2.25.0-3-gabc"),
      "2.25.0-3-gabc (at last connect)");
// A device that never registered has no version, and the callers render their
// own placeholder — so this must stay nullish rather than becoming
// "unknown (at last connect)".
check("no version is null", _firmwareLabel(null), null);
check("empty is null", _firmwareLabel(""), null);
check("undefined is null", _firmwareLabel(undefined), null);

// The rule that will be broken first by someone making it friendlier.
const banned = /\b(ago|minutes?|hours?|days?|just now|recently|last seen at)\b/i;
if (banned.test(FW_QUALIFIER)) {
  failures++;
  console.error(
    `FAIL: the qualifier names a time\n      ${JSON.stringify(FW_QUALIFIER)} — ` +
    "last_seen is refreshed on every stats tick, so a time is wrong for every " +
    "connected device and right only for one that is offline"
  );
}

// Every site that shows the value has to go through the helper, or one of them
// keeps reporting a version as current.
const displaySites = [
  ["the device list subtitle", "return <>{ipStr} · {device.device_id}"],
  ["the Firmware row", "row('Firmware',"],
  ["the On device LCD", '<Lcd label="On device"'],
  ["the detail header", "{_middleEllipsis(_firmwareLabel(device.firmware_ver)"],
];
for (const [where, needle] of displaySites) {
  const at = src.indexOf(needle);
  if (at < 0) {
    failures++;
    console.error(`FAIL: ${where} is gone — expected to find ${needle}`);
    continue;
  }
  const line = src.slice(src.lastIndexOf("\n", at) + 1, src.indexOf("\n", at));
  if (!line.includes("_firmwareLabel")) {
    failures++;
    console.error(
      `FAIL: ${where} shows firmware_ver unqualified\n      ${line.trim()}\n` +
      "      it reports a version recorded at registration as though it were " +
      "current (#357)"
    );
  }
}

// The sites that COMPARE firmware_ver must keep the bare value. The rollback
// poll and the reconnect check both test it for equality against a target, and
// a qualified string never equals one — which would report every successful
// update as a rollback.
for (const [where, needle] of [
  ["_pollReconnect", "_pollReconnect(res.version, device.firmware_ver)"],
  ["the rollback equality check", "up.version === device.firmware_ver"],
  ["the reconnect check", "d?.firmware_ver === targetVersion"],
]) {
  if (!src.includes(needle)) {
    failures++;
    console.error(
      `FAIL: ${where} no longer compares the bare version\n      ${needle}\n` +
      "      a qualified string never equals a target, so this reports every " +
      "successful update as a rollback"
    );
  }
}

if (failures) {
  console.error(`\n${failures} failure(s)`);
  process.exit(1);
}
console.log("fw_label: all checks passed");
