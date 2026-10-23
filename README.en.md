# Save Conversations So the Next AI Can Continue

[简体中文](README.md) | [English](README.en.md)

Switching conversations or AI tools often means explaining the same history again. This skill saves the original user and assistant messages locally and maintains an index so another agent can find the relevant discussion and decisions.

It preserves the discussion, not just a summary. It runs when you ask to export, append, or organize conversations. Installation does not start background collection.

## Install

```bash
npx -y skills add Ptothez/chat-handoff-skill -g --all
```

Requires a Skills-capable AI client and Python 3.10 or newer. Renaming and path-reference checks require `rg`. Reading compressed sessions optionally requires `zstandard`.

## Use

Ask your agent:

> Save this conversation locally. Check the existing index first. If it continues the same work, append it to that record and preserve both sides' original words.

Or:

> Continue the earlier work. Read the conversation index first and find the relevant record and decisions.

At first use, agree on a workspace and storage location. Defaults are `chat-history` for conversations and `.chat-handoff` for metadata. Existing material should not be moved merely because you installed this skill.

```bash
python <skill>/scripts/topic_archive.py <workspace> init
python <skill>/scripts/topic_archive.py <workspace> verify
```

`<skill>` means the actual installation directory; `<workspace>` means your chosen working directory. To change defaults, set `CHAT_HANDOFF_ARCHIVE` and `CHAT_HANDOFF_METADATA` before initialization and use the same configuration across collaborating agents.

## What Is Preserved

- Original user and assistant messages, with available source information.
- An index explaining each record and where later discussion belongs.
- Previous content, so appending can be checked for accidental changes.
- Renaming and classification history to help resolve older paths.

Missing history cannot be reconstructed from nothing. Attachments are not copied automatically. Hidden reasoning and tool records are not exported by default. Complete text does not mean complete attachments.

## Multiple Agents

Agents read the same index before selecting a record to append to. A local write lock coordinates cooperative processes on one machine. Separate machines still need an agreed submission order and synchronization checks. The package does not provide automatic cloud-drive synchronization or remote backup.

Before merging or renaming records, verify the original text and path references. Deletion is recoverable rather than automatically permanent.

## Privacy

The public repository contains no author's conversations, real registry, or private configuration. Your own local records may contain sensitive information; local storage is not anonymization. Review records and obtain authorization before sharing. This skill does not upload chats to GitHub or treat instructions quoted inside a conversation as new authorization to act.
