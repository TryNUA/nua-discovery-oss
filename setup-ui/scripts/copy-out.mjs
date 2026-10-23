// Copy the static export (out/) to nua/setup/static/, which `nua setup` serves and the Python package ships.
import { cpSync, existsSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(dirname(fileURLToPath(import.meta.url)));
const out = join(here, "out");
const dest = join(here, "..", "nua", "setup", "static");

if (!existsSync(join(out, "index.html"))) {
  console.error("no out/index.html: run `pnpm build` (next build) first");
  process.exit(1);
}
rmSync(dest, { recursive: true, force: true });
cpSync(out, dest, { recursive: true });
writeFileSync(join(dest, ".gitattributes"), "* -diff linguist-generated\n");
console.log(`copied ${out} -> ${dest}`);
