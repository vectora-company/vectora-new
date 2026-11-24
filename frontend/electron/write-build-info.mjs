import { execFileSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const repositoryRoot = path.resolve(scriptDirectory, "../..");
const outputPath = path.join(scriptDirectory, "src/build-info.generated.ts");

function gitOutput(args) {
  try {
    return execFileSync("git", args, {
      cwd: repositoryRoot,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
  } catch {
    return null;
  }
}

const candidateRevision =
  process.env.VECTORA_SOURCE_REVISION || gitOutput(["rev-parse", "HEAD"]);
const sourceRevision =
  candidateRevision && /^[0-9a-f]{7,64}$/i.test(candidateRevision)
    ? candidateRevision.toLowerCase()
    : null;
const sourceDirtyRaw = gitOutput(["status", "--porcelain"]);
const sourceDirty = sourceDirtyRaw === null ? null : sourceDirtyRaw.length > 0;

writeFileSync(
  outputPath,
  `export const vectoraBuildInfo = ${JSON.stringify({ sourceRevision, sourceDirty })} as const;\n`,
  "utf8",
);
