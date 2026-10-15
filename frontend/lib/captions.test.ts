import { test } from "node:test";
import assert from "node:assert/strict";
import { activeCueIndex, formatTime, parseTime, toVtt, validateCues } from "./captions.ts";

test("formatTime", () => {
  assert.equal(formatTime(0), "00:00:00.000");
  assert.equal(formatTime(75.5), "00:01:15.500");
  assert.equal(formatTime(59.9996), "00:01:00.000");
  assert.equal(formatTime(3661.25), "01:01:01.250");
  assert.equal(formatTime(-2), "00:00:00.000");
});

test("parseTime accepts common forms", () => {
  assert.equal(parseTime("00:01:15.500"), 75.5);
  assert.equal(parseTime("01:15,5"), 75.5);
  assert.equal(parseTime("12.25"), 12.25);
  assert.equal(parseTime("1:00:00"), 3600);
  assert.equal(parseTime(formatTime(4321.987)), 4321.987);
});

test("parseTime rejects garbage", () => {
  for (const v of ["", "abc", "1:2:3:4", "00:61:00", "-1", "00:00:75"]) {
    assert.equal(parseTime(v), null, v);
  }
});

test("validateCues", () => {
  assert.deepEqual(validateCues([{ start: 0, end: 1, text: "a" }]), []);
  const errs = validateCues([
    { start: 0, end: 2, text: "a" },
    { start: 1, end: 0.5, text: " " },
  ]);
  assert.equal(errs.length, 3);
});

test("toVtt escapes and skips invalid cues", () => {
  const vtt = toVtt([
    { start: 0, end: 1, text: "a < b\n\nsecond" },
    { start: 2, end: 1, text: "bad" },
  ]);
  assert.ok(vtt.startsWith("WEBVTT\n\n1\n00:00:00.000 --> 00:00:01.000\na &lt; b\nsecond"));
  assert.ok(!vtt.includes("bad"));
});

test("activeCueIndex", () => {
  const cues = [
    { start: 0, end: 1, text: "a" },
    { start: 2, end: 3, text: "b" },
  ];
  assert.equal(activeCueIndex(cues, 0.5), 0);
  assert.equal(activeCueIndex(cues, 1.5), -1);
  assert.equal(activeCueIndex(cues, 2), 1);
});
