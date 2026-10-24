// Teammate helpers for step 1. Mirrors nua/setup/people.py (keep the two in step); the personal
// mail domains come from the server (`personal_domains` in GET /api/config), so the list lives in one place.

export type EmailPattern = { format: string; domain: string };

// Local-part formats, in the order they are tried: {f} first name, {l} last name, {i} first initial.
export const EMAIL_FORMATS = ["{f}.{l}", "{f}_{l}", "{f}{l}", "{i}.{l}", "{i}{l}", "{l}.{f}", "{l}{f}", "{f}"];

/** Lowercase ASCII words of a name: "José María-López" -> ["jose", "marialopez"]. */
export function nameTokens(name: string | null | undefined): string[] {
  return (name || "")
    .normalize("NFKD")
    .replace(/[^\x00-\x7f]/g, "")
    .toLowerCase()
    .split(/\s+/)
    .map((w) => w.replace(/[^a-z0-9]/g, ""))
    .filter(Boolean);
}

function render(format: string, tokens: string[]): string | null {
  if (!tokens.length || (format.includes("{l}") && tokens.length < 2)) return null;
  const first = tokens[0];
  const last = tokens[tokens.length - 1];
  return format.replace("{f}", first).replace("{l}", last).replace("{i}", first[0]);
}

/** The pattern `email` follows for `name`, or null when its local part matches no known format. */
export function inferEmailPattern(name: string | null | undefined, email: string | null | undefined): EmailPattern | null {
  const parts = (email || "").trim().toLowerCase().split("@");
  if (parts.length !== 2 || !parts[0] || !parts[1]) return null;
  const tokens = nameTokens(name);
  const format = EMAIL_FORMATS.find((f) => render(f, tokens) === parts[0]);
  return format ? { format, domain: parts[1] } : null;
}

/** The email `pattern` gives `name`, or null when the name lacks a needed part. */
export function emailFor(name: string | null | undefined, pattern: EmailPattern | null): string | null {
  if (!pattern) return null;
  const local = render(pattern.format, nameTokens(name));
  return local ? `${local}@${pattern.domain}` : null;
}

/** A new person key from a name: first name, then first name plus last initial, then a digit. */
export function personKey(name: string | null | undefined, taken: Iterable<string>): string {
  const used = new Set(taken);
  const tokens = nameTokens(name);
  let first = tokens.length ? tokens[0].replace(/[^a-z0-9_]/g, "") : "";
  if (!/^[a-z]/.test(first)) first = `p${first}`;
  const candidates = [first];
  if (tokens.length > 1) candidates.push(first + tokens[tokens.length - 1][0]);
  const free = candidates.find((c) => !used.has(c));
  if (free) return free;
  let n = 2;
  while (used.has(`${first}${n}`)) n += 1;
  return `${first}${n}`;
}

/** Teammates' email domains in first-seen order, without personal mail domains. */
export function suggestedDomains(emails: (string | null | undefined)[], personal: string[]): string[] {
  const out: string[] = [];
  for (const email of emails) {
    if (!email || !email.includes("@")) continue;
    const domain = email.trim().toLowerCase().split("@").pop() || "";
    if (domain && !personal.includes(domain) && !out.includes(domain)) out.push(domain);
  }
  return out;
}
