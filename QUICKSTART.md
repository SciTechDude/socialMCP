# SocialMCP — Quick Start

**For anyone, technical or not.** Five steps, about 15 minutes, most of it
waiting for LinkedIn to email you a file.

When you're done, you'll be able to ask your AI assistant things like:

> *Who did I meet at the conference last month, and who haven't I followed up with?*
>
> *Which people in my network work in enterprise sales?*
>
> *Remind me how I met Dana.*

Everything stays on your own computer. Nothing is uploaded anywhere.

---

## Step 1 — Ask LinkedIn for your data

1. Open LinkedIn in a browser.
2. Click **Me** (your photo, top right) → **Settings & Privacy**.
3. In the left menu, click **Data Privacy**.
4. Click **Get a copy of your data**.
5. Choose **Download larger data archive** (this is important — the smaller
   option leaves out the messages you sent with connection requests).
6. Click **Request archive**.

LinkedIn emails you a download link. It usually takes 10 minutes, occasionally
up to 24 hours. Carry on with Step 2 while you wait.

> **Why this works:** when you send a connection request with a note like
> *"Great meeting you at Seattle AI Week today!"*, LinkedIn keeps that note and
> its date. Your data archive contains all of them. SocialMCP reads those notes
> and turns them into a record of how and where you met each person — the thing
> LinkedIn itself stopped letting you keep.

---

## Step 2 — Install SocialMCP

You need **Python 3.11 or newer**. To check, open a terminal and type:

- **Mac:** press `Cmd+Space`, type `Terminal`, press Enter. Then type `python3 --version`
- **Windows:** press `Start`, type `PowerShell`, press Enter. Then type `python --version`

If it says 3.11 or higher, you're set. If not, or if it says "command not
found", install Python from [python.org/downloads](https://www.python.org/downloads/)
(click the big yellow button, run the installer, and **tick "Add Python to
PATH"** if Windows offers it).

Then install SocialMCP by copying this line into the same terminal window and
pressing Enter:

```
pip install socialmcp
```

On Mac, if that complains about permissions, use `pip3 install --user socialmcp`.

<details>
<summary>Not published to PyPI yet? Install from GitHub instead</summary>

```
pip install git+https://github.com/SciTechDude/socialMCP.git
```
</details>

---

## Step 3 — Connect it to your AI app

Type this one command:

```
socialmcp setup
```

It finds the AI apps already on your computer — Claude Desktop, Claude Code,
Cursor, Windsurf, VS Code, Zed — and connects them for you. You don't have to
edit any settings files. It makes a backup of anything it changes.

You'll see something like:

```
Connecting SocialMCP to the AI apps on this computer...

  OK   Claude Desktop: configured
         Quit and reopen Claude Desktop to pick up the change.
  OK   Cursor: configured
         Restart Cursor, then check Settings -> MCP.
```

**Don't have an AI app yet?** Install [Claude Desktop](https://claude.ai/download)
(free), then run `socialmcp setup` again.

---

## Step 4 — Put your LinkedIn file in the folder

When LinkedIn's email arrives, download the `.zip` file. **Don't unzip it.**

Then type:

```
socialmcp drop
```

This opens the folder where the file goes. Drag the `.zip` into it. Then type:

```
socialmcp ingest
```

You'll see a summary like `"contacts_new": 579`. That's it — your network is now
searchable on your own machine.

---

## Step 5 — Restart your AI app and ask it something

Quit your AI app completely and reopen it. Then try:

> *What event clusters are in my network?*

SocialMCP automatically spots bursts of connections — if you added 17 people
over three days, that was almost certainly a conference. It will show them with
placeholder names like *"13-15 Oct 2025 burst (17 connections)"*.

Tell your assistant what those events actually were:

> *The October 13-15 one was Seattle AI Week 2025. Please rename it.*

Now the good questions work:

> *Show me everyone from Seattle AI Week I never followed up with.*
>
> *Who in my network works in AI infrastructure?*
>
> *Make a note on Dana: wants a pilot in Q1, her VP owns the budget.*
>
> *Which invitations did I send that were never accepted?*

---

## If something isn't working

Type this. It checks everything and tells you exactly what to fix:

```
socialmcp doctor
```

Example output:

```
  OK   Python version                 Python 3.12.11
  OK   Your contacts                  579 people, 5 invites pending
  !    Smart search (sqlite-vec)      not installed, so searches match words literally
       -> Run: pip install sqlite-vec    (then: socialmcp reindex)
  !    Claude Desktop                 installed but not connected yet
       -> Run: socialmcp setup
```

Common issues:

| What you see | What to do |
|---|---|
| AI app doesn't show SocialMCP | Quit it **completely** and reopen. Config is only read at startup. |
| "command not found: socialmcp" | Close and reopen your terminal. If it persists, use `python3 -m socialmcp.cli` instead of `socialmcp`. |
| Searches feel too literal | Install [Ollama](https://ollama.com), then run `ollama pull nomic-embed-text` and `socialmcp reindex`. This adds meaning-based search and still runs entirely on your computer. |
| "no adapter recognized" | You unzipped the file, or it isn't a LinkedIn archive. Use the original `.zip` as downloaded. |

---

## Optional: better search

Out of the box, search matches words. Install [Ollama](https://ollama.com) (free,
runs locally, no account) and searching works by *meaning* — *"GTM background"*
will find a "Head of Revenue Operations" even though no word matches.

```
ollama pull nomic-embed-text
socialmcp reindex
```

Your contacts never leave your computer either way.

---

## Keeping it current

Request a fresh LinkedIn archive whenever you like — monthly works well. Drop it
in the same folder. SocialMCP recognizes what it has already seen, so nothing is
duplicated, and it tells you **who changed jobs** since last time.

If your AI app is open, it notices new files on its own; just ask *"what's new
in my network?"*

---

## Common questions

**Is this against LinkedIn's terms of service?**
No. SocialMCP never logs into LinkedIn, never uses your password or cookies,
and never scrapes any website. It only reads the data export file that LinkedIn
gives you on request, which is your data by law (GDPR / CCPA). That's the whole
design.

**Where is my data?**
In a single file on your computer, by default at `~/.socialmcp/socialmcp.db`.
Delete that file and everything is gone. Nothing is sent anywhere.

**Can my AI provider see my contacts?**
Only what you ask about in a conversation gets included in that conversation,
the same as anything else you type. The database itself is never uploaded. If
you want zero cloud involvement, use a local model via Ollama.

**Does this work with ChatGPT?**
Not today. ChatGPT can't connect to tools running on your own computer the way
Claude Desktop, Cursor and others can — it requires the tool to be hosted on
the public internet, which would mean putting your contact list on a server.
That trade-off is deliberately not made here. Claude Desktop is free and works.

**I'm not technical. Will I break something?**
`socialmcp setup` backs up any file it changes before changing it, and refuses
to overwrite settings it didn't create. `socialmcp doctor` tells you what's
wrong in plain English. The worst case is that an AI app doesn't see the tool,
which a restart usually fixes.
