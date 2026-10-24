// Run with `pnpm test` (node's built-in runner; Node 22.18+ strips the types). Mirrors tests/unit/test_setup_people.py.
import { test } from "node:test";
import assert from "node:assert/strict";

import { emailFor, inferEmailPattern, personKey, suggestedDomains } from "./people.ts";

test("infers each format", () => {
  const cases: [string, string][] = [
    ["alex.rivera@acme.dev", "{f}.{l}"], ["alex_rivera@acme.dev", "{f}_{l}"], ["alexrivera@acme.dev", "{f}{l}"],
    ["a.rivera@acme.dev", "{i}.{l}"], ["arivera@acme.dev", "{i}{l}"], ["rivera.alex@acme.dev", "{l}.{f}"],
    ["riveraalex@acme.dev", "{l}{f}"], ["Alex@ACME.dev", "{f}"],
  ];
  for (const [email, format] of cases) {
    assert.deepEqual(inferEmailPattern("Alex Rivera", email), { format, domain: "acme.dev" }, email);
  }
});

test("strips accents and needs a match", () => {
  assert.deepEqual(inferEmailPattern("José Núñez", "jose.nunez@x.io"), { format: "{f}.{l}", domain: "x.io" });
  assert.equal(inferEmailPattern("Alex Rivera", "ceo@acme.dev"), null);
  assert.equal(inferEmailPattern("Alex Rivera", "not-an-email"), null);
  assert.equal(inferEmailPattern("", "alex@acme.dev"), null);
});

test("applies the pattern", () => {
  const p = inferEmailPattern("Alex Rivera", "alex.rivera@acme.dev");
  assert.equal(emailFor("Sam Chen", p), "sam.chen@acme.dev");
  assert.equal(emailFor("Mary Ann Lee", p), "mary.lee@acme.dev");
  assert.equal(emailFor("Cher", p), null);
  assert.equal(emailFor("Cher", { format: "{f}", domain: "acme.dev" }), "cher@acme.dev");
  assert.equal(emailFor("Sam Chen", null), null);
});

test("person keys are unique and valid", () => {
  assert.equal(personKey("Alex Rivera", []), "alex");
  assert.equal(personKey("Alex Chen", ["alex"]), "alexc");
  assert.equal(personKey("Alex Cole", ["alex", "alexc"]), "alex2");
  assert.equal(personKey("Alex", ["alex", "alex2"]), "alex3");
  assert.equal(personKey("Élodie Brun", []), "elodie");
  assert.equal(personKey("3M Bot", []), "p3m");
});

test("suggested domains skip personal mail", () => {
  const emails = ["a@Acme.dev", "b@gmail.com", null, "c@acme.dev", "d@sub.acme.io", "e@proton.me"];
  assert.deepEqual(suggestedDomains(emails, ["gmail.com", "proton.me"]), ["acme.dev", "sub.acme.io"]);
});
