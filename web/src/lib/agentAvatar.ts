/** The avatar shown for an agent that hasn't picked one. 🦉 for Owlery — the
 * mail-hub owl, matching the app mark and the Hogwarts agent-naming universe. */
export const DEFAULT_AGENT_AVATAR = "🦉";

/** Offered in the agent-settings picker; the field also accepts any custom
 * emoji, so this is a shortlist rather than a constraint. */
export const AVATAR_CHOICES = [
  DEFAULT_AGENT_AVATAR,
  "🤖",
  "🧠",
  "🔬",
  "🛠️",
  "✍️",
  "📊",
  "🐙",
] as const;
