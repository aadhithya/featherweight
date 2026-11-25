# A tool that gets a token for its identity

A tool acts for the user whose identity the run has. featherweight carries the identity; the tool checks what it may do, and gets a credential with code of your own, at each call.

```python
# yours: db, mail_api (the client of the mail service), and Mailer and MailerInput (an agent that sends mail)
from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, Field

from featherweight import AgentRunner, RunContext


class Teacher(BaseModel):
    user_id: str
    tenant: str
    jwt: str | None = Field(None, exclude=True)  # only in the process that has it: not sent, so None on a worker


def send_email_tool(tokens, directory):
    """`tokens` and `directory` are yours: they are given to the tool when your application starts."""

    @tool
    async def send_email(to: str, subject: str, body: str, runtime: ToolRuntime[RunContext[Teacher]]) -> str:
        """Send an email as the signed-in teacher."""
        who = runtime.context.identity
        if who is None or not await directory.may(
            who.user_id, "send_email"
        ):  # re-checked now: the identity is a snapshot
            return "Not allowed."
        token = who.jwt or await tokens.access_token(
            who.user_id, scope="mail.send"
        )  # at each call: a long job outlives a token
        await mail_api.send(token, to, subject, body)
        return "Sent."

    return send_email


runner = AgentRunner(db, identity_type=Teacher)
result = await runner.run(Mailer, MailerInput(...), identity=Teacher(user_id="u1", tenant="uni"))
```

<!-- skill:omit -->
How `tokens.access_token(user_id, scope)` gets a token is your application's. The usual ways:

- **A stored refresh token,** in a vault keyed by the user id: the user connected their account once, and the provider mints a short-lived access token when asked. The usual way for Google, Microsoft or GitHub.
- **A service credential plus the user id,** for your own services: the worker authenticates as itself and passes the user id, and the service decides what that user may do.
- **A token exchange** (RFC 8693) or impersonation, if your identity provider supports it: the worker exchanges the identity for a token of that user.
- **A grant made when the job was submitted,** looked up by the job id: "this job may do this for this user until 3 pm". The most precise, because the user approved the job.

For an MCP server the token must be one **issued for that server**: the MCP authorization rules forbid passing on the token your own API was given.

- **Why re-check.** The identity was captured when the job was submitted. A queued job can run later, after a role changed or a user left: for a sensitive action, ask your application by user id.
- **Keep secrets out of the identity.** It sits in the queue message until the job is acknowledged. A field marked `exclude=True` is not sent, for a value that exists only in the submitting process: it needs a default, and on a worker the tool sees the default and asks by user id, as above.
- **Ask for the token at each call** and let your provider cache it: a long job outlives a token. If it fails, return text (`"Could not get a token."`), as any failure of a tool.
- The rules and what stays yours: [identity](../specs/identity.md#the-developers-responsibilities).
<!-- /skill:omit -->
