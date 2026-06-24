/**
 * apply_patch tool — applies structured patches to files, ported from OpenCode.
 * Supports add, update, and delete operations on text files within the workspace.
 */
import * as fs from "fs";
import * as path from "path";
import { Effect, Schema } from "effect";

export const PatchOp = Schema.Literal("add", "update", "delete");
export const PatchEntry = Schema.Struct({
  op: PatchOp.annotations({ description: "Operation: add (new file), update (edit existing), or delete." }),
  filePath: Schema.String.annotations({ description: "File path relative to workspace root." }),
  content: Schema.optional(Schema.String).annotations({ description: "New file content (required for add)." }),
  oldString: Schema.optional(Schema.String).annotations({ description: "Text to find and replace (required for update)." }),
  newString: Schema.optional(Schema.String).annotations({ description: "Replacement text (required for update)." }),
});

export const ApplyPatchInput = Schema.Struct({
  description: Schema.optional(Schema.String).annotations({ description: "Brief summary of what this patch does." }),
  patches: Schema.Array(PatchEntry).annotations({ description: "Array of patch operations to apply in order." }),
});

export type ApplyPatchInput = typeof ApplyPatchInput.Type;

function applyOne(workspacePath: string, entry: typeof PatchEntry.Type): string {
  const filePath = path.resolve(workspacePath, entry.filePath);
  if (!filePath.startsWith(workspacePath)) {
    return `ERROR: Sandbox violation — "${entry.filePath}" outside workspace.`;
  }

  switch (entry.op) {
    case "add": {
      if (!entry.content) return `ERROR: add requires "content".`;
      if (fs.existsSync(filePath)) return `ERROR: File already exists: ${entry.filePath}`;
      fs.mkdirSync(path.dirname(filePath), { recursive: true });
      fs.writeFileSync(filePath, entry.content, "utf-8");
      return `Created ${entry.filePath} (${entry.content.length} bytes)`;
    }
    case "update": {
      if (!entry.oldString || entry.newString === undefined) return `ERROR: update requires "oldString" and "newString".`;
      if (!fs.existsSync(filePath)) return `ERROR: File not found: ${entry.filePath}`;
      const content = fs.readFileSync(filePath, "utf-8");
      if (!content.includes(entry.oldString)) {
        return `ERROR: oldString not found in ${entry.filePath}. The file may have changed. Reread it before patching.`;
      }
      const occurrences = content.split(entry.oldString).length - 1;
      if (occurrences > 1) {
        return `ERROR: oldString matches ${occurrences} times in ${entry.filePath}. Add more context to make it unique.`;
      }
      const newContent = content.replace(entry.oldString, entry.newString);
      fs.writeFileSync(filePath, newContent, "utf-8");
      const linesChanged = newContent.split("\n").length - content.split("\n").length;
      return `Updated ${entry.filePath} (${linesChanged >= 0 ? "+" : ""}${linesChanged} lines)`;
    }
    case "delete": {
      if (!fs.existsSync(filePath)) return `ERROR: File not found: ${entry.filePath}`;
      fs.unlinkSync(filePath);
      return `Deleted ${entry.filePath}`;
    }
    default:
      return `ERROR: Unknown operation "${(entry as any).op}".`;
  }
}

export function executeApplyPatch(workspacePath: string, rawInput: Record<string, any>): Effect.Effect<string, Error> {
  return Effect.gen(function* () {
    const input = yield* Schema.decodeUnknown(ApplyPatchInput)(rawInput).pipe(
      Effect.mapError((e) => new Error(`ApplyPatch input validation: ${e.message}`)),
    );
    if (!input.patches || input.patches.length === 0) {
      return yield* Effect.fail(new Error("apply_patch requires at least one patch entry."));
    }
    const results: string[] = [];
    for (const entry of input.patches) {
      results.push(applyOne(workspacePath, entry));
    }
    return results.join("\n");
  });
}
