/**
 * todowrite tool — agent maintains a structured task list visible in the chat header.
 */
import { Effect, Schema } from "effect";

export interface TodoItem {
  content: string;
  status: "pending" | "in_progress" | "completed" | "cancelled";
}

export interface AgentTodos {
  agentId: string;
  todos: TodoItem[];
  updatedAt: number;
}

// In-memory todo store (per agent)
const todoStore = new Map<string, AgentTodos>();

export const TodoWriteInput = Schema.Struct({
  todos: Schema.Array(Schema.Struct({
    content: Schema.String.annotations({ description: "Task description." }),
    status: Schema.Literal("pending", "in_progress", "completed", "cancelled").annotations({
      description: "Task status: pending, in_progress, completed, or cancelled.",
    }),
  })).annotations({ description: "Complete list of current tasks. Replaces all previous todos." }),
});

export type TodoWriteInput = typeof TodoWriteInput.Type;

export function getTodos(agentId: string): AgentTodos | undefined {
  return todoStore.get(agentId);
}

export function getAgentsWithTodos(): string[] {
  return [...todoStore.keys()];
}

export function executeTodoWrite(agentId: string, rawInput: Record<string, any>): Effect.Effect<string, Error> {
  return Effect.gen(function* () {
    const input = yield* Schema.decodeUnknown(TodoWriteInput)(rawInput).pipe(
      Effect.mapError((e) => new Error(`TodoWrite input validation: ${e.message}`)),
    );
    const agentTodos: AgentTodos = {
      agentId,
      todos: input.todos.map((t: any) => ({
        content: t.content,
        status: t.status,
      })),
      updatedAt: Date.now(),
    };
    todoStore.set(agentId, agentTodos);
    const counts = {
      pending: input.todos.filter((t: any) => t.status === "pending").length,
      in_progress: input.todos.filter((t: any) => t.status === "in_progress").length,
      completed: input.todos.filter((t: any) => t.status === "completed").length,
      cancelled: input.todos.filter((t: any) => t.status === "cancelled").length,
    };
    return `Tasks updated: ${counts.completed} done, ${counts.in_progress} in progress, ${counts.pending} pending${counts.cancelled ? `, ${counts.cancelled} cancelled` : ""}.`;
  });
}
