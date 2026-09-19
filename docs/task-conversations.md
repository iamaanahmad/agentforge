# Task conversations

Open **Chats** in the dashboard. Each task has its own saved conversation.
Use **New task** to start work. Select any task to return to its history.

- **Ask a question** answers from that conversation's saved evidence. It cannot use tools or change work.
- **Do work** starts a new run after the original task finishes. It keeps the original task's permissions and approval rules.
- When an agent needs information, it can ask a question and pause. Submit **Answer and resume** to continue.
- Progress, questions, errors, and results appear in the conversation. The open view refreshes every three seconds.
- **Work and decisions** opens approval requests, saved files, and detailed execution records.

Messages and answers survive service restarts. Sending the same request twice does not create duplicate work.
Only one follow-up can run in a conversation at a time. Different conversations can have separate queued work.
Worker capacity controls how many runs execute together.

Unsent drafts stay separate while switching chats. They clear on reload or sign-out.
Conversation context includes bounded recent history, not unlimited recall. Full saved messages remain visible.
A conversation supports up to 200 follow-ups. Start a new task after that limit.
Follow-up work on mission, scheduled, or child tasks must start through those workflows.

Questions are information requests, not approvals. Answering one does not authorize a restricted action.
Never put passwords or provider credentials in a conversation. Use the secure credential settings.
This feature does not send email notifications or stream individual model tokens.

## API

Authenticated owners can list `GET /api/chats` and read `GET /api/tasks/{id}/chat`.
Send a message with `POST /api/tasks/{id}/chat`:

```json
{"content":"Explain the result","mode":"ask","request_id":"unique-client-request-id"}
```

Use a stable request ID when retrying the same message. Reusing an ID with different content returns a conflict.
Answer a pending question with `POST /api/tasks/{task_id}/questions/{question_id}/answer` and `{"content":"Your answer"}`.
All writes require the session's CSRF header. Model routes and a running worker are required for replies.
