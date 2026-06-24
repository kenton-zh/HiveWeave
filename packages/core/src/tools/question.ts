/**
 * question tool — agent asks the user a question and waits for an answer.
 * Uses an in-memory question queue + polling from the frontend.
 */
import { randomUUID } from "crypto";
import { Effect, Schema } from "effect";

export interface PendingQuestion {
  id: string;
  agentId: string;
  question: string;
  options?: { label: string; description?: string }[];
  resolve?: (answer: string) => void;
  createdAt: number;
}

// In-memory question queue
const pendingQuestions = new Map<string, PendingQuestion>();

export function addQuestion(q: PendingQuestion) {
  pendingQuestions.set(q.id, q);
}

export function answerQuestion(id: string, answer: string): boolean {
  const q = pendingQuestions.get(id);
  if (!q || !q.resolve) return false;
  q.resolve(answer);
  pendingQuestions.delete(id);
  return true;
}

export function drainQuestions(): PendingQuestion[] {
  const result: PendingQuestion[] = [];
  for (const [id, q] of pendingQuestions) {
    if (Date.now() - q.createdAt > 600_000) {
      pendingQuestions.delete(id); // expire after 10 min
    } else {
      result.push({ id: q.id, agentId: q.agentId, question: q.question, options: q.options, createdAt: q.createdAt });
    }
  }
  return result;
}

export const QuestionInput = Schema.Struct({
  question: Schema.String.annotations({ description: "The question to ask the user." }),
  options: Schema.optional(Schema.Array(Schema.Struct({
    label: Schema.String.annotations({ description: "Option label (e.g. 'Fix all errors')." }),
    description: Schema.optional(Schema.String).annotations({ description: "Explanation of this option." }),
  }))).annotations({ description: "Up to 4 predefined choices. User can also type a custom answer." }),
});

export type QuestionInput = typeof QuestionInput.Type;

export function executeQuestion(agentId: string, rawInput: Record<string, any>): Effect.Effect<string, Error> {
  return Effect.gen(function* () {
    const input = yield* Schema.decodeUnknown(QuestionInput)(rawInput).pipe(
      Effect.mapError((e) => new Error(`Question input validation: ${e.message}`)),
    );
    return yield* Effect.async<string, Error>((resume) => {
      const id = randomUUID();
      const q: PendingQuestion = {
        id,
        agentId,
        question: input.question,
        options: input.options as any,
        createdAt: Date.now(),
        resolve: (answer: string) => {
          resume(Effect.succeed(answer));
        },
      };
      addQuestion(q);
    });
  });
}
