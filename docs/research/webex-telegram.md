# Research: Webex Chat History to Telegram

Research date: 2026-06-25.

## Primary Sources

- Webex Messaging API, List Messages:
  https://developer.webex.com/messaging/docs/api/v1/messages/list-messages
- Webex Messaging API, Get Message Details:
  https://developer.webex.com/messaging/docs/api/v1/messages/get-message-details
- Webex Admin API, List Events:
  https://developer.webex.com/admin/docs/api/v1/events/list-events
- Telegram Bot API:
  https://core.telegram.org/bots/api
- Telegram APIs overview:
  https://core.telegram.org/api
- Telegram TDLib `sendMessage`:
  https://core.telegram.org/tdlib/docs/classtd_1_1td__api_1_1send_message.html
- Telegram imported messages:
  https://core.telegram.org/api/import
- Microsoft Q&A discussion on Webex to Teams migration:
  https://learn.microsoft.com/en-us/answers/questions/5671845/migrating-from-webex-to-teams
- Microsoft Teams external message import with Graph:
  https://learn.microsoft.com/en-us/microsoftteams/platform/graph-api/import-messages/import-external-messages-to-teams
- Microsoft Graph throttling guidance:
  https://learn.microsoft.com/en-us/graph/throttling
- Microsoft Graph JSON batching:
  https://learn.microsoft.com/en-us/graph/json-batching

## Findings

### Webex

Webex exposes official REST APIs for messaging resources such as rooms, people,
messages, and admin events. For individual migration, the relevant path is
enumerating rooms available to the authenticated user and paginating messages per
room. For organization migration, the tool should require a compliance/admin
identity and avoid trying to scrape end-user clients.

Important extraction concerns:

- Pagination and rate limits must be first-class.
- Attachments need separate download handling and checksums.
- Edits/deletes/reactions may require event/compliance data depending on scope.
- A normal user token should not be treated as an organization export token.

### Telegram

Telegram offers three relevant API paths:

- Bot API: HTTPS interface for bot accounts. Good for operational automation, but
  weak for faithful history migration.
- TDLib/Telegram API: full client API. Suitable for user-authorized migration
  tooling.
- MTProto imported messages flow: purpose-built for importing messages and media
  from foreign chat apps.

Telegram's imported messages flow validates an export file, checks whether the
selected destination peer can receive the import, initializes the import, uploads
associated media, and then starts the import. Imported messages are represented as
imported history in the destination UI.

## Decision for First Use Case

The best path for Webex -> Telegram is:

1. Use official Webex APIs for extraction.
2. Write a canonical local archive.
3. Convert that archive into Telegram import-compatible exports.
4. Use Telegram MTProto import where permitted.
5. Use Bot API or TDLib send-message replay only as a documented fallback.

This is faster, cheaper, and more robust than a fully agentic browser operator.
AI can help keep connector docs current, but the migration execution should be
scripted and testable.

## Reference Workflow: Webex to Teams

The Microsoft Q&A answer and official Teams import docs describe a workflow that
is useful beyond Teams:

1. Extract selected data from the third-party system.
2. Persist data in a structured form.
3. Map source containers to destination containers.
4. Map source users to destination identities.
5. Start destination migration/import mode.
6. Import historical messages with original timestamps where supported.
7. Complete destination migration mode.
8. Verify the target state and handle throttling/retries.

For Teams specifically, Microsoft Graph supports importing third-party messages
into Teams and uses migration mode to preserve historical timestamps and
conversation hierarchy. It requires application permissions such as
`Teamwork.Migrate.All`, imports messages via Graph requests, and documents a
five-requests-per-second-per-channel import limit. Graph throttling returns HTTP
429 with `Retry-After`; batching supports up to 20 requests but does not bypass
per-request throttling.

This confirms Exodus should not be hardcoded as Webex -> Telegram. The clean
architecture is source connector -> canonical archive -> mapping planner ->
destination migration session -> import/replay -> verification.

## Open Questions

- Which Webex license/admin role will the first organization migration have?
- Does the target Telegram setup require one supergroup per Webex room, one forum
  topic per room, or an archive channel model?
- Are private/direct Webex conversations in scope?
- Is media preservation mandatory for the first release, or can v1 text-only
  exports ship first?
- What retention/legal basis applies to organization-wide exports?
