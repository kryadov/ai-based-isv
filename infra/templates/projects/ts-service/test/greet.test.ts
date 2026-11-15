import { test } from "node:test";
import assert from "node:assert/strict";
import { greet } from "../src/greet.js";

test("greet uses the name", () => {
  assert.equal(greet("ada"), "hello, ada");
});

test("greet falls back to world", () => {
  assert.equal(greet("  "), "hello, world");
});
